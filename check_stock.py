import os
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

def tg_send(text: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": text
        },
        timeout=20,
    )
    print("Telegram response:", response.text)

def main():
    tg_send("🧪 TEST ALERT — PokeWonder connection successful.")

if __name__ == "__main__":
    main()

