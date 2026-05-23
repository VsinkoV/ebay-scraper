"""
eBay Price Tracker
Uses eBay Browse API to track active listings and alert on deals.
Integrates with sold_listings.csv from scraper.py to use REAL sold prices
as the discount baseline — alerts when a live listing is 50%+ below median sold price.
"""

import requests
import csv
import os
import base64
import json
import time
import threading
from datetime import datetime
from urllib.parse import quote
from collections import defaultdict
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from dateutil import parser as dateparser
import numpy as np
import re
from statistics import median, mean
from pathlib import Path

# ============================================================
#  CONFIG
# ============================================================
APP_ID           = "valeriys-scraping-PRD-69545edfa-2d76aa13"
DEV_ID           = "4265bb0a-ed64-4ef9-b9e4-c7a0753e2b3e"
CERT_ID          = "PRD-9545edfac2c8-d5c3-4a2a-a757-7788"
MAX_RESULTS      = 100
DATASETS_DIR     = "datasets"
CONFIG_FILE      = "trackers.json"
DISCORD_WEBHOOK  = "https://discord.com/api/webhooks/1478702130100572210/14dADxhvzSmn4_bOxnNAOQG21bkqLDYUjice8aAImOifeZU7E7XArjL_hd_cSBjAlXkj"

# ── Deal threshold ──────────────────────────────────────────
# Discord alert fires when a live listing is this % below the sold median.
# e.g. 0.50 = alert if price is 50% or more below median sold price
DEAL_THRESHOLD_PCT = 0.50

# Only flag live listings listed within this many days
RECENT_DAYS = 5

# Re-scrape sold data if it is older than this many days
SOLD_DATA_MAX_AGE_DAYS = 7

# Deal price window:
#   Upper bound — alert if price ≤ sold_median × (1 - DEAL_THRESHOLD_PCT)  e.g. 50% below
#   Lower bound — ignore if price < sold_median × DEAL_MIN_PCT              e.g. below 25% of median (junk filter)
DEAL_MIN_PCT = 0.25

# Titles containing any of these words (case-insensitive) are excluded from results
NEGATIVE_KEYWORDS = [
    "phone case", "keyring", "key ring", "wallet", "purse", "bag", "clutch",
    "book", "dvd", "poster", "print", "card", "gift", "voucher", "pattern",
    "sewing", "repair", "parts", "dummy", "display",
]

# Fixed price overrides per tracker (optional — leave empty to use sold median)
NOTIFY_PRICES = {}

# Max price for leather jacket Discord alerts (safety cap)
LEATHER_MAX_PRICE = 150.00

# How many cheapest active listings to use as threshold baseline
# (used only when no sold data is available)
NTH_CHEAPEST = 5

# Country filter
EU_COUNTRIES      = {"GB","DE","FR","IT","ES","NL","BE","AT","PL","SE","DK","FI","NO","PT","IE"}
TRACKER_COUNTRIES = {}
DEFAULT_COUNTRIES = None
# ============================================================


# ── Sold price baseline (from scraper.py output) ─────────────

def load_sold_median(query: str) -> tuple[float | None, int]:
    """
    Read the sold CSV for this query (produced by scraper.py) and return
    (median_sold_price, sample_size).
    Filters out placeholder rows and outliers (IQR method).
    If the file doesn't exist or has no data, returns (None, 0).
    """
    path = Path(sold_csv_path(query))
    if not path.exists():
        return None, 0

    prices = []
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if "Shop on eBay" in row.get("title", ""):
                continue
            raw = row.get("price", "").replace("£", "").replace("$", "").replace(",", "").strip()
            try:
                prices.append(float(raw))
            except ValueError:
                continue

    if not prices:
        return None, 0

    # Remove outliers via IQR
    q1, q3 = np.percentile(prices, [25, 75])
    iqr = q3 - q1
    prices = [p for p in prices if q1 - 1.5 * iqr <= p <= q3 + 1.5 * iqr]

    if not prices:
        return None, 0

    return float(np.median(prices)), len(prices)


def get_deal_threshold(query: str) -> tuple[float | None, str]:
    """
    Return (threshold_price, description_label).
    Priority:
      1. Fixed override in NOTIFY_PRICES
      2. 50% below median sold price from query-specific sold CSV
      3. None (no threshold — won't alert)
    """
    if query in NOTIFY_PRICES:
        t = NOTIFY_PRICES[query]
        return t, f"fixed override £{t:.2f}"

    sold_median, n = load_sold_median(query)
    if sold_median is not None:
        t = sold_median * (1 - DEAL_THRESHOLD_PCT)
        pct = int(DEAL_THRESHOLD_PCT * 100)
        return t, f"{pct}% below sold median £{sold_median:.2f} (n={n}) → £{t:.2f}"

    return None, f"no threshold available (run scraper.py for '{query}' first)"


# ── Helpers ──────────────────────────────────────────────────

def slug(text):
    return text.lower().replace(" ", "_").replace("/", "_")


def sold_csv_path(query: str) -> str:
    """Returns the path to the sold listings CSV for a given query.
    Matches the filename produced by scraper.py for the same query."""
    os.makedirs(DATASETS_DIR, exist_ok=True)
    return os.path.join(DATASETS_DIR, f"{slug(query)}_sold.csv")


def dataset_path(query):
    os.makedirs(DATASETS_DIR, exist_ok=True)
    return os.path.join(DATASETS_DIR, f"{slug(query)}.csv")


def load_trackers():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {"queries": data, "model_keywords": {}}
            return data
    return {"queries": [], "model_keywords": {}}


def save_trackers(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_queries(data):
    return data.get("queries", [])


def get_model_keywords(data, query):
    stored = data.get("model_keywords", {}).get(query, [])
    for brand, keywords in BRAND_MODEL_KEYWORDS.items():
        if brand in query.lower():
            stored = list(set(stored + keywords))
    return stored


# ── eBay API ──────────────────────────────────────────────────

def get_oauth_token():
    credentials = f"{quote(APP_ID)}:{quote(CERT_ID)}"
    encoded     = base64.b64encode(credentials.encode()).decode()
    response    = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Content-Type":  "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded}"
        },
        data={
            "grant_type": "client_credentials",
            "scope":      "https://api.ebay.com/oauth/api_scope"
        }
    )
    response.raise_for_status()
    return response.json()["access_token"]


def fetch_listings(token, query):
    headers = {
        "Authorization":           f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        "Content-Type":            "application/json"
    }
    params = {
        "q":      query,
        "limit":  MAX_RESULTS,
        "sort":   "newlyListed",
        "filter": "itemLocationCountry:GB|DE|FR|IT|ES|NL|BE|AT|PL|SE|DK|FI|NO|PT|IE"
    }
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        response = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers,
            params=params
        )
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 1800))
            print(f"\n  ⚠️  Rate limited by eBay API (attempt {attempt}/{max_retries}).")
            if attempt == max_retries:
                print(f"  ❌ Max retries reached. Try again in {retry_after // 60} min.\n")
                return None
            print(f"  ⏳ Waiting {retry_after}s before retry...", end="", flush=True)
            for remaining in range(retry_after, 0, -1):
                print(f"\r  ⏳ Retrying in {remaining}s...   ", end="", flush=True)
                time.sleep(1)
            print(f"\r  🔄 Retrying now (attempt {attempt + 1}/{max_retries})...      ")
            continue
        response.raise_for_status()
        return response.json().get("itemSummaries", [])
    return None


# ── Brand / category keywords ─────────────────────────────────

BRAND_MODEL_KEYWORDS = {}

GENERIC_LEATHER_KEYWORDS   = ["leather", "cowhide", "lambskin", "sheepskin",
                               "shearling", "nappa", "goatskin", "hide",
                               "biker", "moto", "racer", "aviator", "distressed"]
GENERIC_SUEDE_KEYWORDS     = ["suede", "nubuck", "brushed leather", "velvet leather"]
GENERIC_DENIM_KEYWORDS     = ["denim", "jean jacket", "trucker"]
GENERIC_BOMBER_KEYWORDS    = ["bomber", "flight jacket", "ma-1", "harrington"]
GENERIC_BLAZER_KEYWORDS    = ["blazer", "tailored", "suit jacket", "sport coat"]
GENERIC_COAT_KEYWORDS      = ["coat", "overcoat", "trench", "parka", "puffer",
                               "padded", "wool", "quilted", "peacoat", "mac "]
GENERIC_OVERSHIRT_KEYWORDS = ["overshirt", "shacket", "shirt jacket", "checked jacket"]


def categorise_item(title, query="", extra_keywords=None):
    t = title.lower() if title else ""
    brand_leather_extras = extra_keywords or []
    for brand, models in BRAND_MODEL_KEYWORDS.items():
        if brand in query.lower():
            brand_leather_extras = list(set(brand_leather_extras + models))
    leather_keywords = GENERIC_LEATHER_KEYWORDS + brand_leather_extras
    if any(k in t for k in leather_keywords):            return "Leather"
    elif any(k in t for k in GENERIC_SUEDE_KEYWORDS):   return "Suede"
    elif any(k in t for k in GENERIC_DENIM_KEYWORDS):   return "Denim"
    elif any(k in t for k in GENERIC_BOMBER_KEYWORDS):  return "Bomber"
    elif any(k in t for k in GENERIC_BLAZER_KEYWORDS):  return "Blazer"
    elif any(k in t for k in GENERIC_COAT_KEYWORDS):    return "Coat"
    elif any(k in t for k in GENERIC_OVERSHIRT_KEYWORDS): return "Overshirt"
    else:                                                 return "Other"


def categorise_jacket(title, query="", extra_keywords=None):
    return categorise_item(title, query, extra_keywords)


def parse_items(raw_items, query, extra_keywords=None):
    items = []
    for item in raw_items:
        try:
            price = float(item["price"]["value"])
        except (KeyError, ValueError):
            continue
        title = item.get("title", "")

        size = "Unknown"
        size_match = re.search(
            r'(UK\s?\d+|US\s?\d+|EU\s?\d+|XXS|XS|S|M|L|XL|XXL|XXXL|'
            r'Size\s?\d+|sz\s?\d+|\d{1,2}/\d{1,2})',
            title, re.IGNORECASE
        )
        if size_match:
            size = size_match.group(0).strip()

        shipping = "Unknown"
        shipping_opts = item.get("shippingOptions", [])
        if shipping_opts:
            ship_val = shipping_opts[0].get("shippingCost", {}).get("value", "")
            if ship_val:
                shipping = f"£{float(ship_val):.2f}" if float(ship_val) > 0 else "Free"

        seller          = item.get("seller", {})
        seller_name     = seller.get("username", "Unknown")
        seller_feedback = seller.get("feedbackPercentage", "")

        location = item.get("itemLocation", {})
        loc_str  = f"{location.get('city','')}, {location.get('country','')}".strip(", ")

        title_lower = title.lower()
        query_words = [w.lower() for w in query.split() if len(w) > 2]
        if query_words and not all(w in title_lower for w in query_words):
            continue

        country           = item.get("itemLocation", {}).get("country", "")
        allowed_countries = TRACKER_COUNTRIES.get(query, DEFAULT_COUNTRIES)
        if allowed_countries and country and country not in allowed_countries:
            continue

        best_offer = "BEST_OFFER" in item.get("buyingOptions", [])

        items.append({
            "item_id":         item.get("itemId"),
            "title":           title,
            "category":        categorise_jacket(title, query, extra_keywords or []),
            "size":            size,
            "price":           price,
            "currency":        item["price"].get("currency", "GBP"),
            "shipping":        shipping,
            "best_offer":      best_offer,
            "end_time":        item.get("itemEndDate", ""),
            "listed_at":       item.get("itemCreationDate", ""),
            "condition":       item.get("condition", "Unknown"),
            "short_desc":      item.get("shortDescription", "")[:200],
            "seller":          seller_name,
            "seller_feedback": seller_feedback,
            "location":        loc_str,
            "listing_type":    "FIXED_PRICE",
            "url":             item.get("itemWebUrl", ""),
            "fetched_at":      datetime.now().isoformat(),
            "search_query":    query
        })
    return items


# ── Dataset ───────────────────────────────────────────────────

def load_existing_ids(query):
    path = dataset_path(query)
    if not os.path.exists(path):
        return set()
    ids = set()
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            item_id = row.get("item_id", "").strip()
            if item_id:
                ids.add(item_id)
    return ids


FIELDNAMES = ["item_id", "title", "category", "size", "price", "currency",
              "shipping", "best_offer", "end_time", "listed_at", "condition",
              "short_desc", "seller", "seller_feedback", "location",
              "listing_type", "url", "fetched_at", "search_query",
              "last_seen", "times_seen", "presumed_sold", "sold_at"]


def save_to_csv(items, query):
    if not items:
        return 0
    path     = dataset_path(query)
    exists   = os.path.exists(path)
    existing = load_existing_ids(query)
    unique   = [i for i in items if i.get("item_id") not in existing]
    if not unique:
        return 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(unique)
    return len(unique)


def load_dataset(query):
    path = dataset_path(query)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            try:
                row["price"] = float(row["price"])
            except (ValueError, KeyError):
                pass
            row.setdefault("listed_at", "")
            row.setdefault("category", "Other")
            row.setdefault("size", "Unknown")
            row.setdefault("shipping", "Unknown")
            row.setdefault("best_offer", False)
            row.setdefault("short_desc", "")
            row.setdefault("seller", "Unknown")
            row.setdefault("seller_feedback", "")
            row.setdefault("location", "")
            row.setdefault("last_seen", "")
            row.setdefault("times_seen", "1")
            row.setdefault("presumed_sold", "False")
            row.setdefault("sold_at", "")
            rows.append(row)
    return rows


# ── Discord ───────────────────────────────────────────────────

def send_discord_notification(item, sold_median, query, threshold):
    if not DISCORD_WEBHOOK:
        return
    price    = item["price"]
    saving   = sold_median - price
    pct_off  = (saving / sold_median) * 100

    payload = {
        "embeds": [{
            "title":       f"🚨 DEAL — {int(pct_off)}% BELOW SOLD MEDIAN",
            "description": f"**{item.get('title', '')}**",
            "color":       15158332,
            "fields": [
                {"name": "💰 Listed Price",    "value": f"£{price:.2f}",                              "inline": True},
                {"name": "📊 Sold Median",     "value": f"£{sold_median:.2f}",                        "inline": True},
                {"name": "📉 You Save",        "value": f"£{saving:.2f} ({pct_off:.0f}% cheaper!)",   "inline": True},
                {"name": "🔔 Alert Threshold", "value": f"£{threshold:.2f}",                          "inline": True},
                {"name": "📏 Size",            "value": item.get("size", "?"),                        "inline": True},
                {"name": "🏷 Condition",       "value": item.get("condition", "?"),                   "inline": True},
                {"name": "🚚 Shipping",        "value": item.get("shipping", "?"),                    "inline": True},
                {"name": "📍 Location",        "value": item.get("location", "?"),                    "inline": True},
                {"name": "👤 Seller",          "value": f"{item.get('seller','')} ({item.get('seller_feedback','')}%)", "inline": True},
                {"name": "🤝 Best Offer",      "value": "✅ Yes" if item.get("best_offer") else "❌ No", "inline": True},
                {"name": "🔗 Link",            "value": item.get("url", ""),                          "inline": False},
            ],
            "footer": {"text": f"eBay Tracker • {datetime.now().strftime('%d %b %Y %H:%M')}  |  Search: {query}"}
        }]
    }
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload)
        if r.status_code == 204:
            print(f"   🔔 Discord alert sent!")
        else:
            print(f"   ⚠️  Discord error: {r.status_code}")
    except Exception as e:
        print(f"   ⚠️  Discord error: {e}")


def send_top5_to_discord(rows, query):
    if not DISCORD_WEBHOOK:
        print("  ⚠️  No webhook set.")
        return
    if len(rows) < 5:
        print("  ⚠️  Not enough data yet.")
        return

    sold_median, n = load_sold_median(query)
    if sold_median:
        baseline     = sold_median
        baseline_lbl = f"sold median £{sold_median:.2f} (n={n})"
    else:
        baseline     = median([r["price"] for r in rows])
        baseline_lbl = f"active median £{baseline:.2f}"

    top5   = sorted(rows, key=lambda r: r["price"])[:5]
    fields = []
    for i, item in enumerate(top5, 1):
        price   = item["price"]
        saving  = baseline - price
        pct_off = (saving / baseline) * 100
        title   = item.get("title", "")[:50]
        recency = _recency(item)
        fields.append({
            "name":  f"#{i} — £{price:.2f} ({pct_off:.0f}% below {baseline_lbl})",
            "value": f"**{title}**\n📏 {item.get('size','?')} | 🏷 {item.get('condition','')} | 🚚 {item.get('shipping','')} | 🕐 {recency}\n🔗 {item.get('url','')}",
            "inline": False
        })

    payload = {
        "embeds": [{
            "title":       f"💰 Top 5 Best Deals — {query}",
            "description": f"Baseline: **{baseline_lbl}** | {len(rows)} listings tracked",
            "color":       3066993,
            "fields":      fields,
            "footer":      {"text": f"eBay Tracker • {datetime.now().strftime('%d %b %Y %H:%M')}"}
        }]
    }
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload)
        print("  🔔 Top 5 sent to Discord!" if r.status_code == 204 else f"  ⚠️ Discord error: {r.status_code}")
    except Exception as e:
        print(f"  ⚠️  Discord error: {e}")


def send_top5_recent_to_discord(rows, query):
    if not DISCORD_WEBHOOK:
        print("  ⚠️  No webhook set.")
        return
    if len(rows) < 5:
        print("  ⚠️  Not enough data yet.")
        return

    sold_median, n = load_sold_median(query)
    baseline       = sold_median or median([r["price"] for r in rows])
    top5           = sorted(rows, key=lambda r: _recency_dt(r), reverse=True)[:5]
    fields = []
    for i, item in enumerate(top5, 1):
        price   = item["price"]
        saving  = baseline - price
        pct_off = (saving / baseline) * 100
        vs_lbl  = f"£{saving:.0f} cheaper" if saving > 0 else f"£{abs(saving):.0f} pricier"
        fields.append({
            "name":  f"#{i} — Listed {_recency(item)}",
            "value": f"**{item.get('title','')[:50]}**\n💰 £{price:.2f} ({vs_lbl} | {pct_off:.0f}% off baseline)\n📏 {item.get('size','?')} | 🏷 {item.get('condition','')} | 🚚 {item.get('shipping','')}\n🔗 {item.get('url','')}",
            "inline": False
        })

    payload = {
        "embeds": [{
            "title":       f"🕐 Top 5 Most Recently Listed — {query}",
            "description": f"Sold median: **£{baseline:.2f}** | {len(rows)} listings tracked",
            "color":       3447003,
            "fields":      fields,
            "footer":      {"text": f"eBay Tracker • {datetime.now().strftime('%d %b %Y %H:%M')}"}
        }]
    }
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload)
        print("  🔔 Top 5 recent sent to Discord!" if r.status_code == 204 else f"  ⚠️ Discord error: {r.status_code}")
    except Exception as e:
        print(f"  ⚠️  Discord error: {e}")


# ── Recency helpers ───────────────────────────────────────────

def _recency_dt(row):
    for field in ["listed_at", "fetched_at"]:
        val = row.get(field)
        if val:
            try:
                return dateparser.parse(val)
            except Exception:
                pass
    return datetime.min


def _recency(row):
    dt = _recency_dt(row)
    if dt == datetime.min:
        return "Unknown"
    try:
        delta = datetime.now(dt.tzinfo) - dt
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds()/60)}m ago"
        elif hours < 24:
            return f"{int(hours)}h ago"
        else:
            return f"{int(hours/24)}d ago"
    except Exception:
        return "Unknown"


# ── Analytics ─────────────────────────────────────────────────

def print_summary(rows, query):
    if not rows:
        print("   No data yet.")
        return
    prices = [r["price"] for r in rows]
    med    = median(prices)
    avg    = mean(prices)

    sold_median, n = load_sold_median(query)
    threshold, lbl = get_deal_threshold(query)

    print(f"\n📊 [{query}] — {len(prices)} listings tracked")
    print(f"   Active median : £{med:.2f}  |  Active mean: £{avg:.2f}  |  Low: £{min(prices):.2f}  |  High: £{max(prices):.2f}")
    if sold_median:
        print(f"   Sold median   : £{sold_median:.2f}  (from {n} sold listings in {sold_csv_path(query)})")
    print(f"   Deal threshold: {lbl}")

    from collections import Counter
    cats = Counter(r.get("category", "Other") for r in rows)
    print(f"\n🧥 Categories:")
    for cat, count in sorted(cats.items()):
        cat_prices = [r["price"] for r in rows if r.get("category") == cat]
        cat_med    = median(cat_prices) if cat_prices else 0
        cat_avg    = mean(cat_prices) if cat_prices else 0
        print(f"   {cat:<12} : {count:>4} listed  |  median £{cat_med:.2f}  |  mean £{cat_avg:.2f}")


def check_deals(new_items, all_rows, query):
    """Alert on new listings that are >= DEAL_THRESHOLD_PCT below the sold median."""
    threshold, lbl = get_deal_threshold(query)

    sold_median, _ = load_sold_median(query)
    # Fallback: use active median if no sold data
    baseline = sold_median or (median([r["price"] for r in all_rows]) if all_rows else None)

    if threshold is None or baseline is None:
        print(f"⚠️  No deal threshold available — run scraper.py first to build sold price data.")
        return

    pct = int(DEAL_THRESHOLD_PCT * 100)
    print(f"\n🔍 Deals ({pct}% below {lbl}):\n")
    deals = [i for i in new_items if i["price"] <= threshold]
    if not deals:
        print(f"   😴 No deals found this run.")
    else:
        for item in deals:
            saving  = baseline - item["price"]
            pct_off = (saving / baseline) * 100
            print(f"   🚨 DEAL: {item['title'][:60]}")
            print(f"      Price   : £{item['price']:.2f}  ({pct_off:.0f}% below sold median £{baseline:.2f})")
            print(f"      Saving  : £{saving:.2f}")
            print(f"      {item['url']}\n")
            send_discord_notification(item, baseline, query, threshold)
        print(f"   ✅ {len(deals)} deal(s) found!")


def print_cheapest(all_rows, query):
    if len(all_rows) < 5:
        return
    sold_median, n = load_sold_median(query)
    baseline       = sold_median or median([r["price"] for r in all_rows])
    baseline_lbl   = f"sold median £{baseline:.2f}" if sold_median else f"active median £{baseline:.2f}"

    top5 = sorted(all_rows, key=lambda r: r["price"])[:5]
    print(f"\n💰 5 Cheapest Active Listings (vs {baseline_lbl}):")
    print(f"   {'#':<3} {'Price':<10} {'Below Baseline':<18} {'Listed':<12} Title")
    print(f"   {'-'*3} {'-'*10} {'-'*18} {'-'*12} {'-'*40}")
    for i, item in enumerate(top5, 1):
        price   = item["price"]
        saving  = baseline - price
        pct_off = (saving / baseline) * 100
        title   = item.get("title", "")[:50]
        print(f"   {i:<3} £{price:<9.2f} £{saving:.2f} ({pct_off:.0f}%)     {_recency(item):<12} {title}")
        print(f"       📏 {item.get('size','?')}  |  {item.get('condition','')}  |  {item.get('shipping','')}  |  {item.get('location','')}")
        print(f"       🔗 {item.get('url','')}")
        print()


def update_sold_status(current_item_ids, query):
    path = dataset_path(query)
    if not os.path.exists(path):
        return []
    rows       = load_dataset(query)
    now        = datetime.now().isoformat()
    newly_sold = []
    updated    = []
    for row in rows:
        item_id = row.get("item_id", "")
        if row.get("presumed_sold") == "True":
            updated.append(row)
            continue
        if item_id in current_item_ids:
            row["last_seen"]  = now
            row["times_seen"] = str(int(row.get("times_seen", 1)) + 1)
        else:
            last_seen = row.get("last_seen") or row.get("fetched_at", "")
            if last_seen:
                try:
                    last_dt    = dateparser.parse(last_seen)
                    hours_gone = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds() / 3600
                    if hours_gone >= 1:
                        row["presumed_sold"] = "True"
                        row["sold_at"]       = now
                        newly_sold.append(row)
                except Exception:
                    pass
        updated.append(row)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(updated)
    return newly_sold


def print_sold_analysis(query):
    rows = load_dataset(query)
    sold = [r for r in rows if r.get("presumed_sold") == "True"]
    live = [r for r in rows if r.get("presumed_sold") != "True"]
    print(f"\n📦 Sold Analysis — {query}")
    print(f"   Total tracked : {len(rows)}")
    print(f"   Presumed sold : {len(sold)}")
    print(f"   Still live    : {len(live)}")
    if sold:
        sold_prices = [r["price"] for r in sold]
        print(f"\n   Sold price stats:")
        print(f"   Median sold : £{median(sold_prices):.2f}")
        print(f"   Mean sold   : £{mean(sold_prices):.2f}")
        print(f"   Lowest sold : £{min(sold_prices):.2f}")
        print(f"   Highest sold: £{max(sold_prices):.2f}")
        print(f"\n   🕐 Recently sold:")
        for item in sorted(sold, key=lambda r: r.get("sold_at",""), reverse=True)[:10]:
            speed = "Unknown"
            if item.get("listed_at") and item.get("sold_at"):
                try:
                    listed = dateparser.parse(item["listed_at"])
                    sold_t = dateparser.parse(item["sold_at"])
                    hours  = (sold_t - listed).total_seconds() / 3600
                    speed  = f"{int(hours)}h" if hours < 24 else f"{int(hours/24)}d"
                except Exception:
                    pass
            print(f"   💸 £{item['price']:.2f} — {item.get('title','')[:45]}")
            print(f"      Size: {item.get('size','?')} | Sold in: {speed} | {item.get('condition','')}")


def print_ranked(all_rows, query):
    if not all_rows:
        print("   No data yet.")
        return
    sold_median, n = load_sold_median(query)
    baseline       = sold_median or median([r["price"] for r in all_rows])
    baseline_lbl   = f"sold median £{baseline:.2f}" if sold_median else f"active median £{baseline:.2f}"
    sorted_rows    = sorted(all_rows, key=lambda r: r["price"])
    print(f"\n📋 {len(sorted_rows)} listings ranked by price (baseline: {baseline_lbl}):")
    print(f"\n   {'#':<4} {'Price':<10} {'vs Baseline':<22} {'Listed':<12} Title")
    print(f"   {'-'*4} {'-'*10} {'-'*22} {'-'*12} {'-'*45}")
    for i, item in enumerate(sorted_rows, 1):
        price  = item["price"]
        diff   = baseline - price
        pct    = (diff / baseline) * 100
        marker = "🟢" if diff > 0 else ("🔴" if diff < 0 else "🟡")
        vs     = (f"£{diff:.0f} ({pct:.0f}% cheaper)" if diff > 0
                  else f"£{abs(diff):.0f} ({abs(pct):.0f}% pricier)")
        print(f"   {marker} {i:<3} £{price:<9.2f} {vs:<25} {_recency(item):<10} {item.get('title','')[:50]}")
        print(f"         📏 {item.get('size','?')}  |  {item.get('condition','')}  |  🚚 {item.get('shipping','')}{'  ✅BO' if item.get('best_offer') else ''}")
        print(f"         🔗 {item.get('url','')}")


def generate_charts(rows, query):
    if len(rows) < 5:
        print("⚠️  Need more data for charts.")
        return
    prices = [r["price"] for r in rows]
    sold_median, n = load_sold_median(query)

    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle(f"eBay Tracker — {query}", fontsize=14, fontweight="bold")

    # Price distribution
    ax1 = axes[0]
    ax1.hist(prices, bins=20, color="#4F46E5", edgecolor="white", alpha=0.85)
    med = median(prices)
    ax1.axvline(med, color="#EF4444", linestyle="--", linewidth=2, label=f"Active median £{med:.2f}")
    if sold_median:
        ax1.axvline(sold_median, color="#F59E0B", linestyle="--", linewidth=2,
                    label=f"Sold median £{sold_median:.2f} (n={n})")
        threshold = sold_median * (1 - DEAL_THRESHOLD_PCT)
        ax1.axvline(threshold, color="#10B981", linestyle=":", linewidth=2,
                    label=f"Deal threshold £{threshold:.2f}")
    ax1.set_title("Price Distribution", fontweight="bold")
    ax1.set_xlabel("Price (£)")
    ax1.set_ylabel("Listings")
    ax1.legend(fontsize=8)
    ax1.grid(axis="y", alpha=0.3)

    # Category breakdown
    ax2 = axes[1]
    from collections import Counter
    all_cats   = ["Leather","Suede","Denim","Bomber","Blazer","Coat","Overshirt","Other"]
    cat_colors = ["#DC2626","#92400E","#1D4ED8","#065F46","#7C3AED","#B45309","#0E7490","#6B7280"]
    cat_counts = [sum(1 for r in rows if r.get("category") == c) for c in all_cats]
    active     = [(c, n, col) for c, n, col in zip(all_cats, cat_counts, cat_colors) if n > 0]
    if active:
        a_cats, a_counts, a_cols = zip(*active)
        bars = ax2.bar(a_cats, a_counts, color=a_cols, edgecolor="white", alpha=0.85)
        ax2.bar_label(bars, padding=3, fontsize=9)
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha="right")
    ax2.set_title("Listings by Category", fontweight="bold")
    ax2.set_xlabel("Category")
    ax2.set_ylabel("Count")
    ax2.grid(axis="y", alpha=0.3)

    # Price over time
    ax3 = axes[2]
    dated = []
    for r in rows:
        if r.get("fetched_at"):
            try:
                dated.append((dateparser.parse(r["fetched_at"]), r["price"]))
            except Exception:
                pass
    if dated:
        dated.sort()
        dates_p, prices_p = zip(*dated)
        ax3.scatter(dates_p, prices_p, color="#F59E0B", alpha=0.5, s=20)
        if len(prices_p) >= 5:
            window      = max(5, len(prices_p) // 10)
            rolling_avg = np.convolve(prices_p, np.ones(window)/window, mode="valid")
            ax3.plot(list(dates_p)[window-1:], rolling_avg, color="#EF4444",
                     linewidth=2, label="Rolling avg")
            ax3.legend()
        ax3.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        plt.setp(ax3.xaxis.get_majorticklabels(), rotation=45, ha="right")
    ax3.set_title("Price Over Time", fontweight="bold")
    ax3.set_xlabel("Date")
    ax3.set_ylabel("Price (£)")
    ax3.grid(alpha=0.3)

    plt.tight_layout()
    chart_file = os.path.join(DATASETS_DIR, f"{slug(query)}_chart.png")
    plt.savefig(chart_file, dpi=150, bbox_inches="tight")
    plt.show()
    print(f"📈 Chart saved to {chart_file}")


# ── Core tracker run ──────────────────────────────────────────

def run_tracker(query, token, extra_keywords=None):
    print(f"\n{'='*60}")
    print(f"  Running tracker: {query}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    raw = fetch_listings(token, query)
    if raw is None:
        return
    items = parse_items(raw, query, extra_keywords or [])
    print(f"📦 Fetched {len(items)} listings")

    current_ids = {i["item_id"] for i in items}
    newly_sold  = update_sold_status(current_ids, query)
    if newly_sold:
        print(f"💸 {len(newly_sold)} item(s) presumed sold since last run:")
        for s in newly_sold[:3]:
            print(f"   £{s['price']:.2f} — {s.get('title','')[:50]}")

    existing = load_existing_ids(query)
    new      = [i for i in items if i["item_id"] not in existing]
    for i in new:
        i["last_seen"]     = i["fetched_at"]
        i["times_seen"]    = "1"
        i["presumed_sold"] = "False"
        i["sold_at"]       = ""
    print(f"✨ {len(new)} new listings")

    all_rows = load_dataset(query)
    print_summary(all_rows, query)
    check_deals(new, all_rows, query)

    saved = save_to_csv(new, query)
    print(f"\n💾 Saved {saved} records → {dataset_path(query)}")

    all_rows_updated = load_dataset(query)
    print_cheapest(all_rows_updated, query)


# ── Scraper integration ───────────────────────────────────────

def _load_scraper_module():
    """Dynamically load scraper.py from the same directory as this file."""
    import importlib.util
    scraper_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper.py")
    spec = importlib.util.spec_from_file_location("scraper", scraper_path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sold_data_age_days(query: str) -> float | None:
    """Return age in days of the sold CSV for this query, or None if it doesn't exist."""
    path = sold_csv_path(query)
    if not os.path.exists(path):
        return None
    return (time.time() - os.path.getmtime(path)) / 86400


def run_sold_scraper(query: str) -> bool:
    """
    Open a browser via scraper.py, scrape sold eBay listings for `query`,
    and save the result to the query-specific sold CSV.
    Returns True on success.
    """
    print(f"\n  🌐 Opening browser to scrape sold listings for: '{query}'")
    print(f"  (Solve any CAPTCHA that appears, then return here)\n")
    try:
        scraper = _load_scraper_module()
        listings = scraper.scrape(query, max_pages=scraper.MAX_PAGES)
        if not listings:
            print("  ⚠️  Scraper returned no listings.")
            return False
        output = sold_csv_path(query)
        scraper.save_to_csv(listings, output)
        print(f"  ✅ Scraped {len(listings)} sold listings → {output}")
        return True
    except Exception as e:
        print(f"  ❌ Scraper error: {e}")
        return False


def _send_no_deals_fallback(item, sold_median, query, rank):
    """Send a Discord embed for a top-cheapest listing when no deals are found."""
    if not DISCORD_WEBHOOK:
        return
    price   = item["price"]
    saving  = sold_median - price
    pct_off = (saving / sold_median) * 100
    payload = {
        "embeds": [{
            "title":       f"📊 No deals — #{rank} Cheapest Listed (last {RECENT_DAYS}d)",
            "description": f"**{item.get('title', '')}**",
            "color":       3447003,  # blue
            "fields": [
                {"name": "💰 Listed Price", "value": f"£{price:.2f}",                            "inline": True},
                {"name": "📊 Sold Median",  "value": f"£{sold_median:.2f}",                      "inline": True},
                {"name": "📉 vs Median",    "value": f"£{saving:.2f} ({pct_off:.0f}% cheaper)",  "inline": True},
                {"name": "📏 Size",         "value": item.get("size", "?"),                       "inline": True},
                {"name": "🏷 Condition",    "value": item.get("condition", "?"),                  "inline": True},
                {"name": "🚚 Shipping",     "value": item.get("shipping", "?"),                   "inline": True},
                {"name": "👤 Seller",       "value": f"{item.get('seller','')} ({item.get('seller_feedback','')}%)", "inline": True},
                {"name": "🤝 Best Offer",   "value": "✅ Yes" if item.get("best_offer") else "❌ No", "inline": True},
                {"name": "🔗 Link",         "value": item.get("url", ""),                         "inline": False},
            ],
            "footer": {"text": f"eBay Tracker • {datetime.now().strftime('%d %b %Y %H:%M')}  |  Search: {query}"}
        }]
    }
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload)
        if r.status_code == 204:
            print(f"   🔔 Discord alert sent (#{rank})!")
        else:
            print(f"   ⚠️  Discord error: {r.status_code}")
    except Exception as e:
        print(f"   ⚠️  Discord error: {e}")


# ── Quick deal scan ───────────────────────────────────────────

def quick_deal_scan(query: str, token: str) -> None:
    """
    One-shot scan: fetch live listings for `query`, keep only those listed
    within RECENT_DAYS days, and flag any priced at ≤ 50% of the sold median.
    """
    print(f"\n{'='*60}")
    print(f"  Quick deal scan: {query}")
    print(f"  Recency filter : listed within last {RECENT_DAYS} days")
    print(f"  Deal threshold : {int(DEAL_THRESHOLD_PCT*100)}% below sold median")
    print(f"{'='*60}")

    # Check sold data — scrape if missing or stale
    age = sold_data_age_days(query)
    sold_median, n = load_sold_median(query)

    needs_scrape = sold_median is None or (age is not None and age > SOLD_DATA_MAX_AGE_DAYS)

    if needs_scrape:
        if sold_median is None:
            print(f"\n  ℹ️  No sold data found for '{query}'.")
        else:
            print(f"\n  ℹ️  Sold data is {age:.0f} days old (refreshes every {SOLD_DATA_MAX_AGE_DAYS} days).")

        ans = input("  Scrape sold listings now? A browser will open. [y/n]: ").strip().lower()
        if ans == "y":
            if run_sold_scraper(query):
                sold_median, n = load_sold_median(query)
            else:
                print("  ⚠️  Scraping failed — cannot continue without a baseline.")
                return
        elif sold_median is None:
            print("  ⚠️  No baseline available. Run scraper.py for this query first.")
            return
        # if they said 'n' but stale data exists, continue with what we have

    if sold_median is None:
        return

    threshold = sold_median * (1 - DEAL_THRESHOLD_PCT)
    print(f"\n  📊 Sold median  : £{sold_median:.2f}  (from {n} listings)")
    print(f"  🔔 Deal threshold: £{threshold:.2f}  ({int(DEAL_THRESHOLD_PCT*100)}% below median)")

    # Fetch live listings
    raw = fetch_listings(token, query)
    if raw is None:
        return
    items = parse_items(raw, query)
    print(f"  📦 Live listings fetched: {len(items)}")

    # Persist to tracker dataset so [2]/[3] analysis works
    for i in items:
        i.setdefault("last_seen", i.get("fetched_at", ""))
        i.setdefault("times_seen", "1")
        i.setdefault("presumed_sold", "False")
        i.setdefault("sold_at", "")
    saved = save_to_csv(items, query)
    if saved:
        print(f"  💾 {saved} new listings saved to tracker dataset")

    # Filter negative keywords
    neg_kw = [kw.lower() for kw in NEGATIVE_KEYWORDS]
    before = len(items)
    items = [i for i in items if not any(kw in i.get("title", "").lower() for kw in neg_kw)]
    filtered = before - len(items)
    if filtered:
        print(f"  🚫 Removed {filtered} listings matching negative keywords")

    # Filter to recently listed
    cutoff = datetime.now().astimezone()
    recent = []
    for item in items:
        listed_str = item.get("listed_at", "")
        if not listed_str:
            continue
        try:
            listed_dt = dateparser.parse(listed_str)
            if listed_dt.tzinfo is None:
                listed_dt = listed_dt.replace(tzinfo=cutoff.tzinfo)
            age_days = (cutoff - listed_dt).total_seconds() / 86400
            if age_days <= RECENT_DAYS:
                item["_age_days"] = round(age_days, 1)
                recent.append(item)
        except Exception:
            continue

    print(f"  🕐 Listed in last {RECENT_DAYS} days: {len(recent)}")

    # Find deals — any price at or below threshold
    deals = [i for i in recent if i["price"] <= threshold]

    if not deals:
        if not recent:
            print(f"\n  😴 No recent listings found at all.")
            return
        print(f"\n  😴 No deals found — sending top 3 cheapest recent listings to Discord instead.\n")
        top3 = sorted(recent, key=lambda i: i["price"])[:3]
        for rank, item in enumerate(top3, 1):
            saving  = sold_median - item["price"]
            pct_off = (saving / sold_median) * 100
            age     = item.get("_age_days", "?")
            print(f"  {'─'*56}")
            print(f"  #{rank}  £{item['price']:.2f}  ({pct_off:.0f}% below median, saving £{saving:.2f})")
            print(f"  📋 {item.get('title','')[:60]}")
            print(f"  🔗 {item.get('url','')}")
            _send_no_deals_fallback(item, sold_median, query, rank)
        print(f"\n  {'─'*56}")
        print(f"  ✅ Done — no deals, sent top 3 cheapest.")
        return

    print(f"\n  🚨 {len(deals)} DEAL(S) FOUND:\n")
    for item in sorted(deals, key=lambda i: i["price"]):
        saving  = sold_median - item["price"]
        pct_off = (saving / sold_median) * 100
        age     = item.get("_age_days", "?")
        print(f"  {'─'*56}")
        print(f"  💰 £{item['price']:.2f}  ({pct_off:.0f}% below median, saving £{saving:.2f})")
        print(f"  📋 {item.get('title','')[:60]}")
        print(f"  📏 {item.get('size','?')}  |  🏷 {item.get('condition','')}  |  🚚 {item.get('shipping','')}  |  🕐 {age}d ago")
        print(f"  👤 {item.get('seller','')}  |  📍 {item.get('location','')}")
        print(f"  🔗 {item.get('url','')}")
        send_discord_notification(item, sold_median, query, threshold)

    print(f"\n  {'─'*56}")
    print(f"  ✅ Done — {len(deals)} deal(s) alerted.")


# ── CLI ───────────────────────────────────────────────────────

def cli():
    print("\n" + "="*50)
    print("  📦 eBay Price Tracker CLI")
    print("="*50)

    # Show sold median per tracker on startup
    tracker_data_startup = load_trackers()
    startup_queries = get_queries(tracker_data_startup)
    if startup_queries:
        pct = int(DEAL_THRESHOLD_PCT * 100)
        print()
        for q in startup_queries:
            sold_median, n = load_sold_median(q)
            csv_path = sold_csv_path(q)
            if sold_median:
                threshold = sold_median * (1 - DEAL_THRESHOLD_PCT)
                print(f"  📊 [{q}] Sold median £{sold_median:.2f} ({n} listings) → alert at £{threshold:.2f} ({pct}% below)")
            else:
                print(f"  ⚠️  [{q}] No sold data ({csv_path}). Run scraper.py first.")
    else:
        print(f"\n  ℹ️  No trackers yet. Use [0] Quick deal scan to add one.")

    tracker_data = load_trackers()
    token        = None

    while True:
        trackers = get_queries(tracker_data)
        print(f"\n  Active trackers: {len(trackers)}")
        for i, t in enumerate(trackers, 1):
            rows    = load_dataset(t)
            kw      = tracker_data.get("model_keywords", {}).get(t, [])
            kw_str  = f" | {len(kw)} keywords" if kw else ""
            print(f"  {i}. {t} ({len(rows)} records{kw_str})")

        print("""
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  🔍 QUICK SCAN
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  [0]  Quick deal scan  ← type a query (or several, comma-separated)
  [5]  Quick scan ALL tracked queries

  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  📦 TRACKER MANAGEMENT
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  [2]  Run a tracker
  [3]  Run ALL
  [4]  Auto-run every 30 min (background)
  [6]  Stop auto-run
  [7]  Delete a tracker

  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  [q]  Quit
        """)

        try:
            choice = input("  Enter choice: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n\n👋 Goodbye!\n")
            return

        if choice == "q":
            print("\n👋 Goodbye!\n")
            break

        elif choice == "0":
            raw_input = input("\n  Search term(s) — comma-separate for multiple (e.g. 'All Saints leather jacket, Barbour jacket'): ").strip()
            if not raw_input:
                continue
            queries = [q.strip() for q in raw_input.split(",") if q.strip()]
            if not token:
                token = get_oauth_token()
                print("✅ Token obtained")
            for query in queries:
                if query not in get_queries(tracker_data):
                    tracker_data["queries"].append(query)
                    tracker_data.setdefault("model_keywords", {})[query] = []
                    save_trackers(tracker_data)
                    print(f"  ✅ '{query}' added to tracker management.")
                quick_deal_scan(query, token)

        elif choice == "5":
            trackers = get_queries(tracker_data)
            if not trackers:
                print("  No trackers yet — use [0] to add some first.")
                continue
            if not token:
                token = get_oauth_token()
                print("✅ Token obtained")
            print(f"\n  🔍 Scanning {len(trackers)} tracked quer{'y' if len(trackers)==1 else 'ies'}...\n")
            for query in trackers:
                quick_deal_scan(query, token)
                time.sleep(1)
            print(f"\n  ✅ All {len(trackers)} queries scanned.")

        elif choice in ("2", "3", "4"):
            trackers = get_queries(tracker_data)
            if not trackers:
                print("  No trackers — add one first!")
                continue
            if not token:
                token = get_oauth_token()
                print("✅ Token obtained")

            if choice == "2":
                for i, t in enumerate(trackers, 1): print(f"  {i}. {t}")
                sel = input("  Number: ").strip()
                try:
                    q  = trackers[int(sel) - 1]
                    kw = get_model_keywords(tracker_data, q)
                    run_tracker(q, token, kw)
                except (IndexError, ValueError):
                    print("  Invalid.")

            elif choice == "3":
                for q in trackers:
                    run_tracker(q, token, get_model_keywords(tracker_data, q))
                    time.sleep(2)

            elif choice == "4":
                if hasattr(cli, "_autorun_stop") and not cli._autorun_stop.is_set():
                    print("  ⚠️  Auto-run is already running in the background.")
                else:
                    stop_event = threading.Event()
                    cli._autorun_stop = stop_event

                    def _autorun_loop(queries, tok, stop):
                        while not stop.is_set():
                            print(f"\n  🔄 [Auto-run] Starting cycle for {len(queries)} tracker(s)...")
                            for q in queries:
                                if stop.is_set():
                                    break
                                run_tracker(q, tok, get_model_keywords(tracker_data, q))
                                time.sleep(2)
                            if not stop.is_set():
                                print(f"\n  ⏰ [Auto-run] Next cycle in 30 min. Menu is still active.\n")
                                stop.wait(timeout=1800)
                        print("  ⏹ [Auto-run] Stopped.")

                    t = threading.Thread(
                        target=_autorun_loop,
                        args=(list(trackers), token, stop_event),
                        daemon=True
                    )
                    t.start()
                    print(f"\n  ✅ Auto-run started in background for {len(trackers)} tracker(s).")
                    print(f"  Menu remains active — use [6] to stop auto-run.\n")

        elif choice == "6":
            if hasattr(cli, "_autorun_stop") and not cli._autorun_stop.is_set():
                cli._autorun_stop.set()
                print("\n  ⏹ Auto-run stop signal sent. It will halt after the current tracker finishes.\n")
            else:
                print("\n  ℹ️  Auto-run is not currently active.\n")

        elif choice == "7":
            trackers = get_queries(tracker_data)
            if not trackers:
                print("  No trackers to delete.")
                continue
            print("\n  Select a tracker to delete:")
            for i, t in enumerate(trackers, 1):
                print(f"  {i}. {t}")
            sel = input("  Number (or Enter to cancel): ").strip()
            if not sel:
                continue
            try:
                idx = int(sel) - 1
                query = trackers[idx]
            except (IndexError, ValueError):
                print("  Invalid selection.")
                continue
            confirm = input(f"  Delete '{query}'? This removes it from tracking (CSV data kept). [y/N]: ").strip().lower()
            if confirm == "y":
                tracker_data["queries"].remove(query)
                tracker_data.get("model_keywords", {}).pop(query, None)
                save_trackers(tracker_data)
                print(f"  ✅ '{query}' removed from trackers.")
            else:
                print("  Cancelled.")


if __name__ == "__main__":
    try:
        cli()
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!\n")