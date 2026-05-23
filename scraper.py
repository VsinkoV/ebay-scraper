"""
eBay Sold Listings Scraper
Target: All Saints leather jackets — UK sales only
"""

import csv
import os
import re
import time
import random
from datetime import datetime, date
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


SEARCH_QUERY = "All Saints leather jacket"
MAX_PAGES = 10  # Set to None to scrape all pages
DATASETS_DIR = "datasets"

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")

OUTPUT_FILE = os.path.join(DATASETS_DIR, f"{_slug(SEARCH_QUERY)}_sold.csv")

FIELDS = ["title", "model", "price", "shipping", "date_sold", "days_ago", "condition", "url"]

# ebay.co.uk + LH_PrefLoc=1 restricts to UK-located items
BASE_URL = (
    "https://www.ebay.co.uk/sch/i.html"
    "?_nkw={query}"
    "&LH_Complete=1"
    "&LH_Sold=1"
    "&LH_PrefLoc=1"
    "&_pgn={page}"
)

# Known All Saints jacket model names (add more as needed)
ALLSAINTS_MODELS = [
    "Barton", "Cargo", "Kushiro", "Dalby", "Conroy", "Milo", "Vex",
    "Spencer", "Kalu", "Arlo", "Fader", "Cora", "Halley", "Leather",
    "Biker", "Cale", "Shift", "Ginza", "Elva", "Iria", "Fia",
    "Balfern", "Doma", "Catch", "City", "Idol", "Moya", "Yola",
    "Papin", "Nour", "Ness", "Belfern", "Wren", "Scout", "Jessa",
]


def build_url(query: str, page: int) -> str:
    encoded = query.replace(" ", "+")
    return BASE_URL.format(query=encoded, page=page)


def extract_model(title: str) -> str:
    """Try to extract the jacket model name from the listing title."""
    title_lower = title.lower()
    for model in ALLSAINTS_MODELS:
        if model.lower() in title_lower:
            return model
    # Fallback: grab the word after "AllSaints" or "All Saints" if not in known list
    match = re.search(r'all\s*saints\s+(\w+)', title, re.IGNORECASE)
    if match:
        word = match.group(1)
        # Skip generic words
        if word.lower() not in {"leather", "jacket", "biker", "the", "a", "an", "womens", "mens", "ladies"}:
            return word.capitalize()
    return ""


def parse_date_sold(raw: str) -> tuple[str, int]:
    """
    Parse 'Sold  Mar 4, 2026' into (date_string, days_ago).
    Returns ("", "") if parsing fails.
    """
    cleaned = re.sub(r"^sold\s*", "", raw, flags=re.IGNORECASE).strip()
    for fmt in ("%b %d, %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(cleaned, fmt).date()
            days_ago = (date.today() - dt).days
            return cleaned, days_ago
        except ValueError:
            continue
    return cleaned, ""


def parse_listings(page) -> list[dict]:
    listings = []

    items = page.query_selector_all("li.s-card[data-listingid]")
    for item in items:
        title_el = item.query_selector(".s-card__title span")
        price_el = item.query_selector(".s-card__price")
        date_el = item.query_selector(".s-card__caption span")
        condition_el = item.query_selector(".s-card__subtitle span")
        link_el = item.query_selector("a.s-card__link")

        title = title_el.inner_text().strip() if title_el else ""
        price = price_el.inner_text().strip() if price_el else ""
        raw_date = date_el.inner_text().strip() if date_el else ""
        condition = condition_el.inner_text().strip() if condition_el else ""
        url = link_el.get_attribute("href") if link_el else ""

        # Shipping: find attribute row mentioning delivery/shipping/free
        shipping = ""
        for row in item.query_selector_all(".s-card__attribute-row span"):
            text = row.inner_text().strip()
            if any(kw in text.lower() for kw in ("delivery", "shipping", "free")):
                shipping = text
                break

        # Clean price range — take the lower value
        if " to " in price:
            price = price.split(" to ")[0]

        date_sold, days_ago = parse_date_sold(raw_date)
        model = extract_model(title)

        listings.append({
            "title": title,
            "model": model,
            "price": price,
            "shipping": shipping,
            "date_sold": date_sold,
            "days_ago": days_ago,
            "condition": condition,
            "url": url,
        })

    return listings


def has_next_page(page) -> bool:
    next_btn = page.query_selector("a.pagination__next, [aria-label='Go to next search page']")
    if next_btn is None:
        return False
    disabled = next_btn.get_attribute("aria-disabled") or next_btn.get_attribute("disabled")
    return not disabled


def check_rate_limited(page) -> bool:
    """
    Return True if eBay is blocking or rate-limiting this session.
    Checks page title and URL for known block/security indicators.
    """
    title = (page.title() or "").lower()
    url   = page.url.lower()
    block_signals = [
        "security measure", "robot check", "blocked", "access denied",
        "verify you're a human", "unusual traffic", "too many requests",
        "sign in to continue", "captcha",
    ]
    if any(s in title for s in block_signals):
        return True
    # eBay sometimes redirects to /signin or /security pages
    if any(seg in url for seg in ["/blocked", "/security", "isrobot=true", "auth/signin"]):
        return True
    return False


def scrape(query: str, max_pages: int | None = None) -> list[dict]:
    all_listings = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        page_num      = 1
        empty_streak  = 0
        while True:
            url = build_url(query, page_num)
            print(f"[Page {page_num}] Fetching: {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_selector("li.s-card[data-listingid], iframe[title*='challenge']", timeout=30_000)

                if check_rate_limited(page):
                    print(f"\n  🚫 Rate limited / blocked by eBay on page {page_num}.")
                    print(f"  Page title : {page.title()}")
                    print(f"  URL        : {page.url}")
                    page.screenshot(path=f"debug_blocked_p{page_num}.png", full_page=True)
                    print(f"  Screenshot saved: debug_blocked_p{page_num}.png")
                    print("  Wait a few minutes, then press Enter to retry — or Ctrl+C to stop.")
                    input()
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    if check_rate_limited(page):
                        print("  Still blocked. Stopping scrape early.")
                        break

                if page.query_selector("iframe[title*='challenge']"):
                    print("  CAPTCHA detected — solve it in the browser, then press Enter here.")
                    input()
                    page.wait_for_selector("li.s-card[data-listingid]", timeout=30_000)
            except PlaywrightTimeout:
                page.screenshot(path=f"debug_timeout_p{page_num}.png", full_page=True)
                with open("debug_page.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                print(f"  Timeout on page {page_num}. Saved debug files.")
                break

            listings = parse_listings(page)
            print(f"  Found {len(listings)} listings")

            if not listings:
                empty_streak += 1
                if empty_streak >= 3:
                    print(f"  ⚠️  {empty_streak} consecutive empty pages — possible soft block. Stopping.")
                    break
            else:
                empty_streak = 0

            all_listings.extend(listings)

            if max_pages and page_num >= max_pages:
                print(f"  Reached max pages ({max_pages}), stopping.")
                break

            if not has_next_page(page):
                print("  No next page, done.")
                break

            page_num += 1
            time.sleep(random.uniform(2.0, 4.0))

        browser.close()

    return all_listings


def save_to_csv(listings: list[dict], filename: str) -> None:
    if not listings:
        print("No listings to save.")
        return
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(listings)
    print(f"\nSaved {len(listings)} listings to {filename}")


def main():
    print(f"Scraping eBay UK sold listings for: '{SEARCH_QUERY}'")
    print(f"Max pages: {MAX_PAGES or 'all'}")
    print("-" * 60)

    start = datetime.now()
    listings = scrape(SEARCH_QUERY, max_pages=MAX_PAGES)
    elapsed = (datetime.now() - start).seconds

    print("-" * 60)
    print(f"Total listings scraped: {len(listings)}")
    print(f"Time elapsed: {elapsed}s")

    save_to_csv(listings, OUTPUT_FILE)

    prices = []
    for item in listings:
        raw = item["price"].replace("£", "").replace("$", "").replace(",", "").strip()
        try:
            prices.append(float(raw))
        except ValueError:
            pass

    if prices:
        print(f"\nPrice summary (GBP):")
        print(f"  Min:    £{min(prices):.2f}")
        print(f"  Max:    £{max(prices):.2f}")
        print(f"  Avg:    £{sum(prices)/len(prices):.2f}")
        print(f"  Median: £{sorted(prices)[len(prices)//2]:.2f}")

    # Model breakdown
    from collections import Counter
    models = [l["model"] for l in listings if l["model"]]
    if models:
        print(f"\nTop models sold:")
        for model, count in Counter(models).most_common(10):
            print(f"  {model}: {count}")


if __name__ == "__main__":
    main()
