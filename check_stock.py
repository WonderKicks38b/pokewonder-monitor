import os
import re
import json
import time
import hashlib
import datetime
from typing import Dict, List, Tuple, Set

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import requests


# -----------------------------
# CONFIG (env / defaults)
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Optional filters (GitHub Secrets recommended)
WATCH_KEYWORDS = os.getenv("WATCH_KEYWORDS", "").strip()   # comma-separated phrases
WATCH_URLS = os.getenv("WATCH_URLS", "").strip()           # comma-separated product/category URLs
SET_NAMES = os.getenv("SET_NAMES", "").strip()             # comma-separated set names

ALERT_ONLY_ON_MATCH = os.getenv("ALERT_ONLY_ON_MATCH", "false").strip().lower() in ("1", "true", "yes", "y")
SEND_SUMMARY = os.getenv("SEND_SUMMARY", "true").strip().lower() in ("1", "true", "yes", "y")

# Scan sources (fallback)
DEFAULT_SCAN_PAGES = [
    "https://www.pokemoncenter.com/en-gb",
    "https://www.pokemoncenter.com/en-gb/category/trading-card-game",
    "https://www.pokemoncenter.com/en-gb/new-releases",
]

STATE_PATH = "state.json"


# -----------------------------
# Helpers
# -----------------------------
def now_utc_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def load_state() -> Dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)

def parse_csv_list(val: str) -> List[str]:
    if not val:
        return []
    parts = [p.strip() for p in val.split(",")]
    return [p for p in parts if p]

def normalise_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def classify(title: str, set_names: List[str]) -> Tuple[str, str]:
    t = normalise_text(title)

    # Product type buckets (simple + useful)
    if "elite trainer box" in t or "etb" in t:
        ptype = "ETB"
    elif "booster box" in t:
        ptype = "Booster Box"
    elif "booster bundle" in t:
        ptype = "Booster Bundle"
    elif "premium collection" in t or "collection" in t:
        ptype = "Premium Collection"
    elif "tin" in t:
        ptype = "Tin"
    elif "mini tin" in t:
        ptype = "Mini Tin"
    elif "blister" in t:
        ptype = "Blister"
    elif "special" in t or "holiday" in t:
        ptype = "Special Set"
    elif "reprint" in t:
        ptype = "Reprint"
    else:
        ptype = "Other"

    # Set detection (from env list)
    detected_set = ""
    for s in set_names:
        if normalise_text(s) and normalise_text(s) in t:
            detected_set = s
            break

    return ptype, detected_set

def keyword_match(title: str, keywords: List[str], set_names: List[str]) -> bool:
    t = normalise_text(title)
    for kw in keywords:
        if normalise_text(kw) and normalise_text(kw) in t:
            return True
    # also allow set-name matching to count as a match
    for s in set_names:
        if normalise_text(s) and normalise_text(s) in t:
            return True
    return False

def tg_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

def telegram_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secrets missing: BOT_TOKEN or CHAT_ID.")
        return
    try:
        r = requests.post(
            tg_api_url("sendMessage"),
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code != 200:
            print("Telegram sendMessage failed:", r.status_code, r.text)
    except Exception as e:
        print("Telegram exception:", repr(e))


# -----------------------------
# Playwright scraping (DOM-based)
# -----------------------------
def extract_product_links_from_page(page) -> Set[str]:
    """
    Extract product links from visible DOM.
    This avoids Next.js BUILD_ID / _next/data fragility.
    """
    links = set()

    # Gather all anchors and filter for /en-gb/product/
    anchors = page.locator("a[href]")
    count = anchors.count()
    for i in range(min(count, 2000)):
        href = anchors.nth(i).get_attribute("href")
        if not href:
            continue
        if "/en-gb/product/" in href:
            # Normalise absolute URL
            if href.startswith("http"):
                url = href
            else:
                url = "https://www.pokemoncenter.com" + href
            # Strip URL fragments/params that can vary
            url = url.split("#")[0]
            url = url.split("?")[0]
            links.add(url)

    return links

def check_stock_on_product_page(page, url: str) -> Tuple[str, bool, str]:
    """
    Returns: (title, in_stock, reason)
    """
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # Some pages lazy-load; give a brief settle
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    # Title
    title = ""
    try:
        h1 = page.locator("h1").first
        if h1.count() > 0:
            title = h1.inner_text().strip()
    except Exception:
        title = ""

    if not title:
        title = url.split("/")[-1].replace("-", " ")

    # Stock signals (site can vary; we check multiple)
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        body_text = ""

    # Common signals
    out_of_stock = ("out of stock" in body_text) or ("currently unavailable" in body_text)
    add_to_cart_visible = False
    add_to_cart_enabled = False

    # Try common button patterns
    button_candidates = [
        "button:has-text('Add to cart')",
        "button:has-text('Add to Cart')",
        "button:has-text('Add to basket')",
        "button:has-text('Add to Basket')",
        "button:has-text('Add')",
    ]

    for sel in button_candidates:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0:
                add_to_cart_visible = True
                disabled = btn.is_disabled()
                add_to_cart_enabled = (not disabled)
                # If it looks good, stop checking
                if add_to_cart_enabled:
                    break
        except Exception:
            continue

    # Decide stock
    if add_to_cart_visible and add_to_cart_enabled and not out_of_stock:
        return title, True, "Add-to-cart enabled"
    if out_of_stock:
        return title, False, "Out of stock text present"
    if add_to_cart_visible and not add_to_cart_enabled:
        return title, False, "Add-to-cart disabled"
    return title, False, "No clear stock signal"

def run_monitor() -> None:
    # Prepare filters
    keywords = parse_csv_list(WATCH_KEYWORDS)
    set_names = parse_csv_list(SET_NAMES)

    # Good default set names (you can override/add via SET_NAMES secret)
    if not set_names:
        set_names = [
            "Destined Rivals",
            "Ascended Heroes",
            # Add more whenever you want:
            "Prismatic Evolutions",
            "Surging Sparks",
            "Temporal Forces",
            "Paldean Fates",
            "151",
        ]

    # Pages to scan (allow override via WATCH_URLS secret)
    scan_pages = parse_csv_list(WATCH_URLS) or DEFAULT_SCAN_PAGES

    state = load_state()
    if "products" not in state:
        state["products"] = {}  # url -> metadata

    # Telegram heartbeat (optional)
    telegram_send(f"🟢 PokeWonder operational — {datetime.date.today().isoformat()}")

    found_links: Set[str] = set()
    errors: List[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # 1) Collect product links from scan pages
        for src in scan_pages:
            try:
                page.goto(src, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass

                links = extract_product_links_from_page(page)
                found_links |= links
                print(f"[SCAN] {src} -> links={len(links)}")
            except Exception as e:
                err = f"{src}: ERROR ({repr(e)})"
                print(err)
                errors.append(err)

        # Safety cap (don’t hammer)
        found_list = sorted(list(found_links))[:250]

        # 2) Check product pages
        new_count = 0
        restock_hits = 0
        matched = 0
        in_stock_count = 0

        for url in found_list:
            prev = state["products"].get(url, {})
            prev_in_stock = bool(prev.get("in_stock", False))
            prev_seen = bool(prev)

            try:
                title, in_stock, reason = check_stock_on_product_page(page, url)
            except Exception as e:
                print(f"[PRODUCT] {url} -> ERROR {repr(e)}")
                continue

            # Matching logic
            is_match = keyword_match(title, keywords, set_names) if (keywords or set_names) else True
            if is_match:
                matched += 1

            # Apply "only alert on match" option
            should_alert_for_item = (is_match or (not ALERT_ONLY_ON_MATCH))

            ptype, set_hit = classify(title, set_names)

            # Update state
            state["products"][url] = {
                "title": title,
                "in_stock": in_stock,
                "ptype": ptype,
                "set": set_hit,
                "last_seen_ts": int(time.time()),
            }

            if in_stock:
                in_stock_count += 1

            # New product (first time seen)
            if not prev_seen:
                new_count += 1
                if should_alert_for_item:
                    telegram_send(
                        "🆕 NEW LISTING\n"
                        f"- {title}\n"
                        f"- Type: {ptype}" + (f" | Set: {set_hit}" if set_hit else "") + "\n"
                        f"- Stock: {'IN STOCK ✅' if in_stock else 'Unknown/Out ❌'}\n"
                        f"- {url}"
                    )

            # Restock event
            if (not prev_in_stock) and in_stock:
                restock_hits += 1
                if should_alert_for_item:
                    telegram_send(
                        "🔥 RESTOCK / IN STOCK\n"
                        f"- {title}\n"
                        f"- Type: {ptype}" + (f" | Set: {set_hit}" if set_hit else "") + "\n"
                        f"- {url}"
                    )

        # Close browser
        context.close()
        browser.close()

    # 3) Summary message
    if SEND_SUMMARY:
        summary = (
            f"📊 PokeWonder scan summary — {now_utc_str()}\n"
            f"- Sources scanned: {len(scan_pages)}\n"
            f"- Products found (links): {len(found_links)}\n"
            f"- Products checked (cap): {min(len(found_links), 250)}\n"
            f"- Matched (keywords/sets): {matched}\n"
            f"- In stock now: {in_stock_count}\n"
            f"- New listings this run: {new_count}\n"
            f"- Restock hits this run: {restock_hits}\n"
        )
        if errors:
            summary += "\n⚠️ Source errors:\n" + "\n".join(f"- {e}" for e in errors[:6])
        telegram_send(summary)

    save_state(state)


if __name__ == "__main__":
    run_monitor()
