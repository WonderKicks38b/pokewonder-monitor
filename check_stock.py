import os
import json
import datetime
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

STATE_FILE = "state.json"

API_ENDPOINTS = [
    "https://www.pokemoncenter.com/api/search?category=trading-card-game&locale=en-gb",
    "https://www.pokemoncenter.com/api/search?category=new-releases&locale=en-gb",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

def tg(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing Telegram credentials")
        return
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg},
        timeout=15,
    )

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen": {}, "last_operational_date": None}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def main():
    state = load_state()
    today = datetime.date.today().isoformat()

    if state.get("last_operational_date") != today:
        tg(f"🟢 PokeWonder operational — {today}")
        state["last_operational_date"] = today

    total_products = 0

    utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = f"📊 PokeWonder scan summary — {utc_now}\n"

    for url in API_ENDPOINTS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                summary += f"- {url} : HTTP {r.status_code}\n"
                continue

            data = r.json()

            products = data.get("products", [])
            total_products += len(products)

            summary += f"- {url} : {len(products)} products returned\n"

        except Exception as e:
            summary += f"- {url} : ERROR\n"

    summary += f"\nTotals: {total_products} products found"

    if total_products == 0:
        summary += "\n⚠ No products returned from API."

    tg(summary)
    save_state(state)

if __name__ == "__main__":
    main()
