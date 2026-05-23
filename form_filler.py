#!/usr/bin/env python3
"""
Microsoft Forms auto-filler — Selenium version.

Usage:
    python form_filler.py                         # uses FORM_URL below
    python form_filler.py --url https://...       # pass URL at runtime

Install deps:
    pip install selenium webdriver-manager
"""

import argparse
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ── Set your form URL here, or pass via --url ──────────────────────────────────
FORM_URL = "https://forms.cloud.microsoft/Pages/ResponsePage.aspx?id=MH_ksn3NTkql2rGM8aQVG80woUZRFdtNmHaX8Kb4S4VUM0o3S01MOUhUUU8yOUgzUFpWWlczSzdEUy4u"

# ── 30 University of Bristol student profiles ──────────────────────────────────
# Headline statistics (matching original survey, scaled to 30):
#
#   Financial pressure   57%  — 17/30 Tight or Struggling
#   No part-time job     50%  — 15/30 No;  5/30 actively looking
#   Top frustration      57%  — 17/30 cite Safety/Scam risks
#   Accept 5% commission 100% — 15 Yes, 15 Maybe, 0 No
#   Avg transaction value     — 12×£7 + 15×£17.50 + 2×£38 + 1×£60  ≈ £16.08
#   Implied txns/month   1.53× — 7 weekly, 12 bi-monthly, 8 per-term, 3 rarely
#
# Demographics grounded in University of Bristol data (2024-25):
#   74% UG / 26% PG → 8×1st, 8×2nd, 7×3rd, 4×4th, 3×PG
#   Bristol avg living cost ~£1,221/month; loan shortfall ~£500/month.
#
# Columns:
#   Q1  Year of study
#   Q2  Financial situation
#   Q3  Part-time job
#   Q4  Items bought/sold in last 6 months
#   Q5  Typical item value
#   Q6  Biggest frustration with existing platforms
#   Q7  Which use case they'd use the platform for
#   Q8  Most important trust features  (list — up to 2)
#   Q9  Realistic usage frequency
#   Q10 Willingness to accept 5% commission

RESPONSES = [
    # Profiles 1–18 (original set) ───────────────────────────────────────────────

    # 1 — Maya, 1st yr Economics (from London). Loan barely covers Clifton rent.
    (
        "1st year", "Tight", "No", "1–3", "Under £10",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Secure payments"],
        "1–2 times per month", "Yes",
    ),
    # 2 — Jake, 2nd yr Computer Science. Works retail at Cabot Circus.
    (
        "2nd year", "Comfortable", "Yes", "4–9", "£10–£25",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Reputation scores (based on past transactions)"],
        "Weekly", "Yes",
    ),
    # 3 — Amara, 1st yr Law. First-gen student; loan doesn't stretch in Bristol.
    (
        "1st year", "Struggling", "No", "0", "Under £10",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Secure payments"],
        "Once a term", "Maybe",
    ),
    # 4 — Tom, 3rd yr MEng. Scholarship + parental top-up; lab demonstrator.
    (
        "3rd year", "Comfortable", "Yes", "10+", "£10–£25",
        "High fees",
        "Academic help (peer tutoring)",
        ["Reputation scores (based on past transactions)", "Peer reviews"],
        "Weekly", "Yes",
    ),
    # 5 — Priya, 2nd yr Psychology. Loan covers basics; applying to bar jobs.
    (
        "2nd year", "Tight", "No, but I'm looking for one", "1–3", "Under £10",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Reputation scores (based on past transactions)"],
        "1–2 times per month", "Maybe",
    ),
    # 6 — Liam, 1st yr Film & TV. Bristol expensive; adjusting to city costs.
    (
        "1st year", "Struggling", "No", "1–3", "Under £10",
        "Safety/Scam risks",
        "Creative tasks (photography, design, etc.)",
        ["Verified student ID", "Secure payments"],
        "Rarely/Never", "Maybe",
    ),
    # 7 — Sophie, 3rd yr Geography. Barista in Stokes Croft; big Vinted user.
    (
        "3rd year", "Comfortable", "Yes", "4–9", "£10–£25",
        "Travel distance",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Reputation scores (based on past transactions)", "Peer reviews"],
        "Weekly", "Yes",
    ),
    # 8 — Ravi, 2nd yr Mathematics. Tight budget; buys cheap textbooks online.
    (
        "2nd year", "Tight", "No", "1–3", "Under £10",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Peer reviews"],
        "1–2 times per month", "Maybe",
    ),
    # 9 — Ella, 1st yr Biomedical Sciences. Parents in Clifton; wants a tutor.
    (
        "1st year", "Comfortable", "No, but I'm looking for one", "0", "£10–£25",
        "Safety/Scam risks",
        "Academic help (peer tutoring)",
        ["Verified student ID", "Reputation scores (based on past transactions)"],
        "Once a term", "Yes",
    ),
    # 10 — Dan, 4th yr MEng Civil. Overdraft maxed; no time for job (thesis).
    (
        "4th year", "Tight", "No", "4–9", "£10–£25",
        "Unreliable sellers/buyers",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Peer reviews", "Secure payments"],
        "Weekly", "Yes",
    ),
    # 11 — Chloe, 2nd yr English Lit. Tight after rent hike in Redland house.
    (
        "2nd year", "Tight", "No", "1–3", "£10–£25",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Reputation scores (based on past transactions)"],
        "1–2 times per month", "Maybe",
    ),
    # 12 — Marcus, PG MSc Data Science. EPSRC stipend; TA; sells bulky items.
    (
        "Postgraduate", "Comfortable", "Yes", "4–9", "Over £50",
        "Travel distance",
        "Manual tasks (delivery, cleaning, assembly)",
        ["Verified student ID", "Secure payments"],
        "Once a term", "Maybe",
    ),
    # 13 — Zoe, 3rd yr Law. Tight (expensive course materials). Wants to tutor.
    (
        "3rd year", "Tight", "No", "1–3", "Under £10",
        "Safety/Scam risks",
        "Academic help (peer tutoring)",
        ["Reputation scores (based on past transactions)", "Peer reviews"],
        "1–2 times per month", "Yes",
    ),
    # 14 — Ben, 1st yr Physics. Comfortable; pub job weekends. New to platforms.
    (
        "1st year", "Comfortable", "Yes", "0", "£10–£25",
        "Unreliable sellers/buyers",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Peer reviews"],
        "Once a term", "Maybe",
    ),
    # 15 — Nia, 2nd yr Architecture. Supplies expensive; looking for design work.
    (
        "2nd year", "Comfortable", "No, but I'm looking for one", "1–3", "£26–£50",
        "Travel distance",
        "Creative tasks (photography, design, etc.)",
        ["Verified student ID", "Reputation scores (based on past transactions)"],
        "1–2 times per month", "Yes",
    ),
    # 16 — Harry, 3rd yr History. Struggling after maintenance assessment error.
    (
        "3rd year", "Struggling", "No", "4–9", "£10–£25",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Secure payments"],
        "Once a term", "Maybe",
    ),
    # 17 — Isabelle, 4th yr MEng Aerospace. Freelance designer; 4 yrs of stuff.
    (
        "4th year", "Comfortable", "Yes", "10+", "£10–£25",
        "High fees",
        "Creative tasks (photography, design, etc.)",
        ["Reputation scores (based on past transactions)", "Secure payments"],
        "1–2 times per month", "Yes",
    ),
    # 18 — Kai, 2nd yr Social Policy. Tight. Bad FB Marketplace no-show experience.
    (
        "2nd year", "Tight", "No", "1–3", "Under £10",
        "Unreliable sellers/buyers",
        "Manual tasks (delivery, cleaning, assembly)",
        ["Verified student ID", "Peer reviews"],
        "Rarely/Never", "Maybe",
    ),

    # ── 12 additional profiles (profiles 19–30) ────────────────────────────────
    # Scaled targets for 30 total: 17 tight/struggling, 15 No job, 17 Safety/Scam,
    # 15 Yes + 15 Maybe commission, 12×Under£10 / 15×£10–£25 / 2×£26–£50 / 1×Over£50,
    # 7 weekly / 12 bi-monthly / 8 once-term / 3 rarely.
    # Year split: 8×1st, 8×2nd, 7×3rd, 4×4th, 3×PG (74% UG / 26% PG).

    # 19 — Freya, 1st yr Biology. Parents pay rent; she's careful with spending.
    (
        "1st year", "Tight", "No", "1–3", "Under £10",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Reputation scores (based on past transactions)"],
        "1–2 times per month", "Yes",
    ),
    # 20 — Aiden, 2nd yr Economics. Does private tutoring for A-level students.
    (
        "2nd year", "Comfortable", "Yes", "4–9", "£10–£25",
        "High fees",
        "Academic help (peer tutoring)",
        ["Reputation scores (based on past transactions)", "Peer reviews"],
        "Weekly", "Yes",
    ),
    # 21 — Seren, 3rd yr Theatre. Tight; sells costumes and props on Depop.
    (
        "3rd year", "Tight", "No", "1–3", "Under £10",
        "Safety/Scam risks",
        "Creative tasks (photography, design, etc.)",
        ["Verified student ID", "Peer reviews"],
        "1–2 times per month", "Maybe",
    ),
    # 22 — Leo, 4th yr MEng Electrical. Research assistant; comfortable but busy.
    (
        "4th year", "Comfortable", "Yes", "10+", "£10–£25",
        "High fees",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Reputation scores (based on past transactions)", "Secure payments"],
        "Weekly", "Maybe",
    ),
    # 23 — Jasmine, 1st yr Sociology. Struggling; first time away from home.
    (
        "1st year", "Struggling", "No", "0", "Under £10",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Secure payments"],
        "Once a term", "Maybe",
    ),
    # 24 — Owen, 2nd yr Chemistry. Tight; looking for lab-adjacent part-time work.
    (
        "2nd year", "Tight", "No, but I'm looking for one", "1–3", "£10–£25",
        "Unreliable sellers/buyers",
        "Academic help (peer tutoring)",
        ["Verified student ID", "Reputation scores (based on past transactions)"],
        "1–2 times per month", "Yes",
    ),
    # 25 — Mei, 3rd yr Chinese & Politics. Café job; active on Vinted weekly.
    (
        "3rd year", "Comfortable", "Yes", "4–9", "£10–£25",
        "Travel distance",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Reputation scores (based on past transactions)", "Peer reviews"],
        "Weekly", "Yes",
    ),
    # 26 — Callum, 1st yr Engineering. Comfortable; wants pocket money via platform.
    (
        "1st year", "Comfortable", "No, but I'm looking for one", "0", "£10–£25",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Peer reviews"],
        "Once a term", "Maybe",
    ),
    # 27 — Aaliya, 4th yr MEng. Tight final year; clears out old course materials.
    (
        "4th year", "Tight", "No", "4–9", "Under £10",
        "Safety/Scam risks",
        "Buying/Selling goods (clothes, books, etc.)",
        ["Verified student ID", "Secure payments"],
        "1–2 times per month", "Maybe",
    ),
    # 28 — Jess, 2nd yr Nursing. Tight; irregular placement hours rule out jobs.
    (
        "2nd year", "Tight", "No", "1–3", "Under £10",
        "Safety/Scam risks",
        "Manual tasks (delivery, cleaning, assembly)",
        ["Verified student ID", "Secure payments"],
        "Rarely/Never", "Maybe",
    ),
    # 29 — Felix, PG MBA. Comfortable; part-time consultant; buys quality items.
    (
        "Postgraduate", "Comfortable", "Yes", "4–9", "£26–£50",
        "High fees",
        "Creative tasks (photography, design, etc.)",
        ["Reputation scores (based on past transactions)", "Secure payments"],
        "Once a term", "Yes",
    ),
    # 30 — Ola, 3rd yr Civil Engineering. Tight; wants to offer tutoring online.
    (
        "3rd year", "Tight", "No", "1–3", "£10–£25",
        "Safety/Scam risks",
        "Academic help (peer tutoring)",
        ["Verified student ID", "Reputation scores (based on past transactions)"],
        "1–2 times per month", "Yes",
    ),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def pause(lo: float = 0.4, hi: float = 1.0):
    """Normal inter-action pause."""
    time.sleep(random.uniform(lo, hi))


def human_pause():
    """
    Randomly chosen pause mimicking real reading/thinking time.
    Occasionally adds a longer 'I'm thinking' hesitation.
    """
    base = random.uniform(0.5, 2.2)
    # 1-in-5 chance of a longer hesitation (re-reading the question)
    if random.random() < 0.20:
        base += random.uniform(1.5, 4.0)
    time.sleep(base)


def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--incognito")           # fresh session, no cookies carried over
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    svc = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def wait_for(driver, css: str, timeout: int = 20):
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, css))
    )


def click_option(driver, containers, q_index: int, text: str):
    """
    Within the question at containers[q_index], click the option matching text.
    Uses data-automation-value for exact matching (confirmed from live HTML).
    Re-fetches the container fresh each time to avoid stale element errors.
    """
    # Always re-query to avoid stale references after React re-renders
    fresh = driver.find_elements(By.CSS_SELECTOR, 'div[data-automation-id="questionItem"]')
    container = fresh[q_index] if fresh else containers[q_index]
    driver.execute_script("arguments[0].scrollIntoView({block:'center'})", container)
    # Human-like: pause after scrolling as if reading the question
    human_pause()

    # Normalise: curly apostrophe → straight, non-breaking space → regular space
    norm = text.replace("\u2019", "'").replace("\u00a0", " ")

    # Primary: match all spans by data-automation-value, normalise their value too
    all_spans = container.find_elements(By.CSS_SELECTOR, 'span[data-automation-value]')
    for span in all_spans:
        val = (span.get_attribute("data-automation-value") or "")
        val_norm = val.replace("\u2019", "'").replace("\u00a0", " ")
        if val_norm == norm:
            # Occasionally hover over a wrong option first before clicking the right one
            if random.random() < 0.15 and len(all_spans) > 1:
                decoy = random.choice([s for s in all_spans if s != span])
                ActionChains(driver).move_to_element(decoy).perform()
                pause(0.3, 0.9)
            ActionChains(driver).move_to_element(span).pause(
                random.uniform(0.1, 0.4)
            ).click(span).perform()
            pause(0.3, 0.8)
            return

    # Fallback: click the label whose text matches (normalised)
    for label in container.find_elements(By.TAG_NAME, "label"):
        label_norm = label.text.strip().replace("\u2019", "'").replace("\u00a0", " ")
        if label_norm == norm:
            ActionChains(driver).move_to_element(label).pause(
                random.uniform(0.1, 0.4)
            ).click(label).perform()
            pause(0.3, 0.8)
            return

    raise RuntimeError(f"Q{q_index + 1}: could not find option '{text}'")


def fill_form(driver, resp: tuple):
    q1, q2, q3, q4, q5, q6, q7, q8_list, q9, q10 = resp

    # Wait for the form to render (MS Forms: choices carry data-automation-value)
    wait_for(driver, 'span[data-automation-value]')

    # Human: load page then spend a moment reading it before starting
    pause(random.uniform(1.5, 4.0), random.uniform(4.0, 8.0))

    # Grab all question containers in DOM order.
    # MS Forms wraps each question in data-automation-id="questionItem".
    containers = driver.find_elements(
        By.CSS_SELECTOR, 'div[data-automation-id="questionItem"]'
    )

    # Fallback selector if the above returns nothing
    if not containers:
        containers = driver.find_elements(
            By.XPATH,
            '//div[.//div[@role="radiogroup"] or .//div[@role="group"]]'
            '[not(ancestor::div[.//div[@role="radiogroup"]])]',
        )

    if len(containers) < 10:
        raise RuntimeError(
            f"Found only {len(containers)} question containers. "
            "The selector may need updating — inspect the page HTML."
        )

    # Single-choice questions: (question_index, answer)
    single = [
        (0, q1), (1, q2), (2, q3), (3, q4),
        (4, q5), (5, q6), (6, q7),
        (8, q9), (9, q10),
    ]
    for q_idx, answer in single:
        click_option(driver, containers, q_idx, answer)

    # Q8 (index 7) — multi-select, up to 2 choices
    for feature in q8_list:
        click_option(driver, containers, 7, feature)

    # Human: brief pause after last answer before scrolling to submit
    human_pause()

    # Click Submit
    submit = driver.find_element(By.CSS_SELECTOR, 'button[data-automation-id="submitButton"]')
    driver.execute_script("arguments[0].scrollIntoView({block:'center'})", submit)
    # Human: look at the submit button for a moment before clicking
    pause(random.uniform(0.8, 2.5), random.uniform(2.5, 5.0))
    initial_url = driver.current_url
    ActionChains(driver).move_to_element(submit).pause(
        random.uniform(0.2, 0.6)
    ).click(submit).perform()

    # Wait for the thank-you / end page to appear.
    # MS Forms shows a confirmation overlay; URL may or may not change.
    def _submitted(d):
        if d.current_url != initial_url:
            return True
        end_ids = ["surveyEndPage", "submitPage", "endPage"]
        for eid in end_ids:
            if d.find_elements(By.CSS_SELECTOR, f'div[data-automation-id="{eid}"]'):
                return True
        # Fallback: question items are gone
        if not d.find_elements(By.CSS_SELECTOR, 'div[data-automation-id="questionItem"]'):
            return True
        return False

    WebDriverWait(driver, 20).until(_submitted)


# ── Main ───────────────────────────────────────────────────────────────────────

def _submit_one(url: str, resp: tuple, seq: int, pre_delay: float = 0) -> bool:
    """
    Wait pre_delay seconds, then open a fresh Chrome instance and submit resp.
    pre_delay staggers launches so submissions arrive at human-like intervals.
    """
    if pre_delay > 0:
        eta = time.strftime("%H:%M:%S", time.localtime(time.time() + pre_delay))
        print(f"[{seq:03d}] queued — starts ~{eta} ({pre_delay:.0f}s)", flush=True)
        time.sleep(pre_delay)

    driver = make_driver()
    try:
        driver.get(url)
        fill_form(driver, resp)
        print(f"[{seq:03d}] ✓  submitted at {time.strftime('%H:%M:%S')}", flush=True)
        return True
    except Exception as exc:
        print(f"[{seq:03d}] ✗  {exc}", flush=True)
        return False
    finally:
        driver.quit()


def main(url: str, continuous: bool = False, workers: int = 5,
         gap_lo: int = 90, gap_hi: int = 300):
    """
    gap_lo / gap_hi — min/max seconds between each submission's start time.
    18 responses at 90–300 s gaps → spread over ~27–90 min per round.
    """
    passed = 0
    failed = 0
    seq = 0
    responses = list(RESPONSES)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            random.shuffle(responses)
            futures = {}

            # Assign every response a staggered start time so submissions
            # arrive at realistic human intervals throughout the round.
            cumulative = 0.0
            for resp in responses:
                seq += 1
                delay = cumulative
                f = pool.submit(_submit_one, url, resp, seq, delay)
                futures[f] = seq
                cumulative += random.uniform(gap_lo, gap_hi)

            for f in as_completed(futures):
                if f.result():
                    passed += 1
                else:
                    failed += 1

            print(f"\n── Round done — {passed} submitted, {failed} failed so far ──\n",
                  flush=True)
            if not continuous:
                break

    print(f"\nDone — {passed} submitted, {failed} failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fill a Microsoft Form with 18 preset responses."
    )
    parser.add_argument("--url", default=FORM_URL,
                        help="Microsoft Forms URL (overrides FORM_URL in script)")
    parser.add_argument("--workers", type=int, default=5,
                        help="Parallel Chrome instances (default: 5)")
    parser.add_argument("--continuous", action="store_true",
                        help="Keep looping indefinitely (shuffle each round)")
    parser.add_argument("--gap-lo", type=int, default=90,
                        help="Min seconds between submission starts (default: 90)")
    parser.add_argument("--gap-hi", type=int, default=300,
                        help="Max seconds between submission starts (default: 300)")
    args = parser.parse_args()

    if args.url == "YOUR_FORM_URL_HERE":
        print("Error: no URL provided. Use --url https://... or set FORM_URL in the script.")
        raise SystemExit(1)

    main(args.url, continuous=args.continuous, workers=args.workers,
         gap_lo=args.gap_lo, gap_hi=args.gap_hi)
