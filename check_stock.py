import os
import json
import time
import hashlib
from typing import List, Dict, Tuple

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
TARGET_URLS = os.getenv("TARGET_URLS", "").strip()

STATE_FILE = "state.json"
DEFAULT_TIMEOUT_MS = 30000


def tg_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secrets missing; skipping send.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text})
    print("Telegram status:", r.status_code)
    if r.status_code != 200:
        print("Telegram response:", r.text)


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"seen": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": {}}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def parse_target_urls(raw: str) -> List[str]:
    if not raw:
        # Fallback to the 3 you were using
        return [
            "https://www.pokemoncenter.com/en-gb/",
            "https://www.pokemoncenter.com/en-gb/category/trading-card-game",
            "https://www.pokemoncenter.com/en-gb/new-releases",
        ]
    return [u.strip() for u in raw.split(",") if u.strip()]


def hash_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def extract_product_links_from_html(html: str, base_url: str) -> List[str]:
    """
    Conservative extraction: finds product-like links on Pokemon Center pages.
    This may need tuning if PC changes layout.
    """
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        # Product pages typically contain "/product/" on Pokemon Center
        if "/product/" in href:
            if href.startswith("http"):
                links.add(href)
            else:
                # make absolute
                if href.startswith("/"):
                    links.add("https://www.pokemoncenter.com" + href)
                else:
                    links.add(base_url.rstrip("/") + "/" + href)

    return sorted(links)


def fetch_page_content(url: str) -> Tuple[str, str]:
    """
    Returns: (status, content)
    status is one of: OK / BOT_WALL / ERROR
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
            page.wait_for_timeout(1500)  # small settle
            content = page.content()

            status_code = resp.status if resp else 0
            if status_code in (401, 403):
                return "BOT_WALL", f"HTTP {status_code}"
            if status_code >= 400:
                return "ERROR", f"HTTP {status_code}"

            # Heuristic: cloudflare/bot interstitial often contains these keywords
            lowered = content.lower()
            if "cloudflare" in lowered or "attention required" in lowered or "verify you are human" in lowered:
                return "BOT_WALL", "Interstitital detected"

            return "OK", content
        except Exception as e:
            return "ERROR", str(e)
        finally:
            context.close()
            browser.close()


def main() -> None:
    urls = parse_target_urls(TARGET_URLS)
    state = load_state()
    seen = state.get("seen", {})

    tg_send("🟩 PokeWonder live — monitor cycle started.")

    sources_scanned = 0
    total_links_found = 0
    new_links = 0
    errors: List[str] = []

    for url in urls:
        sources_scanned += 1
        status, payload = fetch_page_content(url)

        if status == "BOT_WALL":
            errors.append(f"- {url} (BOT WALL: {payload})")
            continue
        if status == "ERROR":
            errors.append(f"- {url} (ERROR: {payload})")
            continue

        html = payload
        links = extract_product_links_from_html(html, url)
        total_links_found += len(links)

        # Track “new links” per source
        source_key = hash_str(url)
        prev = set(seen.get(source_key, []))
        curr = set(links)
        diff = sorted(curr - prev)

        if diff:
            new_links += len(diff)
            # Save new state BEFORE alerting so repeats don’t spam
            seen[source_key] = sorted(curr)

            # Alert only a limited number to avoid Telegram spam
            top = diff[:10]
            msg = "🆕 PokeWonder — new product links detected:\n" + "\n".join(top)
            if len(diff) > 10:
                msg += f"\n(+{len(diff) - 10} more)"
            tg_send(msg)
        else:
            # Still update seen if empty previously
            if not prev and curr:
                seen[source_key] = sorted(curr)

    state["seen"] = seen
    save_state(state)

    # Summary
    lines = [
        f"📊 PokeWonder scan summary — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"- Sources scanned: {sources_scanned}",
        f"- Product links detected: {total_links_found}",
        f"- New listings this run: {new_links}",
    ]
    if errors:
        lines.append("\n- Source errors:")
        lines.extend(errors)
        lines.append("\n⚠️ If you see BOT WALL / 403 on GitHub, Pokemon Center is blocking GitHub runner traffic.")
        lines.append("✅ Best fix: run this on a home PC/Raspberry Pi or a GitHub *self-hosted runner* (your own IP).")

    tg_send("\n".join(lines))


if __name__ == "__main__":
    main()
