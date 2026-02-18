import os
import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

print("BOT_TOKEN exists:", bool(BOT_TOKEN))
print("CHAT_ID exists:", bool(CHAT_ID))
print("CHAT_ID value:", CHAT_ID)

def main():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": "🧪 TEST ALERT — Debug version running."
    }

    response = requests.post(url, data=data)

    print("Status code:", response.status_code)
    print("Response text:", response.text)

if __name__ == "__main__":
    main()


