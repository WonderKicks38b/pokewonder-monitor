import os
import json
import time
import requests
from datetime import datetime, timezone

STATE_PATH = "state.json"

BASE_API = "https://www.pokemoncenter.com/api/search"

DEFAULT_KEYWORDS = [
    "etb",
    "elite trainer box",
    "booster box",
    "booster bundle",
    "premium collection",
    "special collection",
    "destined rivals",
    "ascended heroes",
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

def keyword_match(title, keywords):
    t = title.lower()
    return any(k.lower() in t for k in keywords)

def search_api(query):
    params = {
        "q": query,
        "locale": "en-gb"
    }
    r = requests.get(BASE_API, headers=HEADERS, params=params, timeout=20)
    if r.status_code != 200:
        return []
    data = r.json()
    results = []
    for item in data.get("results", []):
        title = item.get("name")
        url = item.get("url")
        if title and url:
            if not url.startswith("http"):
                url = "https://www.pokemoncenter.com" + url
            results.append({
                "title": title,
                "url": url
            })
    return results

def main():
    keywords = DEFAULT_KEYWORDS
    state = load_state()

    total_found = 0
    new_hits = 0

    summary = [f"📊 PokeWonder scan summary — {now_utc()}"]

    for keyword in keywords:
        try:
            products = search_api(keyword)
            total_found += len(products)

            for p in products:
                if not keyword_match(p["title"], keywords):
                    continue

                key = p["url"]
                if key not in state:
                    state[key] = int(time.time())
                    new_hits += 1
                    tg_send(f"🔥 MATCH FOUND\n{p['title']}\n{p['url']}")

        except:
            continue

    save_state(state)

    summary.append(f"Totals: found={total_found} new={new_hits}")

    tg_send("\n".join(summary))

if __name__ == "__main__":
    main()
