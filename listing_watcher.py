#!/usr/bin/env python3
"""
Unified New Listing Watcher
Watches eBay, Vinted and Depop simultaneously for new listings
and fires a Discord notification the moment one appears.

Usage:
  python3 listing_watcher.py

Requirements:
  pip3 install requests
"""

import csv
import json
import os
import re
import random
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Fix macOS Python 3.13 SSL certificate verification issue
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE",      certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

import requests
from playwright.sync_api import sync_playwright, Playwright

# Single shared Playwright instance — only one sync instance allowed per process
_PW: Playwright | None = None

def _get_pw() -> Playwright:
    global _PW
    if _PW is None:
        _PW = sync_playwright().start()
    return _PW

def _stop_pw() -> None:
    global _PW
    if _PW is not None:
        try:
            _PW.stop()
        except Exception:
            pass
        _PW = None

# ── Shared config (mirrors tracker.py) ───────────────────────────────────────
DISCORD_WEBHOOK = os.environ.get("UNIFI_DISCORD_WEBHOOK", "")

DATASETS_DIR        = Path("datasets")
DATASETS_DIR.mkdir(exist_ok=True)
SAVED_SEARCHES_FILE = DATASETS_DIR / "saved_searches.json"
THRESHOLDS_FILE     = DATASETS_DIR / "thresholds.json"
SOLD_DB_PATH        = DATASETS_DIR / "sold_listings.db"

# ── Sold-listings SQLite DB ───────────────────────────────────────────────────
def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(SOLD_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sold_listings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT    NOT NULL,
            item_id     TEXT    NOT NULL,
            query       TEXT    NOT NULL,
            title       TEXT,
            price_gbp   REAL,
            price_raw   TEXT,
            currency    TEXT    DEFAULT 'GBP',
            condition   TEXT,
            date_sold   TEXT,
            url         TEXT,
            fetched_at  TEXT,
            UNIQUE(platform, item_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sl_query    ON sold_listings(query)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sl_platform ON sold_listings(platform)")
    conn.commit()
    return conn


def db_insert_sold(rows: list[dict]) -> int:
    """Insert rows into the shared sold DB. Returns count of newly inserted rows."""
    if not rows:
        return 0
    with _db_connect() as conn:
        cur = conn.executemany("""
            INSERT OR IGNORE INTO sold_listings
                (platform, item_id, query, title, price_gbp, price_raw,
                 currency, condition, date_sold, url, fetched_at)
            VALUES
                (:platform, :item_id, :query, :title, :price_gbp, :price_raw,
                 :currency, :condition, :date_sold, :url, :fetched_at)
        """, rows)
        conn.commit()
        return cur.rowcount


def db_stats() -> dict:
    """Return high-level DB stats for the settings/info page."""
    try:
        with _db_connect() as conn:
            total   = conn.execute("SELECT COUNT(*) FROM sold_listings").fetchone()[0]
            queries = conn.execute("SELECT COUNT(DISTINCT query) FROM sold_listings").fetchone()[0]
            plats   = conn.execute(
                "SELECT platform, COUNT(*) c FROM sold_listings GROUP BY platform ORDER BY c DESC"
            ).fetchall()
            oldest  = conn.execute("SELECT MIN(fetched_at) FROM sold_listings").fetchone()[0]
            newest  = conn.execute("SELECT MAX(fetched_at) FROM sold_listings").fetchone()[0]
        return {
            "total": total, "queries": queries,
            "platforms": {r["platform"]: r["c"] for r in plats},
            "oldest": oldest, "newest": newest,
        }
    except Exception:
        return {"total": 0, "queries": 0, "platforms": {}, "oldest": None, "newest": None}


def db_query_stats(query: str) -> dict:
    """Return price stats for a specific query across all platforms."""
    q = query.lower().strip()
    try:
        with _db_connect() as conn:
            rows = conn.execute("""
                SELECT platform, price_gbp FROM sold_listings
                WHERE LOWER(query)=? AND price_gbp IS NOT NULL AND price_gbp > 0
            """, (q,)).fetchall()
        if not rows:
            return {}
        by_plat: dict[str, list[float]] = {}
        for r in rows:
            by_plat.setdefault(r["platform"], []).append(r["price_gbp"])
        result = {}
        for plat, prices in by_plat.items():
            prices.sort()
            n = len(prices)
            result[plat] = {
                "count":  n,
                "median": round(prices[n // 2], 2),
                "mean":   round(sum(prices) / n, 2),
                "min":    round(prices[0], 2),
                "max":    round(prices[-1], 2),
            }
        return result
    except Exception:
        return {}


POLL_INTERVAL  = 300      # seconds between full poll cycles
VINTED_MAX     = 96
DEPOP_MAX      = 48

# Platform colours for Discord embeds
COLOURS = {
    "ebay":       0xE53238,
    "vinted":     0x007782,
    "depop":      0xFF4040,
    "mercari_jp": 0xFF0211,   # Mercari red
    "rakuten_jp":  0xBF0000,   # Rakuten crimson
}
ICONS = {
    "ebay":       "🛒",
    "vinted":     "👗",
    "depop":      "📦",
    "mercari_jp": "🇯🇵",
    "rakuten_jp": "🎌",
}

# ── JPY → GBP conversion (cached 1 hour) ─────────────────────────────────────
_JPY_RATE: dict = {"rate": 0.0053, "ts": 0}   # fallback ~£1 = ¥190

def _get_jpy_gbp() -> float:
    now = time.time()
    if now - _JPY_RATE["ts"] < 3600:
        return _JPY_RATE["rate"]
    try:
        r = requests.get("https://open.er-api.com/v6/latest/JPY", timeout=5)
        rate = r.json()["rates"]["GBP"]
        _JPY_RATE.update({"rate": rate, "ts": now})
        print(f"  💱 JPY→GBP: {rate:.5f}  (~¥{1/rate:.0f} per £1)")
    except Exception:
        pass
    return _JPY_RATE["rate"]

def _jpy_to_gbp(yen: int) -> float:
    return round(yen * _get_jpy_gbp(), 2)

FIELDNAMES = [
    "platform", "item_id", "title", "price", "currency",
    "condition", "brand", "size", "seller", "location",
    "image_url", "url", "listed_at", "fetched_at", "search_query",
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── Keyword relevance filter ──────────────────────────────────────────────────
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "for", "in", "on", "of", "with",
    "by", "from", "to", "at", "is", "it", "its", "s", "mens", "womens",
    "ladies", "girls", "boys", "unisex", "size", "new", "used",
}

def _required_terms(query: str) -> list[str]:
    """
    Break the query into must-have keywords, stripping noise words.
    Multi-word brand names are kept as individual tokens so 'AllSaints'
    (no space) still matches 'all' + 'saints' as substrings.
    """
    words = [w.strip("'\"()") for w in query.lower().split()]
    return [w for w in words if w and w not in _STOP_WORDS and len(w) > 1]


def _item_matches(item: dict, terms: list[str]) -> bool:
    """
    Return True only if every required term appears somewhere in the
    listing's title or brand field (case-insensitive substring match).
    'AllSaints' satisfies both 'all' and 'saints' automatically.
    """
    haystack = (
        (item.get("title") or "") + " " + (item.get("brand") or "")
    ).lower().replace("-", " ").replace("_", " ")

    return all(term in haystack for term in terms)


def filter_results(rows: list[dict], query: str) -> tuple[list[dict], int]:
    """Filter rows to only those matching the query keywords. Returns (matches, dropped)."""
    terms = _required_terms(query)
    if not terms:
        return rows, 0
    matched = [r for r in rows if _item_matches(r, terms)]
    return matched, len(rows) - len(matched)


# ── CSV helpers ───────────────────────────────────────────────────────────────
def csv_path(query: str, platform: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    return DATASETS_DIR / f"{slug}_{platform}_new.csv"


def load_seen_ids(path: Path) -> set:
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["item_id"] for row in csv.DictReader(f)}


def append_rows(path: Path, rows: list[dict]) -> None:
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(rows)


# ── eBay sold scraper ────────────────────────────────────────────────────────
_EBAY_SOLD_URL = (
    "https://www.ebay.co.uk/sch/i.html"
    "?_nkw={query}&LH_Complete=1&LH_Sold=1&LH_PrefLoc=1&_pgn={page}"
)
_SOLD_FIELDS = ["title", "price", "condition", "date_sold", "url"]


def _parse_sold_page(page) -> list[dict]:
    rows = []
    for item in page.query_selector_all("li.s-card[data-listingid]"):
        title_el  = item.query_selector(".s-card__title span")
        price_el  = item.query_selector(".s-card__price")
        cond_el   = item.query_selector(".s-card__subtitle span")
        date_el   = item.query_selector(".s-card__caption span")
        link_el   = item.query_selector("a.s-card__link")

        price_raw = (price_el.inner_text().strip() if price_el else "")
        if " to " in price_raw:
            price_raw = price_raw.split(" to ")[0]
        price_clean = price_raw.replace("£", "").replace(",", "").strip()

        rows.append({
            "title":      (title_el.inner_text().strip() if title_el else ""),
            "price":      price_clean,
            "condition":  (cond_el.inner_text().strip()  if cond_el  else ""),
            "date_sold":  (date_el.inner_text().strip()  if date_el  else ""),
            "url":        (link_el.get_attribute("href") if link_el  else ""),
        })
    return rows


def scrape_ebay_sold(query: str, max_pages: int = 5, min_records: int = 0) -> float | None:
    """Scrape eBay UK sold listings, save CSV, return median price."""
    _STEALTH = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
    )
    slug      = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    sold_path = DATASETS_DIR / f"{slug}_sold.csv"

    print(f"\n  🔍 Scraping eBay UK sold listings for '{query}'...")
    browser = _get_pw().chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--lang=en-GB"],
    )
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
    ctx.add_init_script(_STEALTH)
    page = ctx.new_page()
    # Seed eBay session cookie
    try:
        page.goto("https://www.ebay.co.uk", wait_until="domcontentloaded", timeout=15_000)
        page.wait_for_timeout(1500)
    except Exception:
        pass

    all_rows = []
    for page_num in range(1, max_pages + 1):
        url = _EBAY_SOLD_URL.format(query=query.replace(" ", "+"), page=page_num)
        print(f"  [Page {page_num}] Fetching...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2000)
            # Try primary selector, fall back to any s-card
            try:
                page.wait_for_selector(
                    "li.s-card[data-listingid], li[data-view='mi:1686|iid:1']",
                    timeout=15_000,
                )
            except Exception:
                pass  # Parse whatever is on the page
        except Exception as e:
            print(f"  ❌ Page {page_num} error: {e}")
            break

        rows = _parse_sold_page(page)
        if not rows:
            # Try JS-based extraction as fallback
            raw = page.evaluate(r"""() => {
                return Array.from(document.querySelectorAll('li.s-card[data-listingid], li[data-view]'))
                    .filter(el => el.querySelector('a[href*="ebay.co.uk/itm/"]'))
                    .map(el => {
                        const t = el.querySelector('.s-card__title, h3, .lvtitle');
                        const p = el.querySelector('.s-card__price, .s-item__price');
                        return { title: t ? t.innerText.trim() : '', price: p ? p.innerText.trim() : '' };
                    }).filter(r => r.title && r.price);
            }""")
            if raw:
                rows = [{"title": r["title"], "price": r["price"],
                         "condition": "", "date_sold": "", "url": ""} for r in raw]
        all_rows.extend(rows)
        print(f"     {len(rows)} listings (total so far: {len(all_rows)})")

        nxt = page.query_selector("a.pagination__next, [aria-label='Go to next search page']")
        if not nxt or nxt.get_attribute("aria-disabled"):
            break
        time.sleep(random.uniform(2, 4))

    browser.close()

    if not all_rows:
        print("  ❌ No sold listings scraped.")
        return None

    if min_records > 0 and len(all_rows) < min_records:
        print(f"  ⚠️  Only {len(all_rows)} records collected — below minimum of {min_records}.")
        print(f"     Try increasing the Pages setting to collect more data.")

    # Save CSV
    with open(sold_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_SOLD_FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    print(f"  💾 Saved {len(all_rows)} records → {sold_path}")

    # Write to shared DB
    now = datetime.now(timezone.utc).isoformat()
    db_rows = []
    for r in all_rows:
        try:
            price_gbp = float(r["price"]) if r["price"] else None
        except ValueError:
            price_gbp = None
        db_rows.append({
            "platform":   "ebay",
            "item_id":    r.get("url", "").rstrip("/").split("/")[-1].split("?")[0] or r["title"][:40],
            "query":      query,
            "title":      r.get("title", ""),
            "price_gbp":  price_gbp,
            "price_raw":  r.get("price", ""),
            "currency":   "GBP",
            "condition":  r.get("condition", ""),
            "date_sold":  r.get("date_sold", ""),
            "url":        r.get("url", ""),
            "fetched_at": now,
        })
    inserted = db_insert_sold(db_rows)
    print(f"  🗄️  DB: {inserted} new rows added ({len(db_rows) - inserted} already existed)")

    prices = []
    for r in all_rows:
        try:
            prices.append(float(r["price"]))
        except ValueError:
            pass

    if not prices:
        return None

    prices.sort()
    median = prices[len(prices) // 2]
    print(f"  📊 Median: £{median:.2f}  |  Mean: £{sum(prices)/len(prices):.2f}"
          f"  |  Min: £{min(prices):.2f}  |  Max: £{max(prices):.2f}")
    return median


def scrape_mercari_jp_sold(query: str) -> int:
    """
    Scrape Mercari JP sold/trading listings and write to shared DB.
    Returns number of new rows inserted.
    """
    print(f"\n  🇯🇵 Scraping Mercari JP sold listings for '{query}'...")
    browser = _get_pw().chromium.launch(headless=True)
    ctx  = browser.new_context(user_agent=UA, locale="ja-JP")
    page = ctx.new_page()

    api_items: list[dict] = []

    def _on_response(resp):
        if "api.mercari.jp/v2/entities:search" in resp.url and resp.status == 200:
            try:
                data = resp.json()
                api_items.extend(data.get("items", []))
            except Exception:
                pass

    page.on("response", _on_response)

    url = (
        f"https://jp.mercari.com/search?keyword={requests.utils.quote(query)}"
        "&status=sold_out&sort=created_time&order=desc"
    )
    try:
        page.goto(url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  ❌ Mercari JP sold error: {e}")
    finally:
        browser.close()

    if not api_items:
        print("  ❌ No sold items found.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    db_rows = []
    for it in api_items:
        try:
            price_jpy = int(it.get("price", 0) or 0)
            price_gbp = _jpy_to_gbp(price_jpy) if price_jpy else None
            thumb = (it.get("thumbnails") or it.get("photos") or [""])[0]
            db_rows.append({
                "platform":  "mercari_jp",
                "item_id":   it["id"],
                "query":     query,
                "title":     it.get("name") or it.get("title") or "",
                "price_gbp": price_gbp,
                "price_raw": str(price_jpy),
                "currency":  "GBP",
                "condition": str(it.get("itemConditionId") or ""),
                "date_sold": datetime.fromtimestamp(
                    int(it.get("updated") or it.get("created") or 0), tz=timezone.utc
                ).isoformat() if (it.get("updated") or it.get("created")) else "",
                "url":       f"https://jp.mercari.com/item/{it['id']}",
                "fetched_at": now,
            })
        except Exception:
            continue

    inserted = db_insert_sold(db_rows)
    print(f"  ✅ {len(db_rows)} items scraped → {inserted} new rows added to DB")
    if db_rows:
        prices_gbp = [r["price_gbp"] for r in db_rows if r["price_gbp"]]
        if prices_gbp:
            prices_gbp.sort()
            n = len(prices_gbp)
            print(f"  📊 Median: £{prices_gbp[n//2]:.2f}  Min: £{prices_gbp[0]:.2f}  Max: £{prices_gbp[-1]:.2f}")
    return inserted


def load_ebay_threshold(query: str) -> float | None:
    """Return 50% of the eBay sold median (bargain threshold), or None."""
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    path = DATASETS_DIR / f"{slug}_sold.csv"
    if not path.exists():
        return None
    prices = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = str(row.get("price", "") or "").replace("£", "").replace(",", "").strip()
            try:
                prices.append(float(raw))
            except ValueError:
                pass
    if not prices:
        return None
    prices.sort()
    median    = prices[len(prices) // 2]
    threshold = round(median * 0.50, 2)
    print(f"  📊 eBay sold median: £{median:.2f}  →  50% bargain threshold: £{threshold:.2f}"
          f"  ({len(prices)} records)")
    return threshold


def _load_threshold_config() -> dict:
    if not THRESHOLDS_FILE.exists():
        return {}
    try:
        with open(THRESHOLDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_effective_threshold(query: str, name: str = None) -> float | None:
    """Return bargain threshold from thresholds.json config, else 50% of eBay sold median."""
    cfg        = _load_threshold_config()
    search_cfg = cfg.get(name or "", cfg.get(query, {}))
    mode       = search_cfg.get("mode", "percent")

    if mode == "fixed":
        try:
            val = float(search_cfg.get("fixed") or 0)
            if val > 0:
                print(f"  📊 Custom fixed threshold: £{val:.2f}")
                return val
        except (ValueError, TypeError):
            pass

    # Percent of eBay sold median
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    path = DATASETS_DIR / f"{slug}_sold.csv"
    if not path.exists():
        return None
    prices = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = str(row.get("price", "") or "").replace("£", "").replace(",", "").strip()
            try:
                prices.append(float(raw))
            except ValueError:
                pass
    if not prices:
        return None
    prices.sort()
    median    = prices[len(prices) // 2]
    pct       = float(search_cfg.get("percent", 50)) / 100
    threshold = round(median * pct, 2)
    print(f"  📊 eBay sold median: £{median:.2f}  →  {pct*100:.0f}% threshold: £{threshold:.2f}"
          f"  ({len(prices)} records)")
    return threshold


# ── Saved searches ────────────────────────────────────────────────────────────
def load_saved_searches() -> list[dict]:
    if not SAVED_SEARCHES_FILE.exists():
        return []
    try:
        with open(SAVED_SEARCHES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_searches(searches: list[dict]) -> None:
    DATASETS_DIR.mkdir(exist_ok=True)
    with open(SAVED_SEARCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(searches, f, indent=2)


def add_saved_search(name: str, query: str, platforms: list[str], interval_min: int) -> None:
    searches = load_saved_searches()
    # Overwrite if name already exists
    searches = [s for s in searches if s["name"].lower() != name.lower()]
    searches.append({
        "name":         name,
        "query":        query,
        "platforms":    platforms,
        "interval_min": interval_min,
    })
    save_searches(searches)
    print(f"  ✅ Saved search '{name}' stored.")


def delete_saved_search(name: str) -> None:
    searches = load_saved_searches()
    before = len(searches)
    searches = [s for s in searches if s["name"].lower() != name.lower()]
    if len(searches) == before:
        print(f"  ❌ No saved search named '{name}'.")
    else:
        save_searches(searches)
        print(f"  🗑  Deleted saved search '{name}'.")


def multi_poll(watches: list[dict], scrapers: dict) -> None:
    """
    Poll multiple saved searches in a round-robin loop.
    Each watch: {name, query, platforms, interval_min}
    """
    # Per-watch state
    state = []
    for w in watches:
        platforms  = w["platforms"]
        query      = w["query"]
        interval_s = w.get("interval_min", 5) * 60
        seen       = {p: load_seen_ids(csv_path(query, p)) for p in platforms}
        threshold  = get_effective_threshold(query, w.get("name"))
        exclude    = [kw.lower().strip() for kw in w.get("exclude_keywords", []) if kw.strip()]
        first_run  = {p: not seen[p] for p in platforms}
        state.append({
            "watch":      w,
            "seen":       seen,
            "threshold":  threshold,
            "exclude":    exclude,
            "first_run":  first_run,
            "interval_s": interval_s,
            "next_poll":  0,   # run immediately on first tick
        })
        print(f"  📌 '{w['name']}' ({query}) on {', '.join(platforms)}"
              f" every {w.get('interval_min', 5)} min")
        if threshold:
            print(f"     📊 Bargain threshold: £{threshold:.2f}")

    print(f"\n  Monitoring {len(watches)} searches — Ctrl+C to stop\n" + "─" * 60)

    while True:
        now = time.time()
        for s in state:
            if now < s["next_poll"]:
                continue

            w         = s["watch"]
            query     = w["query"]
            platforms = w["platforms"]
            threshold = s["threshold"]
            exclude   = s["exclude"]
            ts        = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{ts}] '{w['name']}' — checking {', '.join(platforms)}...")

            for platform in platforms:
                raw_rows         = scrapers[platform].fetch(query)
                rows, dropped    = filter_results(raw_rows, query)
                new_rows         = [r for r in rows if r["item_id"] not in s["seen"][platform]]
                if exclude:
                    new_rows = [r for r in new_rows
                                if not any(kw in r.get("title", "").lower() for kw in exclude)]

                if not raw_rows:
                    print(f"  {ICONS[platform]} {platform:<8} — fetch returned nothing")
                    time.sleep(random.uniform(1, 3))
                    continue

                if dropped:
                    print(f"  {ICONS[platform]} {platform:<8} — {dropped} irrelevant filtered"
                          f" ({len(rows)} matched)")

                if not rows:
                    print(f"  {ICONS[platform]} {platform:<8} — no listings matched keywords")
                    time.sleep(random.uniform(1, 3))
                    continue

                if new_rows and s["first_run"][platform]:
                    for r in new_rows:
                        s["seen"][platform].add(r["item_id"])
                    append_rows(csv_path(query, platform), new_rows)
                    priced = sorted(
                        [r for r in new_rows if _parse_price(r.get("price", "")) != float("inf")],
                        key=lambda r: _parse_price(r["price"]),
                    )
                    print(f"  {ICONS[platform]} {platform.capitalize()} — seeded {len(new_rows)} | cheapest 5:")
                    for r in priced[:5]:
                        pval     = _parse_price(r["price"])
                        btag     = " 🔥" if (threshold and pval < threshold) else ""
                        size_str = (r.get("size") or "").ljust(7)
                        print(f"     £{r['price']:>7}  {size_str}  {r['title'][:45]}{btag}")
                        print(f"              {r['url']}")
                        send_discord(r, query, threshold=threshold)
                        time.sleep(0.5)
                    s["first_run"][platform] = False
                    time.sleep(random.uniform(1, 3))
                    continue

                s["first_run"][platform] = False

                # Show current cheapest
                priced = [r for r in rows if _parse_price(r.get("price", "")) != float("inf")]
                if priced:
                    cheapest    = min(priced, key=lambda r: _parse_price(r["price"]))
                    bargain_tag = " 🔥" if (threshold and _parse_price(cheapest["price"]) < threshold) else ""
                    print(f"  {ICONS[platform]} {platform:<8} — cheapest: "
                          f"£{cheapest['price']}  {cheapest.get('size') or '':>6}  "
                          f"{cheapest['title'][:40]}{bargain_tag}")

                if new_rows:
                    append_rows(csv_path(query, platform), new_rows)
                    for r in new_rows:
                        s["seen"][platform].add(r["item_id"])
                    print(f"  {ICONS[platform]} {platform:<8} — ✅ {len(new_rows)} NEW")
                    for r in new_rows:
                        price   = f"£{r['price']}" if r["price"] else "?"
                        pval    = _parse_price(r.get("price", ""))
                        bargain = " 🔥 BARGAIN" if (threshold and pval < threshold) else ""
                        print(f"     {price:>8}  {r['title'][:55]}{bargain}")
                        print(f"             {r['url']}")
                        send_discord(r, query, threshold=threshold)
                        if threshold and pval < threshold:
                            print("BARGAIN_ITEM:" + json.dumps({
                                "platform": platform, "title": r["title"],
                                "price": r["price"], "url": r.get("url", ""),
                                "image_url": r.get("image_url", ""),
                                "savings": round(threshold - pval, 2), "query": query,
                            }), flush=True)
                        time.sleep(0.5)
                else:
                    print(f"  {ICONS[platform]} {platform:<8} — no new listings")

                time.sleep(random.uniform(1, 3))

            s["next_poll"] = time.time() + s["interval_s"]

        time.sleep(10)  # check every 10s whether any watch is due


# ── Analyse saved listings ─────────────────────────────────────────────────────
def _parse_price(val: str) -> float:
    try:
        return float(str(val or "").replace("£", "").replace(",", "").strip())
    except ValueError:
        return float("inf")


def analyse_listings(query: str, platform: str) -> None:
    path = csv_path(query, platform)
    if not path.exists():
        print(f"\n  No data file found: {path}")
        print(f"  Run the watcher first to seed listings.")
        return

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print(f"\n  CSV is empty: {path}")
        return

    rows.sort(key=lambda r: _parse_price(r.get("price", "")))

    print(f"\n  {len(rows)} listings on {platform.capitalize()} — cheapest first\n")
    print(f"  {'Price':>8}  {'Size':<8}  {'Condition':<22}  Title")
    print("  " + "─" * 85)
    for r in rows:
        price = f"£{r['price']}" if r.get("price") else "?"
        size  = (r.get("size") or "—")[:8]
        cond  = (r.get("condition") or "—")[:22]
        title = (r.get("title") or "")[:48]
        print(f"  {price:>8}  {size:<8}  {cond:<22}  {title}")

    valid = [_parse_price(r.get("price", "")) for r in rows
             if _parse_price(r.get("price", "")) != float("inf")]
    if valid:
        print(f"\n  Min: £{min(valid):.2f}  |  "
              f"Max: £{max(valid):.2f}  |  "
              f"Mean: £{sum(valid)/len(valid):.2f}  |  "
              f"Median: £{sorted(valid)[len(valid)//2]:.2f}")

    # Show eBay sold threshold comparison if available
    threshold = load_ebay_threshold(query)
    if threshold:
        below = [r for r in rows if _parse_price(r.get("price","")) < threshold]
        print(f"\n  🔥 {len(below)} listings are BELOW the eBay sold median of £{threshold:.2f}:")
        for r in below[:10]:
            savings = threshold - _parse_price(r["price"])
            print(f"     £{r['price']:>7}  saves ~£{savings:.2f}  {r.get('title','')[:55]}")
            print(f"              {r.get('url','')}")


# ── Discord ───────────────────────────────────────────────────────────────────
def send_discord(item: dict, query: str, threshold: float | None = None) -> None:
    if not DISCORD_WEBHOOK:
        return
    platform = item["platform"]
    price    = f"£{item['price']}" if item["price"] else "?"

    price_val = _parse_price(item.get("price", ""))
    is_bargain = (
        threshold is not None
        and price_val != float("inf")
        and price_val < threshold
    )

    fields = [
        {"name": "💰 Price",     "value": price,                          "inline": True},
        {"name": "🏷 Condition", "value": item.get("condition") or "—",   "inline": True},
        {"name": "📐 Size",      "value": item.get("size") or "—",        "inline": True},
        {"name": "🏷 Brand",     "value": item.get("brand") or "—",       "inline": True},
        {"name": "👤 Seller",    "value": item.get("seller") or "—",      "inline": True},
        {"name": "📍 Location",  "value": item.get("location") or "—",    "inline": True},
        {"name": "🔗 Link",      "value": item.get("url") or "—",         "inline": False},
    ]
    if is_bargain:
        savings = threshold - price_val
        fields.append({
            "name":   "🔥 vs eBay Sold Median",
            "value":  f"£{price_val:.2f} vs £{threshold:.2f} median — saves ~£{savings:.2f}",
            "inline": False,
        })

    embed_title = (
        f"🔥 BARGAIN — {ICONS[platform]} New on {platform.capitalize()}"
        if is_bargain
        else f"{ICONS[platform]} New listing on {platform.capitalize()}"
    )
    colour = 0x00C853 if is_bargain else COLOURS[platform]   # bright green for bargains

    payload = {
        "embeds": [{
            "title":       embed_title,
            "description": f"**{item.get('title', '')}**",
            "color":       colour,
            "fields":      fields,
            "footer": {
                "text": (
                    f"{platform.capitalize()} Watcher  •  "
                    f"{datetime.now().strftime('%d %b %Y %H:%M')}  |  "
                    f"Search: {query}"
                )
            },
        }]
    }
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 204:
            bargain_tag = "  🔥 BARGAIN ALERT" if is_bargain else ""
            print(f"     🔔 Discord sent{bargain_tag}")
        else:
            print(f"     ⚠️  Discord error {r.status_code}")
    except Exception as e:
        print(f"     ⚠️  Discord error: {e}")


# ── eBay UK (Playwright) ──────────────────────────────────────────────────────
class EbayScraper:
    BASE   = "https://www.ebay.co.uk"
    SEARCH = BASE + "/sch/i.html"
    _STEALTH = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
    )
    _JS = r"""() => {
        const clean = s => s
            .replace(/^NEW LISTING\s*/i, '')
            .replace(/\s*\nOpens in a new window or tab$/i, '')
            .trim();
        return Array.from(document.querySelectorAll('li.s-card[data-listingid]'))
            .filter(el => el.querySelector("a[href*='ebay.co.uk/itm/']"))
            .map(el => {
                const link  = el.querySelector("a[href*='ebay.co.uk/itm/']");
                const title = el.querySelector('.s-card__title');
                const price = el.querySelector('.s-card__price');
                const img   = el.querySelector('img.s-card__image');
                const cond  = el.querySelector('.s-card__subtitle');
                return {
                    id:    el.getAttribute('data-listingid'),
                    title: title ? clean(title.innerText) : '',
                    price: price ? price.innerText.replace(/\s+/g,' ').trim() : '',
                    href:  link  ? link.href.split('?')[0] : '',
                    img:   img   ? img.src  : '',
                    cond:  cond  ? cond.innerText.split('·')[0].trim() : '',
                };
            });
    }"""

    def __init__(self):
        self._browser = _get_pw().chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--lang=en-GB"],
        )
        self._ctx = self._browser.new_context(
            user_agent=UA, locale="en-GB",
            viewport={"width": 1280, "height": 800},
        )
        self._ctx.add_init_script(self._STEALTH)
        self._page = self._ctx.new_page()
        try:
            self._page.goto(self.BASE, wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            pass
        print(f"  {ICONS['ebay']} eBay browser ready")

    def fetch(self, query: str) -> list[dict]:
        url = f"{self.SEARCH}?_nkw={requests.utils.quote(query)}&_sop=10&LH_PrefLoc=1"
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_selector("li.s-card[data-listingid]", timeout=10_000)
            self._page.wait_for_timeout(800)
            cards = self._page.evaluate(self._JS)
        except Exception as e:
            print(f"  ❌ eBay error: {e}")
            return []

        results = []
        for c in cards:
            try:
                raw   = c.get("price", "")
                price = raw.split(" to ")[0].replace("£", "").replace(",", "").strip()
                results.append({
                    "platform":     "ebay",
                    "item_id":      c["id"],
                    "title":        c.get("title") or "",
                    "price":        price,
                    "currency":     "GBP",
                    "condition":    c.get("cond") or "",
                    "brand":        "",
                    "size":         "",
                    "seller":       "",
                    "location":     "UK",
                    "image_url":    c.get("img") or "",
                    "url":          c.get("href") or "",
                    "listed_at":    "",
                    "fetched_at":   datetime.now(timezone.utc).isoformat(),
                    "search_query": query,
                })
            except Exception:
                continue
        return results

    def close(self):
        try:
            self._browser.close()
        except Exception:
            pass


# ── Vinted (Playwright) ───────────────────────────────────────────────────────
class VintedScraper:
    BASE = "https://www.vinted.co.uk"

    def __init__(self):
        self._browser = _get_pw().chromium.launch(headless=True)
        self._ctx     = self._browser.new_context(
            locale="en-GB",
            user_agent=UA,
        )
        # Seed cookies by visiting homepage once
        page = self._ctx.new_page()
        try:
            page.goto(self.BASE, wait_until="domcontentloaded", timeout=20_000)
        except Exception:
            pass
        finally:
            page.close()
        print(f"  {ICONS['vinted']} Vinted browser ready")

    def fetch(self, query: str) -> list[dict]:
        url = f"{self.BASE}/api/v2/catalog/items"
        params = f"search_text={requests.utils.quote(query)}&order=newest_first&per_page={VINTED_MAX}"
        try:
            resp = self._ctx.request.get(
                f"{url}?{params}",
                headers={
                    "Accept":          "application/json, text/plain, */*",
                    "Referer":         f"{self.BASE}/",
                    "sec-fetch-dest":  "empty",
                    "sec-fetch-mode":  "cors",
                    "sec-fetch-site":  "same-origin",
                },
                timeout=20_000,
            )
            items = resp.json().get("items", [])
        except Exception as e:
            print(f"  ❌ Vinted fetch error: {e}")
            return []

        results = []
        for it in items:
            try:
                price_d = it.get("price", {}) or {}
                user    = it.get("user", {})  or {}
                results.append({
                    "platform":     "vinted",
                    "item_id":      str(it.get("id", "")),
                    "title":        it.get("title", "").strip(),
                    "price":        price_d.get("amount", it.get("price", "")),
                    "currency":     price_d.get("currency_code", "GBP"),
                    "condition":    it.get("status", ""),
                    "brand":        it.get("brand_title", ""),
                    "size":         it.get("size_title", ""),
                    "seller":       user.get("login", ""),
                    "location":     user.get("city", "") or it.get("city", ""),
                    "url":          (f"{self.BASE}/items/{it.get('id')}-{it.get('slug','')}"
                                    ).rstrip("-"),
                    "image_url":    (it.get("photos") or [{}])[0].get("url", ""),
                    "listed_at":    it.get("created_at_ts", ""),
                    "fetched_at":   datetime.now(timezone.utc).isoformat(),
                    "search_query": query,
                })
            except Exception:
                continue
        return results

    def close(self):
        try:
            self._browser.close()
        except Exception:
            pass


# ── Depop (Playwright DOM scraping) ──────────────────────────────────────────
class DepopScraper:
    WEB_BASE = "https://www.depop.com"
    _STEALTH = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
    )
    _JS = r"""() => {
        const cards = Array.from(document.querySelectorAll('li')).filter(
            li => li.querySelector("a[href*='/products/']")
        );
        return cards.map(li => {
            const link   = li.querySelector("a[href*='/products/']");
            const img    = li.querySelector('img');
            const texts  = Array.from(li.querySelectorAll('p'))
                .map(p => p.textContent.trim()).filter(t => t.length > 0);
            const prices = texts.filter(t => t.includes('£'));
            const sizes  = texts.filter(t => /^(UK|EU|US|One)/i.test(t));
            const brand  = texts.find(t => !t.includes('£') && !/^(UK|EU|US|One)/i.test(t)) || '';
            return {
                href:  link ? link.href : null,
                price: prices[0] || null,
                size:  sizes[0]  || null,
                brand: brand,
                img:   img ? img.src : null,
            };
        }).filter(c => c.href);
    }"""

    def __init__(self):
        self._browser = _get_pw().chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--lang=en-GB"],
        )
        self._ctx  = self._browser.new_context(locale="en-GB", user_agent=UA, viewport={"width": 1280, "height": 800})
        self._ctx.add_init_script(self._STEALTH)
        self._page = self._ctx.new_page()
        print(f"  {ICONS['depop']} Depop browser ready")

    def fetch(self, query: str) -> list[dict]:
        url = f"{self.WEB_BASE}/search/?q={requests.utils.quote(query)}&sort=NewestFirst"
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                self._page.wait_for_selector("a[href*='/products/']", timeout=12_000)
            except Exception:
                self._page.wait_for_timeout(4000)
            cards = self._page.evaluate(self._JS)
        except Exception as e:
            print(f"  ❌ Depop fetch error: {e}")
            return []

        if not cards:
            print(f"  ❌ Depop — 0 cards found in DOM")
            return []

        print(f"  {ICONS['depop']} Depop — {len(cards)} items via DOM")
        results = []
        for card in cards:
            try:
                href  = card["href"] or ""
                slug  = href.rstrip("/").split("/")[-1]
                parts = slug.split("-")
                seller = parts[0] if parts else ""
                title_parts = parts[1:-1] if len(parts) > 2 else parts
                title = " ".join(title_parts).title()
                raw_price = (card.get("price") or "").replace("£", "").replace(",", "").strip()
                results.append({
                    "platform":     "depop",
                    "item_id":      slug,
                    "title":        title[:120],
                    "price":        raw_price,
                    "currency":     "GBP",
                    "condition":    "",
                    "brand":        card.get("brand") or "",
                    "size":         card.get("size") or "",
                    "seller":       seller,
                    "location":     "",
                    "image_url":    card.get("img") or "",
                    "url":          href,
                    "listed_at":    "",
                    "fetched_at":   datetime.now(timezone.utc).isoformat(),
                    "search_query": query,
                })
            except Exception:
                continue
        return results

    def close(self):
        try:
            self._browser.close()
        except Exception:
            pass

# ── Mercari Japan (Playwright) ────────────────────────────────────────────────
class MercariJPScraper:
    BASE = "https://jp.mercari.com"

    _JS = r"""() => {
        // Strategy 1: Next.js pre-rendered data (fastest, no DOM needed)
        try {
            const nd = window.__NEXT_DATA__;
            if (nd) {
                const pp = nd.props?.pageProps || {};
                const raw = pp.items || pp.initialItems
                         || pp.searchResult?.items
                         || pp.data?.items || [];
                if (raw.length > 0) {
                    return raw.map(it => ({
                        href:  'https://jp.mercari.com/item/' + it.id,
                        price: String(it.price || ''),
                        name:  it.name || '',
                        img:   (it.thumbnails || [])[0] || it.thumbnail_url || '',
                        _src:  'nextdata',
                    }));
                }
            }
        } catch(e) {}

        // Strategy 2: mer-item-thumbnail web components
        const wc = Array.from(document.querySelectorAll('mer-item-thumbnail'));
        if (wc.length > 0) {
            return wc.map(el => ({
                href:  el.getAttribute('item-url') || ('https://jp.mercari.com/item/' + el.getAttribute('item-id')),
                price: el.getAttribute('price') || '',
                name:  el.getAttribute('name') || '',
                img:   el.getAttribute('thumbnail-url') || '',
                _src:  'webcomponent',
            })).filter(i => i.href && i.href.includes('/item/'));
        }

        // Strategy 3: generic li with /item/ link
        const lis = Array.from(document.querySelectorAll('li')).filter(
            li => li.querySelector('a[href*="/item/"]')
        );
        if (lis.length > 0) {
            return lis.map(el => {
                const link = el.querySelector('a[href*="/item/"]');
                const img  = el.querySelector('img');
                const all  = Array.from(el.querySelectorAll('span, p'));
                const prEl = all.find(s => /[¥￥]/.test(s.innerText));
                return {
                    href:  link ? link.href : null,
                    price: prEl ? prEl.innerText.replace(/[¥￥,\s]/g, '') : null,
                    name:  link ? (link.getAttribute('aria-label') || link.title || link.innerText.trim()) : null,
                    img:   img  ? img.src : null,
                    _src:  'dom',
                };
            }).filter(i => i.href && i.href.includes('/item/'));
        }
        return [];
    }"""

    def __init__(self):
        self._browser = _get_pw().chromium.launch(headless=True)
        self._ctx     = self._browser.new_context(user_agent=UA, locale="ja-JP")
        self._page    = self._ctx.new_page()
        print(f"  {ICONS['mercari_jp']} Mercari JP browser ready")

    def fetch(self, query: str) -> list[dict]:
        url = (
            f"{self.BASE}/search?keyword={requests.utils.quote(query)}"
            "&status=on_sale&sort=created_time&order=desc"
        )
        try:
            self._page.goto(url, wait_until="networkidle", timeout=30_000)
            self._page.wait_for_timeout(2000)
            cards = self._page.evaluate(self._JS)
        except Exception as e:
            print(f"  ❌ Mercari JP error: {e}")
            return []

        if cards:
            print(f"  {ICONS['mercari_jp']} Mercari JP — {len(cards)} items via {cards[0].get('_src','?')}")

        results = []
        for card in cards:
            try:
                href      = card.get("href") or ""
                item_id   = href.rstrip("/").split("/")[-1]
                raw       = (card.get("price") or "").replace("¥", "").replace(",", "").strip()
                price_jpy = int(raw) if raw.isdigit() else None
                price_gbp = _jpy_to_gbp(price_jpy) if price_jpy else None
                results.append({
                    "platform":     "mercari_jp",
                    "item_id":      item_id,
                    "title":        card.get("name") or "",
                    "price":        str(price_gbp) if price_gbp else raw,
                    "currency":     "GBP" if price_gbp else "JPY",
                    "condition":    "",
                    "brand":        "",
                    "size":         "",
                    "seller":       "",
                    "location":     "Japan",
                    "image_url":    card.get("img") or "",
                    "url":          href,
                    "listed_at":    "",
                    "fetched_at":   datetime.now(timezone.utc).isoformat(),
                    "search_query": query,
                })
            except Exception:
                continue
        return results

    def close(self):
        try:
            self._browser.close()
        except Exception:
            pass


# ── Yahoo Auctions Japan (Playwright) ─────────────────────────────────────────
class RakutenJPScraper:
    BASE = "https://search.rakuten.co.jp/search/mall"

    _JS = r"""() => {
        const containers = Array.from(document.querySelectorAll('.searchresultitem'));
        return containers.map(c => {
            const link  = c.querySelector('a[href*="item.rakuten.co.jp"]');
            const img   = c.querySelector('img');
            return {
                href:     link ? link.href : null,
                title:    img  ? img.alt  : (link ? link.innerText.trim() : null),
                price:    c.getAttribute('data-track-price'),
                item_id:  c.getAttribute('data-track-itemid'),
                img:      img  ? img.src  : null,
            };
        }).filter(i => i.href && i.item_id);
    }"""

    def __init__(self):
        self._browser = _get_pw().chromium.launch(headless=True)
        self._ctx     = self._browser.new_context(user_agent=UA, locale="ja-JP")
        self._page    = self._ctx.new_page()
        print(f"  {ICONS['rakuten_jp']} Rakuten JP browser ready")

    def fetch(self, query: str) -> list[dict]:
        url = f"{self.BASE}/{requests.utils.quote(query)}/?s=2"
        try:
            self._page.goto(url, wait_until="networkidle", timeout=30_000)
            self._page.wait_for_timeout(1500)
            cards = self._page.evaluate(self._JS)
        except Exception as e:
            print(f"  ❌ Rakuten JP error: {e}")
            return []

        results = []
        for card in cards:
            try:
                price_jpy = int(card["price"]) if card.get("price") and str(card["price"]).isdigit() else None
                price_gbp = _jpy_to_gbp(price_jpy) if price_jpy else None
                results.append({
                    "platform":     "rakuten_jp",
                    "item_id":      card["item_id"].replace("/", "_"),
                    "title":        card.get("title") or "",
                    "price":        str(price_gbp) if price_gbp else (card.get("price") or ""),
                    "currency":     "GBP" if price_gbp else "JPY",
                    "condition":    "",
                    "brand":        "",
                    "size":         "",
                    "seller":       "",
                    "location":     "Japan",
                    "image_url":    card.get("img") or "",
                    "url":          card.get("href") or "",
                    "listed_at":    "",
                    "fetched_at":   datetime.now(timezone.utc).isoformat(),
                    "search_query": query,
                })
            except Exception:
                continue
        return results

    def close(self):
        try:
            self._browser.close()
        except Exception:
            pass


# ── Poll loop ─────────────────────────────────────────────────────────────────
def poll(query: str, platforms: list[str], scrapers: dict, name: str = None, exclude_keywords: list = None) -> None:
    seen      = {p: load_seen_ids(csv_path(query, p)) for p in platforms}
    threshold = get_effective_threshold(query, name)
    exclude   = [kw.lower().strip() for kw in (exclude_keywords or []) if kw.strip()]

    print(f"\n  Watching: '{query}'")
    for p in platforms:
        print(f"  {ICONS[p]} {p.capitalize():<8} — {len(seen[p])} known IDs loaded")
    if threshold:
        print(f"  📊 Bargain alert: flag listings at or below £{threshold:.2f}")
    if exclude:
        print(f"  🚫 Excluding keywords: {', '.join(exclude)}")
    print(f"\n  Polling every {POLL_INTERVAL // 60} min  |  Ctrl+C to stop\n")
    print("─" * 60)

    # First run: silently load existing IDs without alerting
    first_run = {p: not seen[p] for p in platforms}  # True if nothing cached yet

    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] Checking all platforms...")

        for platform in platforms:
            print(f"  {ICONS[platform]} {platform:<10} — fetching...", flush=True)
            raw_rows         = scrapers[platform].fetch(query)
            rows, dropped    = filter_results(raw_rows, query)
            new_rows         = [r for r in rows if r["item_id"] not in seen[platform]]
            if exclude:
                new_rows = [r for r in new_rows
                            if not any(kw in r.get("title", "").lower() for kw in exclude)]

            if not raw_rows:
                print(f"  {ICONS[platform]} {platform:<8} — fetch returned nothing")
                time.sleep(random.uniform(2, 4))
                continue

            if dropped:
                print(f"  {ICONS[platform]} {platform:<8} — {dropped} irrelevant listings filtered out"
                      f" ({len(rows)} matched query keywords)")

            if not rows:
                print(f"  {ICONS[platform]} {platform:<8} — no listings matched keywords")
                time.sleep(random.uniform(2, 4))
                continue

            if new_rows and first_run[platform]:
                # First ever run — seed cache and emit ALL items to New Listings tab
                for r in new_rows:
                    seen[platform].add(r["item_id"])
                append_rows(csv_path(query, platform), new_rows)
                priced = sorted(
                    [r for r in new_rows if _parse_price(r.get("price","")) != float("inf")],
                    key=lambda r: _parse_price(r["price"])
                )
                if priced:
                    lo = _parse_price(priced[0]["price"])
                    hi = _parse_price(priced[-1]["price"])
                    print(f"  {ICONS[platform]} {platform.capitalize()} — seeded {len(new_rows)} listings  |  £{lo:.2f}–£{hi:.2f}")
                for r in new_rows:
                    pval = _parse_price(r.get("price", ""))
                    is_b = bool(threshold and pval < threshold)
                    print("BARGAIN_ITEM:" + json.dumps({
                        "platform": platform, "title": r["title"],
                        "price": r["price"], "url": r.get("url", ""),
                        "image_url": r.get("image_url", ""),
                        "is_bargain": is_b,
                        "savings": round(threshold - pval, 2) if is_b else 0,
                        "query": query,
                    }), flush=True)
                first_run[platform] = False
                time.sleep(random.uniform(2, 4))
                continue

            first_run[platform] = False

            # Show price range currently on the platform
            priced = sorted([r for r in rows if _parse_price(r.get("price", "")) != float("inf")],
                            key=lambda r: _parse_price(r["price"]))
            if priced:
                lo  = _parse_price(priced[0]["price"])
                hi  = _parse_price(priced[-1]["price"])
                btag = " 🔥" if (threshold and lo < threshold) else ""
                print(f"  {ICONS[platform]} {platform:<8} — £{lo:.2f}–£{hi:.2f}  ({len(priced)} listings){btag}")
                print("PRICE_RANGE:" + json.dumps({
                    "platform": platform, "query": query,
                    "min": lo, "max": hi, "count": len(priced),
                }), flush=True)

            if new_rows:
                append_rows(csv_path(query, platform), new_rows)
                for r in new_rows:
                    seen[platform].add(r["item_id"])
                print(f"  {ICONS[platform]} {platform:<8} — ✅ {len(new_rows)} NEW")
                for r in new_rows:
                    price     = f"£{r['price']}" if r["price"] else "?"
                    pval      = _parse_price(r.get("price", ""))
                    bargain   = " 🔥 BARGAIN" if (threshold and pval < threshold) else ""
                    print(f"     {price:>8}  {r['title'][:55]}{bargain}")
                    print(f"             {r['url']}")
                    send_discord(r, query, threshold=threshold)
                    is_b = bool(threshold and pval < threshold)
                    print("BARGAIN_ITEM:" + json.dumps({
                        "platform": platform, "title": r["title"],
                        "price": r["price"], "url": r.get("url", ""),
                        "image_url": r.get("image_url", ""),
                        "is_bargain": is_b,
                        "savings": round(threshold - pval, 2) if is_b else 0,
                        "query": query,
                    }), flush=True)
                    time.sleep(0.5)
            else:
                print(f"  {ICONS[platform]} {platform:<8} — no new listings")

            time.sleep(random.uniform(2, 4))

        print(f"\n  Sleeping {POLL_INTERVAL // 60} min...")
        time.sleep(POLL_INTERVAL)


# ── CLI helpers ───────────────────────────────────────────────────────────────
def _platform_menu() -> list[str]:
    print("\n  Platforms:")
    print("  [1] eBay only")
    print("  [2] Vinted only")
    print("  [3] Depop only")
    print("  [4] Vinted + Depop")
    print("  [5] All three  (default)\n")
    choice = input("  Choice [5]: ").strip() or "5"
    return {
        "1": ["ebay"],
        "2": ["vinted"],
        "3": ["depop"],
        "4": ["vinted", "depop"],
        "5": ["ebay", "vinted", "depop"],
    }.get(choice, ["ebay", "vinted", "depop"])


def _ensure_scrapers(platforms: list[str], scrapers: dict) -> None:
    if "ebay"       in platforms and "ebay"       not in scrapers:
        scrapers["ebay"]       = EbayScraper();  print(f"  {ICONS['ebay']} eBay ready")
    if "vinted"     in platforms and "vinted"     not in scrapers:
        scrapers["vinted"]     = VintedScraper()
    if "depop"      in platforms and "depop"      not in scrapers:
        scrapers["depop"]      = DepopScraper()
    if "mercari_jp" in platforms and "mercari_jp" not in scrapers:
        scrapers["mercari_jp"] = MercariJPScraper()
    if "rakuten_jp"  in platforms and "rakuten_jp"  not in scrapers:
        scrapers["rakuten_jp"] = RakutenJPScraper()


def _ebay_sold_flow(query: str) -> None:
    slug     = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    sold_csv = DATASETS_DIR / f"{slug}_sold.csv"
    if sold_csv.exists():
        refresh = input(f"\n  eBay sold data found ({sold_csv.name}). Refresh it? [y/N]: ").strip().lower()
        if refresh == "y":
            mp = input("  Max pages [5]: ").strip()
            scrape_ebay_sold(query, max_pages=int(mp) if mp.isdigit() else 5)
    else:
        print(f"\n  No eBay sold data for '{query}'.")
        ans = input("  Scrape eBay sold prices now for bargain detection? [Y/n]: ").strip().lower()
        if ans != "n":
            mp = input("  Max pages [5]: ").strip()
            scrape_ebay_sold(query, max_pages=int(mp) if mp.isdigit() else 5)


def _ensure_playwright_browser() -> None:
    """Install Playwright's Chromium if not present — needed on fresh Railway containers."""
    pw_env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    search_dir = Path(pw_env) if pw_env else Path.home() / ".cache" / "ms-playwright"
    if search_dir.exists() and any(search_dir.glob("chromium*")):
        return
    print("[playwright] Chromium not found — installing (takes ~60s on first run)...", flush=True)
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
        env=env,
    )
    if result.returncode != 0:
        print("[playwright] --with-deps failed, retrying without system deps...", flush=True)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], env=env)
    print("[playwright] Chromium install done.", flush=True)


# ── Non-interactive watch mode (used by GUI subprocesses) ────────────────────
def _run_watch_cli() -> None:
    """
    Parse --query / --platforms / --interval from sys.argv and run poll().
    No interactive prompts — uses existing eBay sold CSV for threshold.
    """
    _ensure_playwright_browser()

    args = sys.argv[1:]

    def _arg(flag: str) -> str | None:
        try:
            return args[args.index(flag) + 1]
        except (ValueError, IndexError):
            return None

    query        = _arg("--query") or ""
    plat_str     = _arg("--platforms") or "vinted,depop"
    interval_min = int(_arg("--interval") or "5")
    name         = _arg("--name") or query
    exclude_str  = _arg("--exclude") or ""
    platforms    = [p.strip() for p in plat_str.split(",") if p.strip() in COLOURS]
    exclude_kws  = [kw.strip() for kw in exclude_str.split(",") if kw.strip()]

    if not query or not platforms:
        print("Usage: --watch --query QUERY [--platforms p1,p2] [--interval N] [--name NAME] [--exclude kw1,kw2]")
        return

    global POLL_INTERVAL
    POLL_INTERVAL = interval_min * 60

    print(f"[WATCH] '{query}'  platforms={platforms}  interval={interval_min}m", flush=True)

    scrapers: dict = {}
    _ensure_scrapers(platforms, scrapers)

    try:
        poll(query, platforms, scrapers, name=name, exclude_keywords=exclude_kws)
    finally:
        for name in ("ebay", "vinted", "depop", "mercari_jp", "rakuten_jp"):
            if name in scrapers:
                scrapers[name].close()
        _stop_pw()


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    # ── GUI subprocess watch mode ──
    if "--watch" in sys.argv:
        _run_watch_cli()
        return

    # ── Scrape eBay sold data (non-interactive) ──
    if "--scrape-sold" in sys.argv:
        args = sys.argv[1:]
        def _arg(flag):
            try:
                return args[args.index(flag) + 1]
            except (ValueError, IndexError):
                return None
        query       = _arg("--query") or ""
        pages       = int(_arg("--pages") or "5")
        min_records = int(_arg("--min-records") or "0")
        if not query:
            print("Usage: --scrape-sold --query QUERY [--pages N] [--min-records N]")
            return
        scrape_ebay_sold(query, max_pages=pages, min_records=min_records)
        return

    # ── Analyse mode: python3 listing_watcher.py --analyse ──
    if "--analyse" in sys.argv:
        print("\n  ╔══════════════════════════════╗")
        print("  ║   Listing Analyser            ║")
        print("  ╚══════════════════════════════╝\n")
        query = input("  Search query (used to find CSV): ").strip()
        if not query:
            print("  No query entered.")
            return
        print("\n  Platform:")
        print("  [1] Vinted  (default)")
        print("  [2] Depop")
        print("  [3] eBay\n")
        p_choice = input("  Choice [1]: ").strip() or "1"
        platform = {"1": "vinted", "2": "depop", "3": "ebay"}.get(p_choice, "vinted")
        analyse_listings(query, platform)
        return

    print("\n  ╔══════════════════════════════════════════╗")
    print("  ║   New Listing Watcher — eBay/Vinted/Depop ║")
    print("  ╚══════════════════════════════════════════╝\n")

    scrapers: dict = {}

    while True:
        searches = load_saved_searches()

        print("\n  ── Main Menu ──────────────────────────────")
        print("  [1] Start new watch")
        print("  [2] Run a saved search")
        print("  [3] Run ALL saved searches")
        print("  [4] Save current / add a search")
        print("  [5] Delete a saved search")
        print("  [6] Analyse listings (cheapest first)")
        print("  [0] Quit\n")

        if searches:
            print(f"  Saved searches ({len(searches)}):")
            for i, s in enumerate(searches, 1):
                print(f"    [{i}] {s['name']:20}  {s['query']:30}"
                      f"  {', '.join(s['platforms'])}  every {s['interval_min']} min")
        else:
            print("  (No saved searches yet)")

        choice = input("\n  Choice: ").strip()

        # ── 0. Quit ──────────────────────────────────────────────────────────
        if choice == "0":
            break

        # ── 1. Start new watch ───────────────────────────────────────────────
        elif choice == "1":
            query = input("\n  Search query: ").strip()
            if not query:
                continue
            _ebay_sold_flow(query)
            platforms = _platform_menu()
            interval  = input(f"\n  Poll interval in minutes [5]: ").strip()
            interval_min = int(interval) if interval.isdigit() else 5
            global POLL_INTERVAL
            POLL_INTERVAL = interval_min * 60

            print("\n  Initialising scrapers...")
            _ensure_scrapers(platforms, scrapers)

            try:
                poll(query, platforms, scrapers)
            except (KeyboardInterrupt, EOFError):
                print("\n\n  👋 Stopped.\n")

        # ── 2. Run a saved search ────────────────────────────────────────────
        elif choice == "2":
            if not searches:
                print("  No saved searches.")
                continue
            idx = input("  Enter number of saved search to run: ").strip()
            try:
                w = searches[int(idx) - 1]
            except (ValueError, IndexError):
                print("  Invalid number.")
                continue
            _ebay_sold_flow(w["query"])
            POLL_INTERVAL = w["interval_min"] * 60
            print("\n  Initialising scrapers...")
            _ensure_scrapers(w["platforms"], scrapers)
            try:
                poll(w["query"], w["platforms"], scrapers)
            except (KeyboardInterrupt, EOFError):
                print("\n\n  👋 Stopped.\n")

        # ── 3. Run ALL saved searches ────────────────────────────────────────
        elif choice == "3":
            if not searches:
                print("  No saved searches.")
                continue
            all_platforms = list({p for w in searches for p in w["platforms"]})
            print("\n  Initialising scrapers for all searches...")
            _ensure_scrapers(all_platforms, scrapers)
            try:
                multi_poll(searches, scrapers)
            except (KeyboardInterrupt, EOFError):
                print("\n\n  👋 Stopped.\n")

        # ── 4. Save / add a search ───────────────────────────────────────────
        elif choice == "4":
            name  = input("\n  Name for this search: ").strip()
            if not name:
                continue
            query = input("  Search query: ").strip()
            if not query:
                continue
            platforms = _platform_menu()
            interval  = input("\n  Poll interval in minutes [5]: ").strip()
            interval_min = int(interval) if interval.isdigit() else 5
            add_saved_search(name, query, platforms, interval_min)

        # ── 5. Delete a saved search ─────────────────────────────────────────
        elif choice == "5":
            if not searches:
                print("  No saved searches.")
                continue
            name = input("  Name to delete: ").strip()
            delete_saved_search(name)

        # ── 6. Analyse ────────────────────────────────────────────────────────
        elif choice == "6":
            query = input("\n  Search query (used to find CSV): ").strip()
            if not query:
                continue
            print("\n  Platform:")
            print("  [1] Vinted  (default)")
            print("  [2] Depop")
            print("  [3] eBay\n")
            p_choice = input("  Choice [1]: ").strip() or "1"
            platform = {"1": "vinted", "2": "depop", "3": "ebay"}.get(p_choice, "vinted")
            analyse_listings(query, platform)

    for name in ("vinted", "depop"):
        if name in scrapers:
            scrapers[name].close()
    _stop_pw()
    print("\n  👋 Goodbye.\n")


if __name__ == "__main__":
    main()
