import os
import json
import time
import requests
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

STATE_PATH = "state.json"

DEFAULT_WATCH_URLS = [
    "https://www.pokemoncenter.com/en-gb",
    "https://www.pokemoncenter.com/en-gb/category/trading-card-game",
    "https://www.pokemoncenter.com/en-gb/new-releases",
]

DEFAULT_KEYWORDS = [
    "etb",
    "elite trainer box",
    "booster box",
    "booster bundle",
    "premium",
    "collection",
    "special",
    "reprint",
    "destined rivals",
    "ascended heroes",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

def tg_send(text):
    token = os.environ.get("BOT_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, data={"chat_id": chat_id, "text": text})

def parse_env_list(name, fallback):
    raw = os.environ.get(name, "")
    if not raw:
        return fallback
    return [x.strip() for x in raw.split(",") if x.strip()]

def keyword_match(title, keywords):
    t = title.lower()
    return any(k.lower() in t for k in keywords)

def fetch_products_from_api(page):
    products = []

    # Look at network responses for product API calls
    for response in page.context.responses:
        try:
            url = response.url
            if "product" in url and response.request.resource_type == "xhr":
                if "json" in response.headers.get("content-type", ""):
                    data = response.json()
                    if isinstance(data, dict):
                        if "products" in data:
                            for p in data["products"]:
                                title = p.get("name", "")
                                link = p.get("url", "")
                                if title and link:
                                    products.append({
                                        "title": title,
                                        "url": link if link.startswith("http") else f"https://www.pokemoncenter.com{link}"
                                    })
        except:
            continue

    return products

def fetch_products(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        page.goto(url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(5000)

        products = fetch_products_from_api(page)

        browser.close()
        return products

def main():
    watch_urls = parse_env_list("WATCH_URLS", DEFAULT_WATCH_URLS)
    keywords = parse_env_list("KEYWORDS", DEFAULT_KEYWORDS)

    state = load_state()

    total_found = 0
    total_matched = 0
    new_hits = 0

    summary = [f"📊 PokeWonder scan summary — {now_utc()}"]

    for url in watch_urls:
        try:
            products = fetch_products(url)
            found = len(products)
            total_found += found

            matched = [p for p in products if keyword_match(p["title"], keywords)]
            total_matched += len(matched)

            for p in matched:
                key = p["url"]
                if key not in state:
                    state[key] = int(time.time())
                    new_hits += 1
                    tg_send(f"🆕 NEW MATCH\n{p['title']}\n{p['url']}")

            summary.append(f"- {url}: found={found} matched={len(matched)}")

        except Exception as e:
            summary.append(f"- {url}: ERROR")

    save_state(state)

    summary.append("")
    summary.append(f"Totals: found={total_found} matched={total_matched} new={new_hits}")

    if total_found == 0:
        summary.append("⚠️ No products detected — API structure may have changed.")

    tg_send("\n".join(summary))

if __name__ == "__main__":
    main()
