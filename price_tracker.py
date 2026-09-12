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


def extract_price_flipkart(page):
    price = extract_price_from_meta_or_jsonld(page)
    if price:
        return price

    # Fallback: Flipkart's CSS classes are randomly hashed and change often,
    # so as a last resort scan visible text for a rupee amount near the top
    # of the page.
    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""

    matches = re.findall(r"₹\s?([\d,]{4,})", body_text)
    if matches:
        # Usually the first ₹ amount on the page is the current price
        return clean_price_to_int(matches[0])
    return None


def get_price(page, url, site):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    # Flipkart especially needs a moment for JS to fill in price info
    page.wait_for_timeout(2500)

    if site == "amazon":
        return extract_price_amazon(page)
    elif site == "flipkart":
        return extract_price_flipkart(page)
    return None


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
                current_price = get_price(page, url, site)
            except Exception as e:
                print(f"  Error loading page: {e}")
                current_price = None

            if current_price is None:
                print(f"  Could not read price for {name} (site may have changed layout).")
                # Don't overwrite state on a failed read
                time.sleep(random.uniform(3, 6))
                continue

            print(f"  Current price: ₹{current_price}")

            previous = state.get(key, {})
            previous_price = previous.get("price")

            if previous_price is not None and current_price < previous_price:
                drop = previous_price - current_price
                message = (
                    f"🔻 Price drop!\n\n"
                    f"{name}\n"
                    f"₹{previous_price} → ₹{current_price} (down ₹{drop})\n\n"
                    f"{url}"
                )
                print("  Price dropped, sending Telegram alert.")
                send_telegram_message(message)
            elif previous_price is None:
                print("  First time seeing this product, no comparison to make yet.")
            else:
                print("  No drop.")

            state[key] = {
                "name": name,
                "price": current_price,
                "last_checked": datetime.now(timezone.utc).isoformat(),
            }

            # small random delay between products, to look less bot-like
            time.sleep(random.uniform(3, 7))

        browser.close()

    save_json(STATE_FILE, state)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
