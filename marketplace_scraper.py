#!/usr/bin/env python3
"""
Vinted & Depop — newly listed item scraper
Polls both platforms for new listings matching a search query.

Usage:
  python3 marketplace_scraper.py

Requirements:
  pip3 install requests
"""

import csv
import json
import os
import time
import random
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL = 300      # seconds between polls (5 min)
DATASETS_DIR  = Path("datasets")
DATASETS_DIR.mkdir(exist_ok=True)

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

FIELDNAMES = [
    "platform", "item_id", "title", "price", "currency",
    "condition", "brand", "size", "seller", "seller_items",
    "location", "url", "listed_at", "fetched_at", "search_query",
]


# ── CSV helpers ───────────────────────────────────────────────────────────────
def csv_path(query: str, platform: str) -> Path:
    slug = query.lower().replace(" ", "_")
    return DATASETS_DIR / f"{slug}_{platform}.csv"


def load_seen_ids(path: Path) -> set:
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["item_id"] for row in csv.DictReader(f)}


def save_rows(path: Path, rows: list[dict]) -> None:
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(rows)


# ── Vinted ────────────────────────────────────────────────────────────────────
class VintedScraper:
    BASE = "https://www.vinted.co.uk"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS_BASE)
        self._init_session()

    def _init_session(self):
        """Visit the homepage once to get the session cookie Vinted requires."""
        try:
            r = self.session.get(self.BASE, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"  ⚠️  Vinted session init failed: {e}")

    def fetch(self, query: str, per_page: int = 96) -> list[dict]:
        url = f"{self.BASE}/api/v2/catalog/items"
        params = {
            "search_text":   query,
            "order":         "newest_first",
            "per_page":      per_page,
        }
        try:
            r = self.session.get(url, params=params, timeout=15)
            if r.status_code == 401:
                print("  🔄 Vinted session expired — refreshing...")
                self._init_session()
                r = self.session.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ❌ Vinted fetch error: {e}")
            return []

        items = data.get("items", [])
        results = []
        for it in items:
            try:
                photo    = it.get("photo", {}) or {}
                user     = it.get("user", {}) or {}
                price    = it.get("price", {}) or {}
                results.append({
                    "platform":     "vinted",
                    "item_id":      str(it.get("id", "")),
                    "title":        it.get("title", "").strip(),
                    "price":        price.get("amount", it.get("price", "")),
                    "currency":     price.get("currency_code", "GBP"),
                    "condition":    it.get("status", ""),
                    "brand":        it.get("brand_title", ""),
                    "size":         it.get("size_title", ""),
                    "seller":       user.get("login", ""),
                    "seller_items": user.get("item_count", ""),
                    "location":     user.get("city", "") or it.get("city", ""),
                    "url":          f"{self.BASE}/items/{it.get('id')}-{it.get('slug', '')}",
                    "listed_at":    it.get("created_at_ts", ""),
                    "fetched_at":   datetime.now(timezone.utc).isoformat(),
                    "search_query": query,
                })
            except Exception:
                continue
        return results


# ── Depop ─────────────────────────────────────────────────────────────────────
class DepopScraper:
    BASE = "https://api.depop.com/api/v1"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            **HEADERS_BASE,
            "Accept":             "application/json",
            "Origin":             "https://www.depop.com",
            "Referer":            "https://www.depop.com/",
            "depop-client-type":  "web",
        })

    def fetch(self, query: str, limit: int = 48) -> list[dict]:
        url = f"{self.BASE}/search/products/"
        params = {
            "q":        query,
            "country":  "gb",
            "currency": "GBP",
            "sort_by":  "NewestFirst",
            "limit":    limit,
            "offset":   0,
        }
        try:
            r = self.session.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ❌ Depop fetch error: {e}")
            return []

        products = data.get("products", [])
        results  = []
        for p in products:
            try:
                seller   = p.get("seller", {}) or {}
                price_d  = p.get("price", {}) or {}
                slug     = p.get("slug", p.get("id", ""))
                s_name   = seller.get("username", "")
                results.append({
                    "platform":     "depop",
                    "item_id":      str(p.get("id", "")),
                    "title":        p.get("description", "").strip()[:120],
                    "price":        price_d.get("priceAmount",
                                    p.get("price", {}).get("amount", "")),
                    "currency":     price_d.get("currencyName", "GBP"),
                    "condition":    p.get("categoryDetails", {}).get("sizeValue", ""),
                    "brand":        p.get("brandName", ""),
                    "size":         p.get("sizeValue", ""),
                    "seller":       s_name,
                    "seller_items": "",
                    "location":     p.get("address", {}).get("country", {}).get("name", ""),
                    "url":          f"https://www.depop.com/products/{slug}/",
                    "listed_at":    p.get("createdAt", ""),
                    "fetched_at":   datetime.now(timezone.utc).isoformat(),
                    "search_query": query,
                })
            except Exception:
                continue
        return results


# ── Poll loop ─────────────────────────────────────────────────────────────────
def poll(query: str, platforms: list[str], scrapers: dict):
    seen = {p: load_seen_ids(csv_path(query, p)) for p in platforms}
    print(f"\n  Polling for: '{query}'  |  platforms: {', '.join(platforms)}")
    print(f"  Known IDs loaded — Vinted: {len(seen.get('vinted', set()))}"
          f"  Depop: {len(seen.get('depop', set()))}\n")

    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Checking...")

        for platform in platforms:
            scraper  = scrapers[platform]
            rows     = scraper.fetch(query)
            new_rows = [r for r in rows if r["item_id"] not in seen[platform]]

            if new_rows:
                save_rows(csv_path(query, platform), new_rows)
                for r in new_rows:
                    seen[platform].add(r["item_id"])
                print(f"  ✅ {platform.capitalize():8s}  +{len(new_rows)} new  "
                      f"(total seen: {len(seen[platform])})")
                for r in new_rows[:5]:
                    price = f"£{r['price']}" if r['price'] else "?"
                    print(f"     {price:>8}  {r['title'][:55]}")
                    print(f"             {r['url']}")
            else:
                print(f"  —  {platform.capitalize():8s}  no new listings")

            # Polite delay between platforms
            time.sleep(random.uniform(2, 4))

        print(f"  Sleeping {POLL_INTERVAL // 60} min...\n")
        time.sleep(POLL_INTERVAL)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    print("\n  ╔══════════════════════════════════════╗")
    print("  ║   Vinted & Depop  New Listing Watcher ║")
    print("  ╚══════════════════════════════════════╝\n")

    query = input("  Search query: ").strip()
    if not query:
        print("  No query entered. Exiting.")
        return

    print("\n  Platforms:")
    print("  [1] Vinted only")
    print("  [2] Depop only")
    print("  [3] Both\n")
    choice = input("  Choice [3]: ").strip() or "3"

    platforms = []
    if choice in ("1", "3"):
        platforms.append("vinted")
    if choice in ("2", "3"):
        platforms.append("depop")

    if not platforms:
        print("  Invalid choice.")
        return

    interval = input(f"\n  Poll interval in minutes [{POLL_INTERVAL // 60}]: ").strip()
    global POLL_INTERVAL
    if interval.isdigit():
        POLL_INTERVAL = int(interval) * 60

    print("\n  Initialising scrapers...")
    scrapers = {}
    if "vinted" in platforms:
        scrapers["vinted"] = VintedScraper()
        print("  ✅ Vinted ready")
    if "depop" in platforms:
        scrapers["depop"] = DepopScraper()
        print("  ✅ Depop ready")

    print("\n  Press Ctrl+C to stop.\n")
    try:
        poll(query, platforms, scrapers)
    except (KeyboardInterrupt, EOFError):
        print("\n\n  👋 Stopped.\n")


if __name__ == "__main__":
    main()
