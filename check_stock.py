import os
import json
import time
import hashlib
import re
import requests
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any, List
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# =====================
# REQUIRED SECRETS
# =====================
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
CHAT_ID = os.environ["CHAT_ID"].strip()

# =====================
# CONFIG (safe defaults)
# =====================
STATE_FILE = "state.json"

PC_UK_HOME = "https://www.pokemoncenter.com/en-gb"
PC_UK_TCG = "https://www.pokemoncenter.com/en-gb/category/trading-card-game"
PC_UK_NEW = "https://www.pokemoncenter.com/en-gb/new-releases"

# Optional: supply product URLs via GitHub secret/variable PRODUCT_URLS (comma-separated)
# e.g. https://.../product/xxx,https://.../product/yyy
PRODUCT_URLS_ENV = os.environ.get("PRODUCT_URLS", "").strip()
PRODUCT_URLS = [u.strip() for u in PRODUCT_URLS_ENV.split(",") if u.strip()]

# Every run schedule: alerts are rate-limited via state to avoid spam
RE_ALERT_SECONDS = 60 * 60  # 1 hour general re-alert
ERROR_RE_ALERT_SECONDS = 60 * 30  # 30 min for errors
SNIPE_COOLDOWN_SECONDS = 60 * 5  # 5 min cooldown for "Add to cart detected" on same page

# Wait-time threshold alerts (hours). Can override via WAIT_THRESHOLDS="6,3,1"
WAIT_THRESHOLDS_ENV = os.environ.get("WAIT_THRESHOLDS", "").strip()
if WAIT_THRESHOLDS_ENV:
    try:
        WAITTIME_THRESHOLDS_HOURS = [int(x.strip()) for x in WAIT_THRESHOLDS_ENV.split(",") if x.strip()]
    except Exception:
        WAITTIME_THRESHOLDS_HOURS = [6, 3, 1]
else:
    WAITTIME_THRESHOLDS_HOURS = [6, 3, 1]

# User agent
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# =====================
# QUEUE / BLOCK DETECTION
# =====================
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
# HELPERS
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
    last = state.get(key, {})
    last_ts = int(last.get("last_alert_ts", 0) or 0)
    return (now_ts - last_ts) > window_seconds


def parse_wait_time_seconds(text: str) -> Optional[int]:
    """
    Tries to parse:
    - HH:MM:SS or H:MM:SS
    - MM:SS
    - Also catches "Estimated wait time : 06:36:00"
    Returns seconds or None.
    """
    t = " ".join((text or "").split())

    # HH:MM:SS
    m = re.search(r"\b(\d{1,2}):(\d{2}):(\d{2})\b", t)
    if m:
        h = int(m.group(1)); mm = int(m.group(2)); ss = int(m.group(3))
        return h * 3600 + mm * 60 + ss

    # MM:SS
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


def extract_title(html: str) -> str:
    if not html:
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title[:120]


def detect_stock_signals(html: str, body_text: str) -> Dict[str, bool]:
    """
    Minimal but useful heuristics.
    We treat "add to cart/basket" as a strong restock signal.
    """
    t = (body_text or "").lower()
    h = (html or "").lower()

    out_of_stock = (
        ("out of stock" in t) or ("sold out" in t) or
        ("out of stock" in h) or ("sold out" in h)
    )

    add_to_cart = (
        ("add to cart" in t) or ("add to basket" in t) or
        ("add to cart" in h) or ("add to basket" in h)
    )

    return {"out_of_stock": out_of_stock, "add_to_cart": add_to_cart}


# =====================
# PLAYWRIGHT FETCH (with smarter signals)
# =====================
def browser_fetch(url: str) -> Dict[str, Any]:
    """
    Returns:
      final_url, body_text, html, first_url, response_status (best-effort)
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        page = context.new_page()

        first_url = url
        response_status = None

        def on_response(resp):
            nonlocal response_status
            # capture first main-document-ish status
            try:
                if resp.request.resource_type == "document" and response_status is None:
                    response_status = resp.status
            except Exception:
                pass

        page.on("response", on_response)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # give it a moment for redirects/queue scripts
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeoutError:
            pass

        final_url = page.url
        body_text = ""
        try:
            if page.locator("body").count():
                body_text = page.inner_text("body")
        except Exception:
            body_text = ""

        html = ""
        try:
            html = page.content()
        except Exception:
            html = ""

        context.close()
        browser.close()

        return {
            "first_url": first_url,
            "final_url": final_url,
            "response_status": response_status,
            "body_text": body_text,
            "html": html,
        }


# =====================
# ALERT LOGIC
# =====================
def daily_alive(state: dict, now_ts: int) -> None:
    """
    Upgrade #1: Alive message daily only (UTC).
    """
    key = "daily_alive_utc"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get(key) != today:
        tg_send(f"🟢 PokeWonder operational — {today}")
        state[key] = today
        state.setdefault("_meta", {})["last_alive_ts"] = now_ts


def queue_alerts(state: dict, now_ts: int, name: str, source_url: str, final_url: str, body_text: str) -> None:
    """
    Upgrade #3: Better queue intelligence + wait-time thresholds + status change.
    """
    key = stable_key("queue", source_url)
    prev_status = state.get(key, {}).get("status", "UNKNOWN")

    is_q, q_reason = detect_queue(final_url, body_text)
    is_b, b_reason = detect_block(body_text)

    status = "QUEUE" if is_q else ("BLOCK" if is_b else "OK")
    state.setdefault(key, {})
    state[key].update({
        "status": status,
        "final_url": final_url,
        "last_seen_ts": now_ts,
    })

    # Status transition alerts (high value, low noise)
    if status != prev_status:
        if status == "QUEUE":
            tg_send(
                f"🚨 QUEUE DETECTED\nTime: {now_utc_str()}\nSource: {name}\nLink: {final_url}\nReason: {q_reason}"
            )
            state[key]["last_alert_ts"] = now_ts

        elif status == "BLOCK":
            # Don't spam blocks; rate-limit
            if should_realert(state, key, now_ts, ERROR_RE_ALERT_SECONDS):
                tg_send(
                    f"🧱 POSSIBLE BLOCK/CAPTCHA\nTime: {now_utc_str()}\nSource: {name}\nLink: {final_url}\nReason: {b_reason}"
                )
                state[key]["last_alert_ts"] = now_ts

        elif status == "OK" and prev_status == "QUEUE":
            tg_send(
                f"✅ QUEUE CLEARED\nTime: {now_utc_str()}\nSource: {name}\nLink: {final_url}"
            )
            state[key]["last_alert_ts"] = now_ts

    # Wait-time thresholds (only when QUEUE)
    if status == "QUEUE":
        wait_seconds = parse_wait_time_seconds(body_text)
        if wait_seconds is not None:
            hours = wait_seconds / 3600.0

            th_key = stable_key("wait_thresholds", source_url)
            passed = set(state.get(th_key, {}).get("passed", []))

            for th in sorted(WAITTIME_THRESHOLDS_HOURS, reverse=True):
                # alert when it drops BELOW threshold for first time
                if hours <= th and str(th) not in passed:
                    tg_send(
                        f"⏱ WAIT TIME DROP\nTime: {now_utc_str()}\nSource: {name}\nWait: ~{hours:.2f}h (<= {th}h)\nLink: {final_url}"
                    )
                    passed.add(str(th))

            state[th_key] = {"passed": sorted(list(passed)), "last_seen_ts": now_ts}

        # Optional re-alert if queue stays active forever (hourly)
        if should_realert(state, stable_key("queue_realert", source_url), now_ts, RE_ALERT_SECONDS):
            tg_send(
                f"🔁 QUEUE STILL ACTIVE\nTime: {now_utc_str()}\nSource: {name}\nLink: {final_url}"
            )
            state[stable_key("queue_realert", source_url)] = {"last_alert_ts": now_ts}


def stock_alerts(
    state: dict,
    now_ts: int,
    name: str,
    source_url: str,
    final_url: str,
    body_text: str,
    html: str,
    is_product: bool,
) -> None:
    """
    Upgrade #2: Noise reduction.
      - For categories: only alert on strong signals (add_to_cart appearing) or major signature changes (rare).
      - For products: alert on add_to_cart and out_of_stock->false transitions.
    Upgrade #4: Sniping mode.
      - "Add to cart detected" sends immediately with cooldown.
    """
    key = stable_key("stock", source_url)
    prev = state.get(key, {})
    prev_signals = prev.get("signals", {})
    prev_add = bool(prev_signals.get("add_to_cart", False))
    prev_oos = bool(prev_signals.get("out_of_stock", False))

    signals = detect_stock_signals(html, body_text)
    add_to_cart = signals["add_to_cart"]
    out_of_stock = signals["out_of_stock"]

    title = extract_title(html)

    # Build a conservative signature (reduces noise):
    # - We do NOT hash full HTML.
    # - We hash just these booleans + title.
    sig = stable_key(str(add_to_cart), str(out_of_stock), title)

    # Store baseline
    first_seen = prev.get("sig") is None
    state[key] = {
        "sig": sig,
        "signals": signals,
        "final_url": final_url,
        "last_seen_ts": now_ts,
        "last_alert_ts": prev.get("last_alert_ts", 0),
        "title": title,
    }

    # -------------------
    # SNIPE: Add-to-cart detected (strong)
    # -------------------
    if add_to_cart:
        last_alert = int(state[key].get("last_alert_ts", 0) or 0)
        if (now_ts - last_alert) > SNIPE_COOLDOWN_SECONDS:
            tg_send(
                f"🟢 ADD TO CART DETECTED (SNIPE)\n"
                f"Time: {now_utc_str()}\n"
                f"Target: {name}\n"
                f"Title: {title or 'N/A'}\n"
                f"Link: {final_url}"
            )
            state[key]["last_alert_ts"] = now_ts
        return  # if add-to-cart is true we don't need other noisy alerts

    # -------------------
    # PRODUCT: out_of_stock -> not out_of_stock (restock hint)
    # -------------------
    if is_product and prev_oos and (not out_of_stock) and (not first_seen):
        tg_send(
            f"📦 RESTOCK SIGNAL (OOS cleared)\n"
            f"Time: {now_utc_str()}\n"
            f"Target: {name}\n"
            f"Title: {title or 'N/A'}\n"
            f"Link: {final_url}"
        )
        state[key]["last_alert_ts"] = now_ts
        return

    # -------------------
    # CATEGORY: only alert if add-to-cart appears (handled above) or major signature change
    # PRODUCT: optionally alert on major signature changes too
    # -------------------
    prev_sig = prev.get("sig")
    if (not first_seen) and prev_sig and prev_sig != sig:
        # Noise reduction:
        # - categories: do NOT alert for small changes unless this is a product page
        if is_product:
            tg_send(
                f"🧩 PRODUCT PAGE CHANGED\n"
                f"Time: {now_utc_str()}\n"
                f"Target: {name}\n"
                f"Title: {title or 'N/A'}\n"
                f"Link: {final_url}\n"
                f"Now: out_of_stock={out_of_stock} | add_to_cart={add_to_cart}"
            )
            state[key]["last_alert_ts"] = now_ts
        else:
            # categories: silence; just keep baseline updated
            pass


def main():
    now_ts = int(time.time())
    state = load_state()

    # Upgrade #1: daily-only alive message
    daily_alive(state, now_ts)

    # Targets
    category_targets = [
        ("PC UK Home", PC_UK_HOME),
        ("PC UK TCG Category", PC_UK_TCG),
        ("PC UK New Releases", PC_UK_NEW),
    ]
    product_targets = [(f"Product {i}", u) for i, u in enumerate(PRODUCT_URLS, start=1)]

    # If user hasn't provided product URLs yet, still run categories
    targets = [(n, u, False) for (n, u) in category_targets] + [(n, u, True) for (n, u) in product_targets]

    for name, url, is_product in targets:
        try:
            res = browser_fetch(url)
            final_url = res["final_url"]
            body_text = res["body_text"]
            html = res["html"]

            # Upgrade #3: advanced queue detection + wait-time thresholds + block detection
            queue_alerts(state, now_ts, name, url, final_url, body_text)

            # Upgrade #2 + #4: noise-reduced stock alerts + sniping mode
            stock_alerts(state, now_ts, name, url, final_url, body_text, html, is_product=is_product)

            print(f"Checked {name} OK -> {final_url}")

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
