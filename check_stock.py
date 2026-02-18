import os
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

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message
    }
    requests.post(url, data=data)

def scrape_page(name, url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=60000)
        page.wait_for_timeout(5000)

        products = page.locator("a[href*='/product/']").all()
        titles = []

        for product in products:
            text = product.inner_text().strip()
            if text:
                titles.append(text)

        browser.close()
        return titles

def main():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = f"📊 PokeWonder scan summary — {now}\n"

    total_found = 0

    for name, url in URLS.items():
        titles = scrape_page(name, url)
        count = len(titles)
        total_found += count
        summary += f"- {name}: found={count}\n"

    if total_found == 0:
        summary += "\n⚠️ No products detected — possible structure change."
    else:
        summary += f"\n✅ Total products detected: {total_found}"

    send_telegram(summary)

if __name__ == "__main__":
    main()
