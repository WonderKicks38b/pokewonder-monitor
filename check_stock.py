import os
import json
import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SOURCES = [
    "https://www.pokemoncenter.com/en-gb/category/trading-card-game",
    "https://www.pokemoncenter.com/en-gb/new-releases"
]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Telegram error:", e)

def fetch_page(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            locale="en-GB"
        )

        page = context.new_page()
        stealth_sync(page)

        page.goto(url, timeout=60000)
        page.wait_for_timeout(5000)

        html = page.content()

        browser.close()
        return html

def main():
    results = []

    for source in SOURCES:
        try:
            html = fetch_page(source)

            if "Add to Cart" in html or "add-to-cart" in html:
                results.append(source)

        except Exception as e:
            print("Error:", e)

    if results:
        message = "🔥 PokeWonder Alert — Possible Stock Live:\n\n"
        message += "\n".join(results)
        send_telegram(message)
    else:
        send_telegram("📊 Scan complete — no stock detected.")

if __name__ == "__main__":
    main()
