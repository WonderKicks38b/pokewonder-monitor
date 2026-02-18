import os
import json
import time
import requests
from datetime import datetime, timezone

STATE_PATH = "state.json"

TARGET_KEYWORDS = [
    "elite trainer box",
    "etb",
    "booster box",
    "booster bundle",
    "premium collection",
    "special collection",
    "destined rivals",
    "ascended heroes"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
}

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

def tg_send(text):
    token = os.environ.get("BOT_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, data={"chat_id": chat_id, "text": text})

def keyword_match(title):
    t = title.lower()
    return any(k in t for k in TARGET_KEYWORDS)

def get_build_id():
    r = requests.get("https://www.pokemoncenter.com/en-gb", headers=HEADERS)
    if r.status_code != 200:
        return None
    marker = '"buildId":"'
    if marker not in r.text:
        return None
    return r.text.split(marker)[1].split('"')[0]

def fetch_home_data(build_id):
    url = f"https://www.pokemoncenter.com/_next/data/{build_id}/en-gb.json"
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        return None
    return r.json()

def extract_products(data):
    products = []
    try:
        page_props = data["pageProps"]
        for section in page_props.values():
            if isinstance(section, dict):
                for v in section.values():
                    if isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict):
                                name = item.get("name")
                                url = item.get("url")
                                if name and url:
                                    if not url.startswith("http"):
                                        url = "https://www.pokemoncenter.com" + url
                                    products.append({
                                        "title": name,
                                        "url": url
                                    })
    except:
        pass
    return products

def main():
    state = load_state()

    summary = [f"📊 PokeWonder scan summary — {now_utc()}"]

    build_id = get_build_id()
    if not build_id:
        tg_send("⚠️ Could not retrieve build ID.")
        return

    data = fetch_home_data(build_id)
    if not data:
        tg_send("⚠️ Could not fetch Next.js data.")
        return

    products = extract_products(data)

    found = 0
    new_hits = 0

    for p in products:
        if keyword_match(p["title"]):
            found += 1
            if p["url"] not in state:
                state[p["url"]] = int(time.time())
                new_hits += 1
                tg_send(f"🔥 NEW MATCH\n{p['title']}\n{p['url']}")

    save_state(state)

    summary.append(f"Totals: found={found} new={new_hits}")

    if found == 0:
        summary.append("⚠️ No keyword matches found on homepage.")

    tg_send("\n".join(summary))

if __name__ == "__main__":
    main()
