import os
import re
import json
import time
import hashlib
import datetime
import requests
from playwright.sync_api import sync_playwright

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

STATE_FILE = "state.json"

SOURCES = [
    "https://www.pokemoncenter.com/en-gb/category/trading-card-game",
    "https://www.pokemoncenter.com/en-gb/new-releases",
]

def tg(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing Telegram credentials")
        return
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": True},
        timeout=15,
    )

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen": {}, "last_operational_date": None}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def hash_id(url):
    return hashlib.sha256(url.encode()).hexdigest()[:16]

def is_bot_wall(html):
    html = html.lower()
    patterns = [
        "verify you are human",
        "cloudflare",
        "attention required",
        "access denied",
        "captcha",
    ]
    return any(p in html for p in patterns)

def extract_links_from_html(html):
    return set(
        re.findall(r'https://www\.pokemoncenter\.com/en-gb/product/[^"\']+', html)
    )

def main():
    state = load_state()
    today = datetime.date.today().isoformat()

    if state.get("last_operational_date") != today:
        tg(f"🟢 PokeWonder operational — {today}")
        state["last_operational_date"] = today

    tg("🟩 PokeWonder live — monitor cycle started.")

    total_links = 0
    all_links = set()
    error_sources = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-GB",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36"
        )
        page = context.new_page()

        for src in SOURCES:
            try:
                page.goto(src, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                html = page.content()

                if is_bot_wall(html):
                    error_sources.append(src + " (BOT WALL)")
                    continue

                # Extract from DOM
                anchors = page.locator("a[href*='/product/']")
                count = anchors.count()

                links = set()
                for i in range(min(count, 500)):
                    href = anchors.nth(i).get_attribute("href")
                    if href:
                        if href.startswith("/"):
                            href = "https://www.pokemoncenter.com" + href
                        links.add(href.split("?")[0])

                # Fallback raw HTML scan
                if not links:
                    links = extract_links_from_html(html)

                total_links += len(links)
                all_links |= links

            except Exception as e:
                error_sources.append(src + " (ERROR)")
                continue

        browser.close()

    # Diagnostic output
    utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    msg = (
        f"📊 PokeWonder scan summary — {utc_now}\n"
        f"- Sources scanned: {len(SOURCES)}\n"
        f"- Product links detected: {len(all_links)}\n"
    )

    if error_sources:
        msg += "\n- Source errors:\n"
        for e in error_sources:
            msg += f"  - {e}\n"

    if len(all_links) == 0:
        msg += "\n⚠ No products detected. Likely:\n"
        msg += "- Cloudflare soft block\n"
        msg += "- JS-rendered grid not accessible\n"
        msg += "- Structure changed\n"

    tg(msg)
    save_state(state)

if __name__ == "__main__":
    main()
