"""Run this to dump the form HTML so we can find the right selectors."""
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

URL = "https://forms.cloud.microsoft/e/EdiaKAAQwF"

opts = Options()
opts.add_argument("--window-size=1280,900")
svc = Service(ChromeDriverManager().install())
driver = webdriver.Chrome(service=svc, options=opts)

driver.get(URL)
print("Waiting 6s for JS to render...")
time.sleep(6)

html = driver.page_source
with open("form_debug.html", "w", encoding="utf-8") as f:
    f.write(html)
print("Saved to form_debug.html")

# Print any elements with role=radio or role=checkbox
radios = driver.find_elements(By.CSS_SELECTOR, '[role="radio"]')
print(f"\nFound {len(radios)} role=radio elements:")
for r in radios[:6]:
    print(f"  text={r.text!r}  tag={r.tag_name}  class={r.get_attribute('class')[:60]}")

# Print question containers
for sel in [
    'div[data-automation-id="questionItem"]',
    'div[data-automation-id="FormQuestion"]',
    '[class*="question"]',
    '[class*="Question"]',
]:
    els = driver.find_elements(By.CSS_SELECTOR, sel)
    print(f"\n{sel!r} → {len(els)} found")

driver.quit()
