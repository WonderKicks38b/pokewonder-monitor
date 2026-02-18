import os
import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

def main():
    print("BOT_TOKEN length:", len(BOT_TOKEN))
    print("CHAT_ID:", CHAT_ID)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    r = requests.get(url, timeout=20)
    print("getMe status:", r.status_code)
    print("getMe body:", r.text)

    url2 = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r2 = requests.post(
        url2,
        data={"chat_id": CHAT_ID, "text": "🧪 TEST ALERT — if you see this, it works."},
        timeout=20,
    )
    print("sendMessage status:", r2.status_code)
    print("sendMessage body:", r2.text)

if __name__ == "__main__":
    main()



