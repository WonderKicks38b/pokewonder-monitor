import os
import json
import hashlib
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

URLS = {
    "PC UK Home": "https://www.pokemoncenter.com/en-gb",
    "PC UK TCG Category": "https://www.pokemoncenter.com/en-gb/category/trading-card-game",
    "PC UK New Releases": "https://www.pokemoncenter.com/en-gb/new-releases"
}

STATE_FILE = "state.json"


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }
    requests.post(url, data=payload)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def scrape_page(name, url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(8000)

        # Updated selector for Pokémon Center UK
        products = page.locator("div.product-tile, div[data-testid='product-tile']").all()

        titles = []

        for product in products:
            try:
                text = product.inner_text().strip()
                if len(text) > 10:
                    titles.append(text)
            except:
                continue

        browser.close()
        return titles


def main():
    state = load_state()
    summary_lines = []
    total_found = 0

    for name, url in URLS.items():
        titles = scrape_page(name, url)
        found_count = len(titles)
        total_found += found_count

        url_hash = hashlib.md5(url.encode()).hexdigest()

        previous_titles = state.get(url_hash, {}).get("titles", [])
        new_titles = [t for t in titles if t not in previous_titles]

        state[url_hash] = {
            "titles": titles,
            "last_checked": int(datetime.utcnow().timestamp())
        }

        summary_lines.append(
            f"- {name}: found={found_count} new={len(new_titles)}"
        )

        for title in new_titles:
            send_telegram(f"🆕 NEW PRODUCT DETECTED\n{name}\n{title}")

    save_state(state)

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    summary_message = "📊 PokeWonder scan summary — " + timestamp + "\n" + "\n".join(summary_lines)

    send_telegram(summary_message)


if __name__ == "__main__":
    main()
