import os
import re
import json
import time
import hashlib
import datetime
from typing import Dict, List, Set, Tuple, Optional

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================
# CONFIG (defaults; can be overridden by GitHub Secrets)
# ============================================================

DEFAULT_SOURCES = [
    "https://www.pokemoncenter.com/en-gb",
    "https://www.pokemoncenter.com/en-gb/category/trading-card-game",
    "https://www.pokemoncenter.com/en-gb/new-releases",
]

STATE_FILE = "state.json"

# Secrets (optional):
# BOT_TOKEN (required)
# CHAT_ID   (required)
# TARGET_URLS  (optional) -> comma-separated URLs (product pages or listing pages)
# KEYWORDS     (optional) -> comma-separated keywords, e.g. "etb,booster box,elite trainer"
# SET_NAMES    (optional) -> comma-separated set names, e.g. "destined rivals,ascended heroes"
# MAX_PRODUCTS (optional) -> int cap for how many product pages to open per run (default 60)
# HEADLESS     (optional) -> "true"/"false" (default true)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

TARGET_URLS = os.getenv("TARGET_URLS", "").strip()
KEYWORDS = os.getenv("KEYWORDS", "").strip()
SET_NAMES = os.getenv("SET_NAMES", "").strip()

MAX_PRODUCTS = int(os.getenv("MAX_PRODUCTS", "60").strip() or "60")
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() != "false"

# ============================================================
# TELEGRAM
# ============================================================

def tg_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Missing BOT_TOKEN or CHAT_ID. Telegram disabled.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=20)
        print("Telegram send status:", r.status_code)
        if r.status_code != 200:
            print("Telegram error:", r.text[:500])
    except Exception as e:
        print("Telegram exception:", repr(e))


# ============================================================
# STATE
# ============================================================

def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"seen": {}, "last_operational_date": None}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"seen": {}, "last_operational_date": None}
        data.setdefault("seen", {})
        data.setdefault("last_operational_date", None)
        return data
    except Exception:
        return {"seen": {}, "last_operational_date": None}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def stable_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# ============================================================
# PARSING / MATCHING
# ============================================================

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def split_csv(s: str) -> List[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


KW_LIST = [norm(x) for x in split_csv(KEYWORDS)]
SET_LIST = [norm(x) for x in split_csv(SET_NAMES)]


def matches_interest(title: str, url: str) -> bool:
    t = norm(title)
    u = norm(url)

    # If no filters provided, match everything.
    if not KW_LIST and not SET_LIST:
        return True

    for k in KW_LIST:
        if k and (k in t or k in u):
            return True

    for s in SET_LIST:
        if s and (s in t or s in u):
            return True

    return False


# ============================================================
# PLAYWRIGHT HELPERS
# ============================================================

def is_bot_wall(html: str) -> bool:
    h = (html or "").lower()
    patterns = [
        "verify you are human",
        "access denied",
        "attention required",
        "cloudflare",
        "captcha",
        "bot detection",
        "challenge",
        "not authorized",
    ]
    return any(p in h for p in patterns)


def build_sources() -> List[str]:
    if TARGET_URLS:
        # Allow user-defined list; can include listing pages and product pages
        return [u.strip() for u in TARGET_URLS.split(",") if u.strip()]
    return DEFAULT_SOURCES


def extract_candidate_product_links(page) -> Set[str]:
    """
    Extract product-like links from the rendered DOM.
    Pokémon Center typically uses /en-gb/product/ but we also capture variants.
    """
    links = set()

    anchors = page.locator("a[href]")
    count = anchors.count()

    for i in range(min(count, 3000)):
        href = anchors.nth(i).get_attribute("href")
        if not href:
            continue

        # Normalize
        if href.startswith("/"):
            href_full = "https://www.pokemoncenter.com" + href
        else:
            href_full = href

        href_full = href_full.split("#")[0].split("?")[0]

        # Primary pattern
        if "/en-gb/product/" in href_full:
            links.add(href_full)
            continue

        # Fallback patterns sometimes appear as /product/ without locale
        if "/product/" in href_full and "pokemoncenter.com" in href_full:
            links.add(href_full)
            continue

    return links


def best_wait_for_content(page) -> None:
    """
    We try multiple waits because PC can be JS-heavy or guarded.
    """
    # Basic network settle
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    # Try common product grid/container patterns
    selectors = [
        'a[href*="/product/"]',
        'a[href*="/en-gb/product/"]',
        "[data-testid*='product']",
        "[class*='product']",
    ]
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=8000)
            return
        except Exception:
            continue

    # If nothing matched, we still proceed (diagnostics will catch)
    return


def fetch_page_html(page) -> str:
    try:
        return page.content()
    except Exception:
        return ""


# ============================================================
# PRODUCT CHECK (in-stock detection)
# ============================================================

def detect_stock_from_product_page(page) -> Tuple[bool, Dict[str, bool]]:
    """
    Returns (in_stock_now, signals)
    signals keys: out_of_stock, add_to_cart
    """
    html = (fetch_page_html(page) or "").lower()

    # Very common signals
    out_markers = [
        "sold out",
        "out of stock",
        "currently unavailable",
        "unavailable",
    ]
    in_markers = [
        "add to cart",
        "add-to-cart",
    ]

    out_of_stock = any(m in html for m in out_markers)
    add_to_cart = any(m in html for m in in_markers)

    in_stock_now = (add_to_cart and not out_of_stock)

    return in_stock_now, {"out_of_stock": out_of_stock, "add_to_cart": add_to_cart}


def safe_title(page) -> str:
    try:
        t = page.title()
        return t.strip() if t else ""
    except Exception:
        return ""


# ============================================================
# MAIN RUN
# ============================================================

def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ BOT_TOKEN or CHAT_ID missing. Add them in GitHub → Settings → Secrets and variables → Actions.")
        return

    state = load_state()
    today = datetime.date.today().isoformat()

    # Daily operational ping (once per date)
    if state.get("last_operational_date") != today:
        tg_send(f"🟢 PokeWonder operational — {today}")
        state["last_operational_date"] = today

    sources = build_sources()

    tg_send("🟩 PokeWonder live — monitor cycle started.")

    # Summary counters
    total_sources = len(sources)
    found_links_total = 0
    checked_products = 0
    matched_products = 0
    in_stock_now_count = 0
    new_listings = 0
    restock_hits = 0
    error_sources: List[str] = []

    # Alerts (collected then sent)
    new_alerts: List[str] = []
    restock_alerts: List[str] = []
    instock_alerts: List[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)

        context = browser.new_context(
            locale="en-GB",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        page = context.new_page()

        # Collect links from each source
        all_product_links: Set[str] = set()

        for src in sources:
            try:
                page.goto(src, wait_until="domcontentloaded", timeout=45000)
                best_wait_for_content(page)
                html = fetch_page_html(page)

                if is_bot_wall(html):
                    error_sources.append(src + " (BOT/WALL)")
                    continue

                # If source itself is a product page, include it
                if "/product/" in src:
                    all_product_links.add(src.split("#")[0].split("?")[0])

                links = extract_candidate_product_links(page)
                found_links_total += len(links)
                all_product_links |= links

            except PlaywrightTimeoutError:
                error_sources.append(src + " (TIMEOUT)")
            except Exception as e:
                print("Source error:", src, repr(e))
                error_sources.append(src + " (ERROR)")

        # Hard stop if nothing found (but give diagnostics)
        if not all_product_links:
            msg = (
                f"📊 PokeWonder scan summary — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"- Sources scanned: {total_sources}\n"
                f"- Products found (links): 0\n"
                f"- Products checked (cap): 0\n"
            )
            if error_sources:
                msg += "\n- Errors:\n" + "\n".join([f"  - {x}" for x in error_sources])
            msg += "\n⚠ No product links detected — likely bot wall / structure change / heavy JS."
            tg_send(msg)
            save_state(state)
            browser.close()
            return

        # Cap product checks per run
        product_links_list = sorted(all_product_links)[:MAX_PRODUCTS]

        # Visit product pages to detect stock + titles
        for url in product_links_list:
            checked_products += 1
            pid = stable_id(url)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                best_wait_for_content(page)

                html = fetch_page_html(page)
                if is_bot_wall(html):
                    continue

                title = safe_title(page)
                # If title is empty, still try something
                if not title:
                    title = "Product"

                # Apply interest filters
                if not matches_interest(title, url):
                    continue

                matched_products += 1

                in_stock_now, signals = detect_stock_from_product_page(page)
                if in_stock_now:
                    in_stock_now_count += 1

                seen_entry = state["seen"].get(pid, {})
                prev_seen = bool(seen_entry)
                prev_in_stock = bool(seen_entry.get("in_stock_now", False))

                # Determine NEW listing (first time we ever see this product)
                if not prev_seen:
                    state["seen"][pid] = {
                        "url": url,
                        "title": title,
                        "first_seen_utc": int(time.time()),
                        "in_stock_now": in_stock_now,
                        "signals": signals,
                        "last_seen_utc": int(time.time()),
                        "last_alert_utc": 0,
                    }
                    new_listings += 1
                    new_alerts.append(f"🆕 NEW: {title}\n{url}")
                else:
                    # Update existing
                    state["seen"][pid]["title"] = title
                    state["seen"][pid]["url"] = url
                    state["seen"][pid]["signals"] = signals
                    state["seen"][pid]["last_seen_utc"] = int(time.time())

                    # Restock hit (was not in stock before, now is)
                    if (not prev_in_stock) and in_stock_now:
                        restock_hits += 1
                        restock_alerts.append(f"🔥 RESTOCK: {title}\n{url}")

                    # In-stock now (informational; optional)
                    if in_stock_now and prev_in_stock:
                        # don’t spam this every run, only if last alert was long ago
                        last_alert = int(state["seen"][pid].get("last_alert_utc", 0) or 0)
                        if int(time.time()) - last_alert > 6 * 60 * 60:  # 6h
                            instock_alerts.append(f"✅ In stock: {title}\n{url}")

                    state["seen"][pid]["in_stock_now"] = in_stock_now

            except PlaywrightTimeoutError:
                continue
            except Exception as e:
                print("Product error:", url, repr(e))
                continue

        browser.close()

    # Send alerts (new + restock first)
    for a in new_alerts[:10]:
        tg_send(a)

    for a in restock_alerts[:10]:
        tg_send(a)

    for a in instock_alerts[:5]:
        tg_send(a)

    # Build summary
    utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = (
        f"📊 PokeWonder scan summary — {utc_now}\n"
        f"- Sources scanned: {total_sources}\n"
        f"- Products found (links): {len(all_product_links)}\n"
        f"- Products checked (cap): {min(len(all_product_links), MAX_PRODUCTS)}\n"
        f"- Matched (keywords/sets): {matched_products}\n"
        f"- In stock now: {in_stock_now_count}\n"
        f"- New listings this run: {new_listings}\n"
        f"- Restock hits this run: {restock_hits}\n"
    )

    if error_sources:
        summary += "\n- Source errors:\n" + "\n".join([f"  - {x}" for x in error_sources])

    tg_send(summary)

    save_state(state)


if __name__ == "__main__":
    main()
