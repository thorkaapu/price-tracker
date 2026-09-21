"""
Price tracker for Amazon.in and Flipkart product pages.

What it does:
- Opens each product URL listed in products.json using a real (headless) browser
- Reads the current price off the page
- Compares it to the last price we saw (stored in last_prices.json)
- If the price DROPPED, sends a Telegram message
- Always updates last_prices.json with the latest price/time, so next run
  has something to compare against

This is meant to run on a schedule (see .github/workflows/track-prices.yml),
so you don't need to keep any device on for it to work.
"""

import json
import os
import re
import sys
import time
import random
from pathlib import Path
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
STATE_FILE = BASE_DIR / "last_prices.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Minimum drop (in rupees) before a general "Price drop!" alert is sent.
# Small wobbles (₹50-500) happen often due to rounding/regional pricing and
# aren't usually worth an alert. This does NOT affect target-price alerts,
# which always fire the moment a product hits its target.
MIN_DROP_ALERT_THRESHOLD = 2000


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing, skipping message send.")
        print("Message would have been:\n" + text)
        return
    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "false"}
    ).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")


def clean_price_to_int(raw):
    """Turn '₹1,09,990' or '109990.00' into 109990 (int). Returns None if it can't parse."""
    if raw is None:
        return None
    digits = re.sub(r"[^\d]", "", str(raw))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def extract_price_from_meta_or_jsonld(page):
    """
    Try robust, selector-independent ways to get price first, since visible
    CSS classes on Amazon/Flipkart change often:
    1. <meta itemprop="price" content="...">
    2. <meta property="product:price:amount" content="...">
    3. JSON-LD script tags with a "price" or "offers.price" field
    """
    # 1 & 2: meta tags
    for selector in [
        'meta[itemprop="price"]',
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
    ]:
        el = page.query_selector(selector)
        if el:
            content = el.get_attribute("content")
            price = clean_price_to_int(content)
            if price:
                return price

    # 3: JSON-LD
    scripts = page.query_selector_all('script[type="application/ld+json"]')
    for script in scripts:
        try:
            raw = script.inner_text()
            data = json.loads(raw)
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            if isinstance(offers, dict):
                price = clean_price_to_int(offers.get("price"))
                if price:
                    return price
            if isinstance(offers, list):
                for offer in offers:
                    if isinstance(offer, dict):
                        price = clean_price_to_int(offer.get("price"))
                        if price:
                            return price
    return None


def extract_price_amazon(page):
    price = extract_price_from_meta_or_jsonld(page)
    if price:
        return price

    # Fallback: known Amazon price selectors (may change over time)
    for selector in [
        "#corePriceDisplay_desktop_feature_div span.a-price-whole",
        "#corePrice_feature_div span.a-price-whole",
        "span.a-price.a-text-price span.a-offscreen",
        "span.a-price span.a-offscreen",
    ]:
        el = page.query_selector(selector)
        if el:
            price = clean_price_to_int(el.inner_text())
            if price:
                return price
    return None


def extract_price_generic(page):
    """
    Generic price reader for any site that isn't Amazon or Flipkart (e.g. a
    brand's own official store). Tries meta/JSON-LD first, then falls back
    to scanning visible text for a rupee amount.
    """
    price = extract_price_from_meta_or_jsonld(page)
    if price:
        return price

    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""

    matches = re.findall(r"₹\s?([\d,]{4,})", body_text)
    if matches:
        # Usually the first ₹ amount on the page is the current price
        return clean_price_to_int(matches[0])
    return None


def extract_price_flipkart(page):
    return extract_price_generic(page)


OUT_OF_STOCK_PHRASES = [
    "currently unavailable",
    "out of stock",
    "sold out",
    "coming soon",
    "this item cannot be shipped",
    "temporarily out of stock",
    "notify me",
]


def is_out_of_stock(page, site):
    """
    Checks visible page text for common 'not buyable right now' phrases.
    Amazon/Flipkart often keep the old price embedded in the page's data even
    when the item is unavailable, so this check runs BEFORE we trust any
    price we found.
    """
    try:
        body_text = page.inner_text("body").lower()
    except Exception:
        return False

    for phrase in OUT_OF_STOCK_PHRASES:
        if phrase in body_text:
            return True

    if site == "amazon":
        # Amazon shows a dedicated "Currently unavailable" block, and the
        # normal Buy Now / Add to Cart buttons disappear when unavailable.
        if page.query_selector("#outOfStock"):
            return True
        has_buy_button = page.query_selector("#buy-now-button") or page.query_selector(
            "#add-to-cart-button"
        )
        if not has_buy_button:
            # No purchase button found at all is a strong signal something's off,
            # but pages can be slow to render, so this alone isn't conclusive —
            # only treat it as out of stock combined with no price element below.
            pass

    return False


def get_price(page, url, site):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    # Flipkart especially needs a moment for JS to fill in price info
    page.wait_for_timeout(2500)

    if is_out_of_stock(page, site):
        return None, True  # (price, out_of_stock)

    if site == "amazon":
        return extract_price_amazon(page), False
    elif site == "flipkart":
        return extract_price_flipkart(page), False
    else:
        # Any other site (e.g. a brand's own official store) uses the
        # generic reader: meta/JSON-LD first, then a text-scan fallback.
        return extract_price_generic(page), False


def main():
    products = load_json(PRODUCTS_FILE, [])
    state = load_json(STATE_FILE, {})

    if not products:
        print("No products configured in products.json")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="en-IN")
        page = context.new_page()

        for product in products:
            name = product["name"]
            url = product["url"]
            site = product["site"]
            key = url  # use URL as the unique key in state file

            print(f"Checking: {name}")
            try:
                current_price, out_of_stock = get_price(page, url, site)
            except Exception as e:
                print(f"  Error loading page: {e}")
                current_price, out_of_stock = None, False

            if out_of_stock:
                print(f"  {name} is currently OUT OF STOCK — skipping price comparison.")
                previous = state.get(key, {})
                state[key] = {
                    "name": name,
                    "price": previous.get("price"),  # keep last known real price
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                    "target_alerted": previous.get("target_alerted", False),
                    "in_stock": False,
                }
                time.sleep(random.uniform(3, 6))
                continue

            if current_price is None:
                print(f"  Could not read price for {name} (site may have changed layout).")
                # Don't overwrite state on a failed read
                time.sleep(random.uniform(3, 6))
                continue

            print(f"  Current price: ₹{current_price}")

            previous = state.get(key, {})
            previous_price = previous.get("price")
            target_price = product.get("target_price")
            target_already_alerted = previous.get("target_alerted", False)

            if previous_price is not None and current_price < previous_price:
                drop = previous_price - current_price
                if drop >= MIN_DROP_ALERT_THRESHOLD:
                    message = (
                        f"🔻 Price drop!\n\n"
                        f"{name}\n"
                        f"₹{previous_price} → ₹{current_price} (down ₹{drop})\n\n"
                        f"{url}"
                    )
                    print(f"  Price dropped by ₹{drop}, sending Telegram alert.")
                    send_telegram_message(message)
                else:
                    print(f"  Price dropped by only ₹{drop} (below ₹{MIN_DROP_ALERT_THRESHOLD} threshold), not alerting.")
            elif previous_price is None:
                print("  First time seeing this product, no comparison to make yet.")
            else:
                print("  No drop.")

            # Separate check: has it hit (or gone below) your target price?
            # Only alerts once per crossing, so it won't spam you every 30 min.
            target_hit_now = target_price is not None and current_price <= target_price

            if target_hit_now and not target_already_alerted:
                message = (
                    f"🎯 Target price reached!\n\n"
                    f"{name}\n"
                    f"Current price: ₹{current_price} (your target: ₹{target_price})\n\n"
                    f"{url}"
                )
                print("  Target price reached, sending Telegram alert.")
                send_telegram_message(message)
                target_already_alerted = True
            elif not target_hit_now:
                # Price is back above target, so reset — a future drop below
                # target will alert again.
                target_already_alerted = False

            state[key] = {
                "name": name,
                "price": current_price,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "target_alerted": target_already_alerted,
                "in_stock": True,
            }

            # small random delay between products, to look less bot-like
            time.sleep(random.uniform(3, 7))

        browser.close()

    save_json(STATE_FILE, state)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
