import os
import json
import time
import re
import hashlib
import requests
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
CHAT_ID = os.environ["CHAT_ID"].strip()

STATE_FILE = "state.json"

SEND_SUMMARY = os.environ.get("SEND_SUMMARY", "").strip() == "1"

PC_UK_HOME = "https://www.pokemoncenter.com/en-gb"
PC_UK_TCG = "https://www.pokemoncenter.com/en-gb/category/trading-card-game"
PC_UK_NEW = "https://www.pokemoncenter.com/en-gb/new-releases"

DEFAULT_KEYWORDS = [
    "elite trainer box", "etb",
    "booster box", "booster display",
    "booster bundle",
    "build & battle",
    "trainer toolkit",
    "collection",
    "premium collection",
]

KEYWORDS_ENV = os.environ.get("KEYWORDS", "").strip()
if KEYWORDS_ENV:
    KEYWORDS = [k.strip().lower() for k in KEYWORDS_ENV.split(",") if k.strip()]
else:
    KEYWORDS = [k.lower() for k in DEFAULT_KEYWORDS]

DAILY_ALIVE_KEY = "daily_alive_utc"
ERROR_RE_ALERT_SECONDS = 30 * 60
QUEUE_RE_ALERT_SECONDS = 60 * 60

WAITTIME_THRESHOLDS_HOURS = [6, 3, 1]

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
QUEUE_URL_HINTS = ["queue-it", "virtual-queue", "waiting", "line"]

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
    print("Telegram:", r.status_code, r.text[:200])

def should_realert(state: dict, key: str, now_ts: int, window_seconds: int) -> bool:
    last_ts = int(state.get(key, {}).get("last_alert_ts", 0) or 0)
    return (now_ts - last_ts) > window_seconds

def parse_wait_time_seconds(text: str) -> Optional[int]:
    t = " ".join((text or "").split())
    m = re.search(r"\b(\d{1,2}):(\d{2}):(\d{2})\b", t)
    if m:
        h = int(m.group(1)); mm = int(m.group(2)); ss = int(m.group(3))
        return h * 3600 + mm * 60 + ss
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
            return True, f"Block hint: {hint}"
    return False, "No block hints"

def text_has_keywords(text: str) -> bool:
    tt = (text or "").lower()
    return any(k in tt for k in KEYWORDS)

def infer_in_stock(text_blob: str) -> Optional[bool]:
    t = (text_blob or "").lower()
    if "out of stock" in t or "sold out" in t or "unavailable" in t:
        return False
    if "add to cart" in t or "add to basket" in t:
        return True
    return None

def product_kind(text_blob: str) -> str:
    t = (text_blob or "").lower()
    if "elite trainer box" in t or re.search(r"\betb\b", t): return "ETB"
    if "booster box" in t or "booster display" in t: return "Booster Box"
    if "booster bundle" in t: return "Booster Bundle"
    if "build & battle" in t: return "Build & Battle"
    if "trainer toolkit" in t: return "Trainer Toolkit"
    if "collection" in t: return "Collection"
    return "TCG Item"

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

        try:
            body_text = page.inner_text("body")
        except Exception:
            body_text = ""

        try:
            html = page.content()
        except Exception:
            html = ""

        # DOM extraction
        items = page.evaluate("""
          () => {
            function norm(s){ return (s || "").replace(/\\s+/g," ").trim(); }
            const anchors = Array.from(document.querySelectorAll('a[href*="/product/"]'));
            const results = [];
            const seen = new Set();
            for (const a of anchors) {
              const href = a.getAttribute("href") || "";
              if (!href.includes("/product/")) continue;
              const abs = new URL(href, window.location.origin).toString();
              if (seen.has(abs)) continue;
              seen.add(abs);
              const linkText = norm(a.innerText);
              let container = a.closest('li, article, div');
              let containerText = container ? norm(container.innerText) : "";
              if (containerText.length > 500) containerText = containerText.slice(0, 500);
              results.push({ url: abs, linkText, containerText });
            }
            return results;
          }
        """)

        # HTML fallback extraction (important if DOM rendering is odd)
        if not items:
            found = set(re.findall(r'href="([^"]*/product/[^"]+)"', html))
            items = [{"url": (u if u.startswith("http") else f"https://www.pokemoncenter.com{u}"),
                      "linkText": "",
                      "containerText": ""} for u in sorted(found)]

        context.close()
        browser.close()

        return {"final_url": final_url, "body_text": body_text, "items": items or []}

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

    if status == "QUEUE":
        wait_s = parse_wait_time_seconds(body_text)
        if wait_s is not None:
            hours = wait_s / 3600.0
            th_key = stable_key("wait_th", url)
            passed = set(state.get(th_key, {}).get("passed", []))
            for th in WAITTIME_THRESHOLDS_HOURS:
                if hours <= th and str(th) not in passed:
                    tg_send(f"⏱ WAIT TIME DROP\nTime: {now_utc_str()}\nSource: {name}\nWait: ~{hours:.2f}h (<= {th}h)\nLink: {final_url}")
                    passed.add(str(th))
            state[th_key] = {"passed": sorted(list(passed)), "last_seen_ts": now_ts}

        realert_key = stable_key("queue_re", url)
        if should_realert(state, realert_key, now_ts, QUEUE_RE_ALERT_SECONDS):
            tg_send(f"🔁 QUEUE STILL ACTIVE\nTime: {now_utc_str()}\nSource: {name}\nLink: {final_url}")
            state[realert_key] = {"last_alert_ts": now_ts}

def scan_and_alert(state: dict, now_ts: int, page_name: str, page_url: str, items: List[Dict[str, str]], summary: dict) -> None:
    items_state = state.setdefault("items", {})
    matched = 0
    new_items = 0
    buyable_hits = 0

    sample_titles = []

    for it in items:
        url = (it.get("url") or "").strip()
        link_text = (it.get("linkText") or "").strip()
        container_text = (it.get("containerText") or "").strip()
        if not url:
            continue

        blob = f"{link_text}\n{container_text}".strip()

        if not text_has_keywords(blob):
            continue

        matched += 1
        kind = product_kind(blob)
        stock_guess = infer_in_stock(blob)

        prev = items_state.get(url)

        if not prev:
            new_items += 1
            items_state[url] = {
                "title": link_text[:200],
                "kind": kind,
                "stock": stock_guess,
                "last_seen_ts": now_ts,
                "source": page_name
            }
            if len(sample_titles) < 5:
                sample_titles.append(f"{kind}: {link_text[:80] or '(no title)'}")
            # only alert new item if it has a title (avoid pure href spam)
            if link_text:
                tg_send(f"🆕 NEW MATCHING LISTING\nTime: {now_utc_str()}\nType: {kind}\nTitle: {link_text}\nStock: {stock_guess}\nLink: {url}\nFound on: {page_name}")
            continue

        # Buyable transition
        if prev.get("stock") is False and stock_guess is True:
            buyable_hits += 1
            tg_send(f"📦 RESTOCK SIGNAL\nTime: {now_utc_str()}\nType: {kind}\nTitle: {link_text or prev.get('title','')}\nLink: {url}\nSource: {page_name}")

        items_state[url]["stock"] = stock_guess
        items_state[url]["last_seen_ts"] = now_ts

    summary["pages"].append({
        "page": page_name,
        "url": page_url,
        "items_found": len(items),
        "matched": matched,
        "new": new_items,
        "restock_hits": buyable_hits,
        "samples": sample_titles
    })

def main():
    now_ts = int(time.time())
    state = load_state()

    daily_alive(state, now_ts)

    targets = [
        ("PC UK Home", PC_UK_HOME),
        ("PC UK TCG Category", PC_UK_TCG),
        ("PC UK New Releases", PC_UK_NEW),
    ]

    summary = {"ts": now_utc_str(), "pages": []}

    for name, url in targets:
        try:
            res = fetch_page(url)
            final_url = res["final_url"]
            body_text = res["body_text"]
            items = res["items"] or []

            queue_alerts(state, now_ts, name, url, final_url, body_text)
            scan_and_alert(state, now_ts, name, url, items, summary)

            print(f"OK {name}: items={len(items)} final={final_url}")

        except Exception as e:
            err_key = stable_key("error", url)
            if should_realert(state, err_key, now_ts, ERROR_RE_ALERT_SECONDS):
                tg_send(f"⚠️ MONITOR ERROR\nTime: {now_utc_str()}\nTarget: {name}\nURL: {url}\nError: {type(e).__name__}: {e}")
                state[err_key] = {"last_alert_ts": now_ts}
            print("Error:", name, e)

    save_state(state)

    # Summary mode: always prove it scanned something
    if SEND_SUMMARY:
        lines = [f"📊 PokeWonder scan summary — {summary['ts']}"]
        for p in summary["pages"]:
            lines.append(f"- {p['page']}: found={p['items_found']} matched={p['matched']} new={p['new']} restock_hits={p['restock_hits']}")
            if p["samples"]:
                lines.append("  samples: " + " | ".join(p["samples"]))
        tg_send("\n".join(lines))

if __name__ == "__main__":
    main()
