import os
import json
import time
import hashlib
import requests
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
CHAT_ID = os.environ["CHAT_ID"].strip()

STATE_FILE = "state.json"

# --- What we monitor ---
PC_UK_HOME = "https://www.pokemoncenter.com/en-gb"
PC_UK_TCG = "https://www.pokemoncenter.com/en-gb/category/trading-card-game"
PC_UK_NEW = "https://www.pokemoncenter.com/en-gb/new-releases"

# Add product URLs you care about (paste real product pages here)
PRODUCT_URLS = [
    # "https://www.pokemoncenter.com/en-gb/product/....",
]

# How often we re-alert if something stays "ON" (prevents spam)
RE_ALERT_SECONDS = 60 * 60  # 1 hour

# Wait-time alerts (hours)
WAITTIME_THRESHOLDS_HOURS = [6, 3, 1]  # alert when <= 6h, <= 3h, <= 1h

# Queue detection hints (text + URL)
QUEUE_TEXT_HINTS = [
    "virtual queue",
    "you're in the virtual queue",
    "estimated wait time",
    "keep this window open",
    "do not refresh",
    "lose your place in line",
    "high volume of requests",
]
QUEUE_URL_HINTS = ["queue-it", "virtual-queue", "queue", "waiting", "line"]

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def tg_send(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    # Print for logs (safe)
    print("Telegram sendMessage:", r.status_code, r.text[:200])


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


def parse_wait_time_seconds(text: str) -> int | None:
    """
    Attempts to parse wait time formats commonly seen:
    - 06:36:00
    - 1:05:00
    - 15:20
    - "Estimated wait time : 06:36:00"
    Returns seconds or None.
    """
    t = " ".join(text.split())
    # find a token that looks like H:MM:SS or HH:MM:SS or MM:SS
    import re

    candidates = re.findall(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", t)
    if not candidates:
        return None

    # take the first match
    h, m, s = candidates[0]
    h = int(h)
    m = int(m)
    if s is None or s == "":
        sec = 0
    else:
        sec = int(s)
        # if pattern was MM:SS, h is actually minutes
        # But our regex uses h:m(:s). If only two groups -> treat as MM:SS.
    if len(candidates[0]) == 3 and candidates[0][2] is not None:
        # H:MM:SS or HH:MM:SS
        return h * 3600 + m * 60 + sec
    else:
        # MM:SS
        return h * 60 + m


def is_queue(final_url: str, body_text: str) -> tuple[bool, str]:
    u = (final_url or "").lower()
    t = (body_text or "").lower()
    if any(h in u for h in QUEUE_URL_HINTS):
        return True, "URL matched queue hints"
    if any(h in t for h in QUEUE_TEXT_HINTS):
        return True, "Text matched queue hints"
    return False, "No queue hints"


def should_realert(state: dict, key: str, now_ts: int) -> bool:
    last = state.get(key, {})
    last_ts = int(last.get("last_alert_ts", 0) or 0)
    return (now_ts - last_ts) > RE_ALERT_SECONDS


def browser_fetch(url: str) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        page = context.new_page()

        page.goto(url, wait_until="networkidle", timeout=60000)

        final_url = page.url
        body_text = page.inner_text("body") if page.locator("body").count() else ""
        html = page.content()

        context.close()
        browser.close()

        return {"final_url": final_url, "body_text": body_text, "html": html}


def detect_stock_signals(html: str, body_text: str) -> dict:
    """
    Lightweight, generic product/category detection:
    - looks for 'out of stock' and 'add to cart' style signals.
    This is heuristic; we can harden later per-page.
    """
    t = (body_text or "").lower()
    h = (html or "").lower()

    out_of_stock = ("out of stock" in t) or ("out of stock" in h) or ("sold out" in t) or ("sold out" in h)
    add_to_cart = ("add to cart" in t) or ("add to basket" in t) or ("add to cart" in h) or ("add to basket" in h)

    return {
        "out_of_stock": out_of_stock,
        "add_to_cart": add_to_cart,
    }


def alert_queue_changes(state: dict, now_ts: int, name: str, url: str, final_url: str, body_text: str):
    key = stable_key("queue", url)
    was = state.get(key, {}).get("status", "UNKNOWN")

    queue, reason = is_queue(final_url, body_text)
    wait_seconds = parse_wait_time_seconds(body_text) if queue else None

    # Always store latest snapshot
    state.setdefault(key, {})
    state[key]["status"] = "QUEUE" if queue else "OK"
    state[key]["final_url"] = final_url
    state[key]["last_seen_ts"] = now_ts

    # Status change alerts
    if queue and was != "QUEUE":
        tg_send(
            f"🚨 QUEUE DETECTED\n"
            f"Time: {now_utc_str()}\n"
            f"Source: {name}\n"
            f"Link: {final_url}\n"
            f"Reason: {reason}"
        )
        state[key]["last_alert_ts"] = now_ts

    if (not queue) and was == "QUEUE":
        tg_send(
            f"✅ QUEUE CLEARED\n"
            f"Time: {now_utc_str()}\n"
            f"Source: {name}\n"
            f"Link: {final_url}"
        )
        state[key]["last_alert_ts"] = now_ts

    # Wait-time threshold alerts (only when queue is on)
    if queue and wait_seconds is not None:
        hours = wait_seconds / 3600.0
        thresholds_key = stable_key("wait_thresholds", url)
        passed = set(state.get(thresholds_key, {}).get("passed", []))

        for th in WAITTIME_THRESHOLDS_HOURS:
            if hours <= th and str(th) not in passed:
                tg_send(
                    f"⏱ WAIT TIME DROP\n"
                    f"Time: {now_utc_str()}\n"
                    f"Source: {name}\n"
                    f"Wait: ~{hours:.2f}h (<= {th}h)\n"
                    f"Link: {final_url}"
                )
                passed.add(str(th))

        state[thresholds_key] = {"passed": sorted(list(passed)), "last_seen_ts": now_ts}

    # Re-alert if queue stays on forever (optional)
    if queue and should_realert(state, key, now_ts):
        tg_send(
            f"🔁 QUEUE STILL ACTIVE\n"
            f"Time: {now_utc_str()}\n"
            f"Source: {name}\n"
            f"Link: {final_url}"
        )
        state[key]["last_alert_ts"] = now_ts


def alert_stock_changes(state: dict, now_ts: int, name: str, url: str, final_url: str, body_text: str, html: str):
    key = stable_key("stock", url)

    signals = detect_stock_signals(html, body_text)
    signature = stable_key(str(signals["out_of_stock"]), str(signals["add_to_cart"]))

    prev_sig = state.get(key, {}).get("sig")

    # Alert on change
    if prev_sig is not None and prev_sig != signature:
        tg_send(
            f"📦 STOCK PAGE CHANGE\n"
            f"Time: {now_utc_str()}\n"
            f"Page: {name}\n"
            f"Link: {final_url}\n"
            f"Now: out_of_stock={signals['out_of_stock']} | add_to_cart={signals['add_to_cart']}"
        )
        state[key]["last_alert_ts"] = now_ts

    # First time baseline: optionally send nothing
    if prev_sig is None:
        print(f"Baseline set for {name}: {signals}")

    # Special: if add_to_cart becomes true, alert immediately
    if signals["add_to_cart"]:
        last_alert = int(state.get(key, {}).get("last_alert_ts", 0) or 0)
        if (now_ts - last_alert) > 300:  # 5 min cooldown for add-to-cart
            tg_send(
                f"🟢 POSSIBLE RESTOCK (Add to cart detected)\n"
                f"Time: {now_utc_str()}\n"
                f"Page: {name}\n"
                f"Link: {final_url}"
            )
            state[key]["last_alert_ts"] = now_ts

    state[key] = {
        "sig": signature,
        "signals": signals,
        "final_url": final_url,
        "last_seen_ts": now_ts,
        "last_alert_ts": state.get(key, {}).get("last_alert_ts", 0),
    }


def main():
    now_ts = int(time.time())
    state = load_state()

    # --- Quick connection test (kept lightweight) ---
    tg_send("🧪 PokeWonder live — monitor cycle started.")

    targets = [
        ("PC UK Home", PC_UK_HOME),
        ("PC UK TCG Category", PC_UK_TCG),
        ("PC UK New Releases", PC_UK_NEW),
    ]

    # Add product targets
    for i, u in enumerate(PRODUCT_URLS, start=1):
        targets.append((f"Product {i}", u))

    for name, url in targets:
        try:
            res = browser_fetch(url)
            final_url = res["final_url"]
            body_text = res["body_text"]
            html = res["html"]

            # Queue detection & alerts
            alert_queue_changes(state, now_ts, name, url, final_url, body_text)

            # Stock-style change detection (categories + products)
            alert_stock_changes(state, now_ts, name, url, final_url, body_text, html)

            print(f"Checked {name} OK -> {final_url}")

        except Exception as e:
            # On errors, alert but don't spam
            err_key = stable_key("error", url)
            if should_realert(state, err_key, now_ts):
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




