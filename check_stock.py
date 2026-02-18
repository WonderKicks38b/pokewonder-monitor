import os
import json
import time
import re
import hashlib
import requests
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# =====================
# REQUIRED SECRETS
# =====================
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
CHAT_ID = os.environ["CHAT_ID"].strip()

STATE_FILE = "state.json"

# =====================
# TARGET PAGES
# =====================
PC_UK_HOME = "https://www.pokemoncenter.com/en-gb"
PC_UK_TCG = "https://www.pokemoncenter.com/en-gb/category/trading-card-game"
PC_UK_NEW = "https://www.pokemoncenter.com/en-gb/new-releases"

# =====================
# DEFAULT KEYWORDS (Option 5)
# =====================
DEFAULT_KEYWORDS = [
    "elite trainer box", "etb",
    "booster box", "booster display",
    "booster bundle",
    "booster pack",
    "build & battle",
    "collection box",
    "premium collection",
    "trainer toolkit",
]

# Optional override via repo secret KEYWORDS (comma-separated)
# Example: elite trainer box,booster box,151,prismatic evolutions
KEYWORDS_ENV = os.environ.get("KEYWORDS", "").strip()
if KEYWORDS_ENV:
    KEYWORDS = [k.strip().lower() for k in KEYWORDS_ENV.split(",") if k.strip()]
else:
    KEYWORDS = [k.lower() for k in DEFAULT_KEYWORDS]

# Rate limits (reduce spam)
DAILY_ALIVE_KEY = "daily_alive_utc"
ERROR_RE_ALERT_SECONDS = 30 * 60
QUEUE_RE_ALERT_SECONDS = 60 * 60
RESTOCK_COOLDOWN_SECONDS = 5 * 60
NEW_ITEM_COOLDOWN_SECONDS = 10 * 60

WAITTIME_THRESHOLDS_HOURS = [6, 3, 1]  # alerts when <= 6h, <= 3h, <= 1h

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

QUEUE_TEXT_HINTS = [
    "virtual queue",
    "you're in the virtual queue",
    "estimated wait time",
    "keep this window open",
    "do not refresh",
    "lose your place in line",
    "high volume of requests",
    "waiting room",
]
QUEUE_URL_HINTS = ["queue-it", "virtual-queue", "queue", "waiting", "line"]

BLOCK_TEXT_HINTS = [
    "access denied",
    "are you a robot",
    "unusual traffic",
    "captcha",
    "verify you are human",
    "temporarily unavailable",
    "service unavailable",
    "request blocked",
]

# =====================
# BASIC HELPERS
# =====================
def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def stable_key(*parts: str) -> str:
    s = "|".join(parts)
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:16]

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def tg_send(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    print("Telegram sendMessage:", r.status_code, r.text[:200])

def should_realert(state: dict, key: str, now_ts: int, window_seconds: int) -> bool:
    last_ts = int(state.get(key, {}).get("last_alert_ts", 0) or 0)
    return (now_ts - last_ts) > window_seconds

def parse_wait_time_seconds(text: str) -> Optional[int]:
    t = " ".join((text or "").split())
    m = re.search(r"\b(\d{1,2}):(\d{2}):(\d{2})\b", t)
    if m:
        h = int(m.group(1)); mm = int(m.group(2)); ss = int(m.group(3))
        return h * 3600 + mm * 60 + ss
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        mm = int(m.group(1)); ss = int(m.group(2))
        return mm * 60 + ss
    return None

def detect_queue(final_url: str, body_text: str) -> Tuple[bool, str]:
    u = (final_url or "").lower()
    t = (body_text or "").lower()
    if any(h in u for h in QUEUE_URL_HINTS):
        return True, "URL matched queue hints"
    if any(h in t for h in QUEUE_TEXT_HINTS):
        return True, "Text matched queue hints"
    return False, "No queue hints"

def detect_block(body_text: str) -> Tuple[bool, str]:
    t = (body_text or "").lower()
    for hint in BLOCK_TEXT_HINTS:
        if hint in t:
            return True, f"Block hint: '{hint}'"
    return False, "No block hints"

def text_has_keywords(text: str) -> bool:
    tt = (text or "").lower()
    return any(k in tt for k in KEYWORDS)

# =====================
# PLAYWRIGHT FETCH + EXTRACT
# =====================
def fetch_page(url: str) -> Dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        page = context.new_page()

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeoutError:
            pass

        final_url = page.url

        body_text = ""
        try:
            body_text = page.inner_text("body")
        except Exception:
            body_text = ""

        html = ""
        try:
            html = page.content()
        except Exception:
            html = ""

        # Extract product-like items from the DOM robustly:
        # - find <a href*="/product/">
        # - capture href + link text + nearby container text (best-effort)
        items = page.evaluate(
            """
            () => {
              function norm(s){
                return (s || "").replace(/\\s+/g," ").trim();
              }

              const anchors = Array.from(document.querySelectorAll('a[href*="/product/"]'));
              const results = [];
              const seen = new Set();

              for (const a of anchors) {
                const href = a.getAttribute("href") || "";
                if (!href.includes("/product/")) continue;

                // build absolute url
                const url = new URL(href, window.location.origin).toString();

                // de-dupe
                const key = url;
                if (seen.has(key)) continue;
                seen.add(key);

                const linkText = norm(a.innerText);

                // attempt to find a reasonable card/container
                let container = a.closest('li, article, div');
                let containerText = "";
                if (container) {
                  containerText = norm(container.innerText);
                  // keep it bounded
                  if (containerText.length > 500) containerText = containerText.slice(0, 500);
                }

                results.push({
                  url,
                  linkText,
                  containerText
                });
              }

              return results;
            }
            """
        )

        context.close()
        browser.close()

        return {
            "final_url": final_url,
            "body_text": body_text,
            "html": html,
            "items": items or [],
        }

# =====================
# INTELLIGENCE: classify + infer stock
# =====================
def infer_in_stock(text_blob: str) -> Optional[bool]:
    """
    Very safe inference from visible text around the product card.
    Returns:
      True  -> strong in-stock signal
      False -> strong out-of-stock signal
      None  -> unknown
    """
    t = (text_blob or "").lower()

    # Strong OOS signals
    if "out of stock" in t or "sold out" in t or "unavailable" in t:
        return False

    # Strong in-stock / purchase signals
    if "add to cart" in t or "add to basket" in t:
        return True

    # Sometimes just "add" appears
    if re.search(r"\badd to\b", t):
        return True

    return None

def product_kind(text_blob: str) -> str:
    t = (text_blob or "").lower()
    if "elite trainer box" in t or re.search(r"\betb\b", t):
        return "ETB"
    if "booster box" in t or "booster display" in t:
        return "Booster Box"
    if "booster bundle" in t:
        return "Booster Bundle"
    if "booster pack" in t or "booster" in t:
        return "Booster"
    if "build & battle" in t:
        return "Build & Battle"
    if "collection" in t:
        return "Collection"
    return "TCG Item"

# =====================
# ALERTS
# =====================
def daily_alive(state: dict, now_ts: int) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get(DAILY_ALIVE_KEY) != today:
        tg_send(f"🟢 PokeWonder operational — {today}")
        state[DAILY_ALIVE_KEY] = today
        state.setdefault("_meta", {})["last_alive_ts"] = now_ts

def queue_alerts(state: dict, now_ts: int, name: str, url: str, final_url: str, body_text: str) -> None:
    key = stable_key("queue", url)
    prev = state.get(key, {}).get("status", "UNKNOWN")

    q, q_reason = detect_queue(final_url, body_text)
    b, b_reason = detect_block(body_text)
    status = "QUEUE" if q else ("BLOCK" if b else "OK")

    state.setdefault(key, {})
    state[key].update({"status": status, "final_url": final_url, "last_seen_ts": now_ts})

    if status != prev:
        if status == "QUEUE":
            tg_send(f"🚨 QUEUE DETECTED\nTime: {now_utc_str()}\nSource: {name}\nLink: {final_url}\nReason: {q_reason}")
            state[key]["last_alert_ts"] = now_ts

        elif status == "BLOCK":
            if should_realert(state, key, now_ts, ERROR_RE_ALERT_SECONDS):
                tg_send(f"🧱 POSSIBLE BLOCK/CAPTCHA\nTime: {now_utc_str()}\nSource: {name}\nLink: {final_url}\nReason: {b_reason}")
                state[key]["last_alert_ts"] = now_ts

        elif status == "OK" and prev == "QUEUE":
            tg_send(f"✅ QUEUE CLEARED\nTime: {now_utc_str()}\nSource: {name}\nLink: {final_url}")
            state[key]["last_alert_ts"] = now_ts

    # Wait time thresholds only when queue
    if status == "QUEUE":
        wait_s = parse_wait_time_seconds(body_text)
        if wait_s is not None:
            hours = wait_s / 3600.0
            th_key = stable_key("wait_th", url)
            passed = set(state.get(th_key, {}).get("passed", []))
            for th in WAITTIME_THRESHOLDS_HOURS:
                if hours <= th and str(th) not in passed:
                    tg_send(
                        f"⏱ WAIT TIME DROP\nTime: {now_utc_str()}\nSource: {name}\nWait: ~{hours:.2f}h (<= {th}h)\nLink: {final_url}"
                    )
                    passed.add(str(th))
            state[th_key] = {"passed": sorted(list(passed)), "last_seen_ts": now_ts}

        # Re-alert hourly if queue remains active
        realert_key = stable_key("queue_re", url)
        if should_realert(state, realert_key, now_ts, QUEUE_RE_ALERT_SECONDS):
            tg_send(f"🔁 QUEUE STILL ACTIVE\nTime: {now_utc_str()}\nSource: {name}\nLink: {final_url}")
            state[realert_key] = {"last_alert_ts": now_ts}

def scan_and_alert_intel(state: dict, now_ts: int, page_name: str, page_url: str, extracted_items: List[Dict[str, str]]) -> None:
    """
    Core intelligence scanner:
    - Filters items by KEYWORDS
    - Tracks item status in state
    - Alerts on:
      NEW matching item
      RESTOCK (was out_of_stock, now in_stock)
      ADD TO CART detected (sniping signal)
    """
    items_state = state.setdefault("items", {})  # url -> record

    for it in extracted_items:
        url = (it.get("url") or "").strip()
        link_text = (it.get("linkText") or "").strip()
        container_text = (it.get("containerText") or "").strip()

        if not url:
            continue

        blob = f"{link_text}\n{container_text}"
        if not text_has_keywords(blob):
            continue  # not relevant to ETB/Booster etc

        kind = product_kind(blob)
        stock_guess = infer_in_stock(blob)  # True/False/None

        # Build signature to detect meaningful changes without HTML noise
        sig = stable_key(link_text.lower()[:200], container_text.lower()[:400])

        prev = items_state.get(url, {})
        prev_sig = prev.get("sig")
        prev_stock = prev.get("stock")  # True/False/None

        first_seen = (url not in items_state)

        # Update state first
        items_state[url] = {
            "sig": sig,
            "title": link_text[:200],
            "kind": kind,
            "stock": stock_guess,
            "last_seen_ts": now_ts,
            "source_page": page_url,
            "source_name": page_name,
            "last_alert_ts": prev.get("last_alert_ts", 0),
        }

        # --- Alert: NEW matching item (low spam, cooldown per item) ---
        if first_seen:
            # Only alert if it looks purchaseable OR unknown (new listings matter)
            last_alert = int(items_state[url].get("last_alert_ts", 0) or 0)
            if (now_ts - last_alert) > NEW_ITEM_COOLDOWN_SECONDS:
                tg_send(
                    f"🆕 NEW MATCHING LISTING\n"
                    f"Time: {now_utc_str()}\n"
                    f"Type: {kind}\n"
                    f"Title: {link_text or 'N/A'}\n"
                    f"Stock: {stock_guess}\n"
                    f"Link: {url}\n"
                    f"Found on: {page_name}"
                )
                items_state[url]["last_alert_ts"] = now_ts
            continue

        # --- Alert: RESTOCK signal (was False, now True) ---
        if prev_stock is False and stock_guess is True:
            last_alert = int(items_state[url].get("last_alert_ts", 0) or 0)
            if (now_ts - last_alert) > RESTOCK_COOLDOWN_SECONDS:
                tg_send(
                    f"📦 RESTOCK SIGNAL\n"
                    f"Time: {now_utc_str()}\n"
                    f"Type: {kind}\n"
                    f"Title: {link_text or 'N/A'}\n"
                    f"Link: {url}\n"
                    f"Source: {page_name}"
                )
                items_state[url]["last_alert_ts"] = now_ts
            continue

        # --- Alert: SNIPING signal if card shows Add to cart/basket ---
        if stock_guess is True and (prev_stock is not True):
            # treat as "became buyable"
            last_alert = int(items_state[url].get("last_alert_ts", 0) or 0)
            if (now_ts - last_alert) > RESTOCK_COOLDOWN_SECONDS:
                tg_send(
                    f"🟢 BUYABLE NOW (Add-to-cart detected)\n"
                    f"Time: {now_utc_str()}\n"
                    f"Type: {kind}\n"
                    f"Title: {link_text or 'N/A'}\n"
                    f"Link: {url}\n"
                    f"Source: {page_name}"
                )
                items_state[url]["last_alert_ts"] = now_ts
            continue

        # We intentionally do NOT alert on minor signature changes for categories
        # (noise reduction). We only care about new listings + stock transitions.

def main():
    now_ts = int(time.time())
    state = load_state()

    # Daily alive (once per day)
    daily_alive(state, now_ts)

    targets = [
        ("PC UK Home", PC_UK_HOME),
        ("PC UK TCG Category", PC_UK_TCG),
        ("PC UK New Releases", PC_UK_NEW),
    ]

    for name, url in targets:
        try:
            res = fetch_page(url)
            final_url = res["final_url"]
            body_text = res["body_text"]
            items = res["items"] or []

            # Queue intelligence
            queue_alerts(state, now_ts, name, url, final_url, body_text)

            # Category intelligence scanner
            scan_and_alert_intel(state, now_ts, name, url, items)

            print(f"OK: {name} -> {final_url} items={len(items)}")

        except Exception as e:
            err_key = stable_key("error", url)
            if should_realert(state, err_key, now_ts, ERROR_RE_ALERT_SECONDS):
                tg_send(
                    f"⚠️ MONITOR ERROR\n"
                    f"Time: {now_utc_str()}\n"
                    f"Target: {name}\n"
                    f"URL: {url}\n"
                    f"Error: {type(e).__name__}: {e}"
                )
                state[err_key] = {"last_alert_ts": now_ts}
            print("Error:", name, e)

    save_state(state)

if __name__ == "__main__":
    main()
