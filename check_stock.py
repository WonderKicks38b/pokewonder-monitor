import os
import json
import time
import random
import hashlib
import requests
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from playwright_stealth import stealth_sync


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

STATE_FILE = "state.json"

SOURCES = [
    ("PC UK Home", "https://www.pokemoncenter.com/en-gb"),
    ("PC UK TCG Category", "https://www.pokemoncenter.com/en-gb/category/trading-card-game"),
    ("PC UK New Releases", "https://www.pokemoncenter.com/en-gb/new-releases"),
]

# What we care about (keywords)
KEYWORDS = [
    "etb", "elite trainer", "booster box", "display",
    "premium collection", "collection", "bundle",
    "special", "reprint",
    "destined rivals", "ascended heroes",
]

# Safety cap: don’t alert on huge spam if site goes wild
MAX_LINKS_PER_SOURCE = 250


def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing BOT_TOKEN or CHAT_ID; cannot send Telegram.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=25)
        # If it fails, print for Actions logs
        if r.status_code != 200:
            print("Telegram send failed:", r.status_code, r.text)
    except Exception as e:
        print("Telegram exception:", str(e))


def looks_like_bot_wall(html: str) -> bool:
    h = (html or "").lower()
    # common Cloudflare / bot-wall signals
    signals = [
        "cloudflare",
        "attention required",
        "verify you are human",
        "checking your browser",
        "access denied",
        "request blocked",
        "/cdn-cgi/",
    ]
    return any(s in h for s in signals)


def extract_product_links(page, base_url: str):
    # Pull all anchor hrefs and filter down to product pages
    hrefs = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
    )

    links = []
    for href in hrefs:
        if href.startswith("/"):
            full = "https://www.pokemoncenter.com" + href
        elif href.startswith("http"):
            full = href
        else:
            continue

        # UK product pages often look like /en-gb/product/...
        if "/en-gb/product/" in full:
            links.append(full)

    # de-dup while keeping order
    seen = set()
    uniq = []
    for l in links:
        if l not in seen:
            seen.add(l)
            uniq.append(l)

    return uniq[:MAX_LINKS_PER_SOURCE]


def keyword_match(url_or_text: str) -> bool:
    t = (url_or_text or "").lower()
    return any(k in t for k in KEYWORDS)


def run():
    # Basic sanity: if these are missing, still run but you'll see logs
    if BOT_TOKEN:
        print("BOT_TOKEN length:", len(BOT_TOKEN))
    else:
        print("BOT_TOKEN missing")

    if CHAT_ID:
        print("CHAT_ID set")
    else:
        print("CHAT_ID missing")

    state = load_state()

    # Start-of-run ping (keeps you confident it’s alive)
    tg_send("🟩 PokeWonder live — monitor cycle started.")

    sources_scanned = 0
    total_links_found = 0
    matched = 0
    new_listings = 0
    restock_hits = 0
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # headed via Xvfb
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 768},
        )

        # More realistic headers
        context.set_extra_http_headers({
            "Accept-Language": "en-GB,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        })

        page = context.new_page()
        stealth_sync(page)

        # Gentle human-like behavior
        page.set_default_timeout(45000)

        for label, url in SOURCES:
            sources_scanned += 1

            try:
                # random delay between sources
                time.sleep(random.uniform(1.0, 2.5))

                resp = page.goto(url, wait_until="domcontentloaded")
                # Some pages render late; allow a bit
                time.sleep(random.uniform(2.0, 4.0))

                html = page.content()

                if looks_like_bot_wall(html):
                    errors.append(f"- {url} (BOT WALL)")
                    continue

                # Extract product links
                links = extract_product_links(page, url)
                total_links_found += len(links)

                # Evaluate changes vs state
                # We track by hash key of URL; value stores last_seen and last_alert_ts
                for link in links:
                    key = sha1(link)
                    prev = state.get(key)

                    is_new = prev is None
                    is_kw = keyword_match(link)

                    if is_kw:
                        matched += 1

                    # update last seen always
                    if prev is None:
                        state[key] = {"url": link, "first_seen": int(time.time()), "last_seen": int(time.time()), "last_alert_ts": 0}
                        if is_kw:
                            new_listings += 1
                            # New + matched -> alert
                            tg_send(f"🆕 NEW MATCH: {link}")
                            state[key]["last_alert_ts"] = int(time.time())
                    else:
                        prev["last_seen"] = int(time.time())
                        state[key] = prev

                # Optional: restock detection requires per-product checks; not doing deep checks on GitHub
                # (keeps it light & less botty)
            except PWTimeoutError:
                errors.append(f"- {url} (TIMEOUT)")
            except Exception as e:
                errors.append(f"- {url} (ERROR: {str(e)[:90]})")

        browser.close()

    save_state(state)

    # Summary message
    lines = []
    lines.append(f"📊 PokeWonder scan summary — {now_utc_str()}")
    lines.append(f"- Sources scanned: {sources_scanned}")
    lines.append(f"- Product links detected: {total_links_found}")
    lines.append(f"- Matched (keywords/sets): {matched}")
    lines.append(f"- New listings this run: {new_listings}")
    lines.append(f"- Restock hits this run: {restock_hits}")

    if errors:
        lines.append("")
        lines.append("⚠️ Source errors:")
        lines.extend(errors)
        lines.append("")
        lines.append("If you see BOT WALL/403 repeatedly: GitHub IPs are being blocked (datacenter).")

    if total_links_found == 0:
        lines.append("")
        lines.append("⚠️ No products detected — likely Cloudflare soft block or JS grid inaccessible.")

    tg_send("\n".join(lines))


if __name__ == "__main__":
    run()
