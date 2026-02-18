import os
import json
import time
import hashlib
import re
import requests
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

STATE_PATH = "state.json"
TG_API = "https://api.telegram.org/bot{token}/{method}"

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
    "tins",
    "reprint",
    "destined rivals",
    "ascended heroes",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def tg_send(text: str):
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets missing (BOT_TOKEN/CHAT_ID). Skipping send.")
        return

    url = TG_API.format(token=token, method="sendMessage")
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, data=payload, timeout=20)
    print("TG send status:", r.status_code)
    if r.status_code != 200:
        print("TG response:", r.text)


def parse_list_env(name: str, fallback: list[str]) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return fallback
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts if parts else fallback


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


def looks_like_queue(page_text: str) -> bool:
    t = page_text.lower()
    return ("virtual queue" in t) or ("estimated wait time" in t) or ("you’re in the virtual queue" in t) or ("you are in the virtual queue" in t)


def keyword_match(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(k.lower() in t for k in keywords)


def extract_products(page, base_url: str):
    """
    Returns list of dicts: {title, url, availability}
    availability: "in_stock" | "out_of_stock" | "unknown"
    """
    products = []

    # Try several selectors because PC changes markup often.
    selectors = [
        "div[data-testid='product-tile']",
        "div.product-tile",
        "li[data-testid='product-tile']",
        "li.product-tile",
        "div[class*='product-tile']",
        "li[class*='product-tile']",
    ]

    tiles = []
    for sel in selectors:
        loc = page.locator(sel)
        count = loc.count()
        if count and count > 0:
            tiles = loc.all()
            break

    # Fallback: anchor pattern
    if not tiles:
        anchors = page.locator("a[href*='/product/']").all()
        for a in anchors:
            try:
                href = a.get_attribute("href") or ""
                href = href.strip()
                if "/product/" not in href:
                    continue
                title = (a.inner_text() or "").strip()
                if not title or len(title) < 3:
                    continue
                url = href if href.startswith("http") else f"https://www.pokemoncenter.com{href}"
                products.append({"title": title, "url": normalize_url(url), "availability": "unknown"})
            except Exception:
                continue
        return dedupe_products(products)

    for tile in tiles:
        try:
            # Find a clickable link inside the tile
            a = tile.locator("a").first
            href = a.get_attribute("href") if a else None
            if not href:
                # sometimes nested
                href = tile.get_attribute("href")

            url = ""
            if href:
                href = href.strip()
                if href.startswith("http"):
                    url = href
                elif href.startswith("/"):
                    url = f"https://www.pokemoncenter.com{href}"
            url = normalize_url(url) if url else ""

            # Extract title
            text = (tile.inner_text() or "").strip()
            # Try to reduce noise
            title = text.split("\n")[0].strip() if text else ""
            if not title or len(title) < 3:
                continue

            # Quick availability heuristics
            lower_block = text.lower()
            if "out of stock" in lower_block or "sold out" in lower_block:
                avail = "out_of_stock"
            elif "add to cart" in lower_block:
                avail = "in_stock"
            else:
                avail = "unknown"

            products.append({"title": title, "url": url or base_url, "availability": avail})
        except Exception:
            continue

    return dedupe_products(products)


def dedupe_products(items):
    seen = set()
    out = []
    for it in items:
        key = (it.get("url", ""), it.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch_page_products(p, name: str, url: str):
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
    page = context.new_page()

    page.goto(url, timeout=90000, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(4000)

    page_text = page.inner_text("body") if page.locator("body").count() else ""
    if looks_like_queue(page_text):
        tg_send(f"🚨 PokeWonder QUEUE DETECTED on {name}\n{url}")

    products = extract_products(page, url)

    browser.close()
    return products, len(page_text)


def main():
    watch_urls = parse_list_env("WATCH_URLS", DEFAULT_WATCH_URLS)
    keywords = parse_list_env("KEYWORDS", DEFAULT_KEYWORDS)

    watch_urls = [normalize_url(u) for u in watch_urls]
    keywords = [k.strip() for k in keywords if k.strip()]

    state = load_state()
    if not isinstance(state, dict):
        state = {}

    tg_send("🟢 PokeWonder live — monitor cycle started.")

    total_found = 0
    total_matched = 0
    new_hits = 0
    restock_hits = 0
    warnings = 0

    sections_summary = []

    with sync_playwright() as p:
        for url in watch_urls:
            # Name section from URL
            if "/category/trading-card-game" in url:
                section_name = "PC UK TCG Category"
            elif "/new-releases" in url:
                section_name = "PC UK New Releases"
            elif url.endswith("/en-gb") or url.endswith("pokemoncenter.com/en-gb"):
                section_name = "PC UK Home"
            elif "/product/" in url:
                section_name = "PC UK Product"
            else:
                section_name = "PC UK Page"

            try:
                products, body_len = fetch_page_products(p, section_name, url)

                found = len(products)
                total_found += found

                # If page returns no products repeatedly, warn (likely markup/API changed)
                if found == 0:
                    warnings += 1

                matched = []
                for prod in products:
                    title = prod.get("title", "").strip()
                    if not title:
                        continue
                    if keyword_match(title, keywords):
                        matched.append(prod)

                total_matched += len(matched)

                # Detect new / restock using state by product URL hash
                for prod in matched:
                    prod_url = prod.get("url", url)
                    prod_title = prod.get("title", "Unknown")
                    prod_avail = prod.get("availability", "unknown")

                    pid = sha1(prod_url or (url + prod_title))
                    prev = state.get(pid)

                    if not prev:
                        # New match
                        state[pid] = {
                            "first_seen": int(time.time()),
                            "last_seen": int(time.time()),
                            "title": prod_title,
                            "url": prod_url,
                            "availability": prod_avail,
                            "last_alert": 0,
                        }
                        new_hits += 1
                        tg_send(f"🆕 NEW MATCH: {prod_title}\n{prod_url}")
                        state[pid]["last_alert"] = int(time.time())
                    else:
                        # Existing
                        prev_av = prev.get("availability", "unknown")
                        prev["last_seen"] = int(time.time())
                        prev["availability"] = prod_avail
                        state[pid] = prev

                        # Restock signal: previously out_of_stock -> now in_stock/unknown (or add-to-cart seen)
                        if prev_av == "out_of_stock" and prod_avail == "in_stock":
                            restock_hits += 1
                            tg_send(f"✅ RESTOCK: {prod_title}\n{prod_url}")
                            prev["last_alert"] = int(time.time())
                            state[pid] = prev

                sections_summary.append(
                    f"- {section_name}: found={found} matched={len(matched)} new={sum(1 for _ in [])} restock_hits={0}"
                )

            except Exception as e:
                warnings += 1
                tg_send(f"⚠️ PokeWonder error on {url}\n{repr(e)}")
                sections_summary.append(f"- {section_name}: ERROR")

    # Save state
    save_state(state)

    # Summary message (always)
    summary_lines = [
        f"📊 PokeWonder scan summary — {now_utc_str()}",
        *sections_summary,
        "",
        f"Totals: found={total_found} matched={total_matched} new={new_hits} restock_hits={restock_hits}",
    ]

    if warnings > 0 and total_found == 0:
        summary_lines.append("⚠️ No products detected — possible structure change or heavy JS/API rendering.")

    tg_send("\n".join(summary_lines))


if __name__ == "__main__":
    main()
