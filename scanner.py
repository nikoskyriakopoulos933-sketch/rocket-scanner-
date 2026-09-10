import requests, os

from datetime import datetime, timezone

 

TOKEN = os.environ["TELEGRAM_TOKEN"]

CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

 

def send(msg):

    requests.post(fhttps://api.telegram.org/bot{TOKEN}/sendMessage,

        json={"chat_id": CHAT_ID, "text": msg}, timeout=10)

 

try:

    r = requests.get(https://api.bybit.com/v5/market/tickers,

        params={"category":"linear"},

        headers={"User-Agent": "Mozilla/5.0"},

        timeout=15)

    data = r.json()

    count = len(data.get("result",{}).get("list",[]))

    send(f"✅ Bybit OK — {count} symbols\n⏰ {datetime.now(tz=timezone.utc).strftime('%H:%M UTC')}")

except Exception as e:

    send(f"❌ Bybit error: {e}")

 

print("Done")

