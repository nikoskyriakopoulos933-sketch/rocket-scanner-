import requests
import time
import csv
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BYBIT_URL = "https://api.bybit.com/v5"
LOG_FILE = "scanner_signals_log.csv"
SENT_FILE = "scanner_sent_signals.txt"

def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except:
        pass

def log_signal(sig, score, dt_str):
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["datetime_utc","symbol","chg","vol_M","qr","spike","oi_spike","score"])
        w.writerow([dt_str, sig["sym"], round(sig["chg"],2), round(sig["vol"],3),
                    round(sig["quiet"],1), round(sig["spike"],2), round(sig["oi_spike"],2), score])
        f.flush()

# Load dedup set
sent = set()
if os.path.exists(SENT_FILE):
    with open(SENT_FILE) as f:
        sent = set(line.strip() for line in f if line.strip())

# Step 1: Universe — ≥$3M daily turnover
r = requests.get(f"{BYBIT_URL}/market/instruments-info",
    params={"category":"linear","limit":1000}, timeout=15).json()
all_syms = [s["symbol"] for s in r["result"]["list"]
            if s["symbol"].endswith("USDT") and s["status"]=="Trading"]
tickers = requests.get(f"{BYBIT_URL}/market/tickers",
    params={"category":"linear"}, timeout=15).json()["result"]["list"]
ticker_map = {t["symbol"]: t for t in tickers}
universe = [s for s in all_syms
            if s in ticker_map and
            float(ticker_map[s].get("turnover24h", 0)) >= 3_000_000]

print(f"Universe: {len(universe)} symbols")

# Step 2: Scan — i=1 and i=2 double-check
def fetch_and_check(sym):
    try:
        rows = requests.get(f"{BYBIT_URL}/market/kline",
            params={"category":"linear","symbol":sym,"interval":"240","limit":50},
            timeout=10).json()["result"]["list"]
        if len(rows) < 35:
            return None
        results = []
        for i in [1, 2]:
            ts = int(rows[i][0])
            dedup_key = f"{sym}_{ts}"
            if dedup_key in sent:
                continue
            sv = float(rows[i][6])
            if sv < 700_000:
                continue
            p30 = [float(rows[i+j][6]) for j in range(1, 31)]
            a30 = sum(p30) / 30
            if a30 < 1:
                continue
            qr = sv / a30
            if qr < 10:
                continue
            p6 = [float(rows[i+j][6]) for j in range(1, 7)]
            a6 = sum(p6) / 6
            sp = sv / a6 if a6 > 0 else 0
            if not (5.0 <= sp <= 7.0):
                continue
            o, c = float(rows[i][1]), float(rows[i][4])
            chg = (c - o) / o * 100
            if not (10 <= chg <= 25):
                continue
            n1 = rows[i - 1]
            conf1 = float(n1[4]) > float(n1[1])
            if not conf1:
                continue
            n2 = rows[i + 1]
            conf2 = float(n2[4]) > float(n2[1])
            if not conf2:
                continue
            entry = float(ticker_map.get(sym, {}).get("lastPrice", c))
            results.append({"sym": sym, "entry": entry, "signal_close": c,
                    "chg": chg, "vol": sv / 1e6, "quiet": qr, "spike": sp,
                    "ts": ts, "dedup_key": dedup_key, "candle_idx": i})
        return results if results else None
    except:
        return None

base_signals = []
with ThreadPoolExecutor(max_workers=20) as ex:
    futs = {ex.submit(fetch_and_check, sym): sym for sym in universe}
    for fut in as_completed(futs):
        res = fut.result()
        if res:
            base_signals.extend(res)

# Step 3: OI spike ≥1.2x
def check_oi(sig):
    try:
        oi_list = requests.get(f"{BYBIT_URL}/market/open-interest",
            params={"category":"linear","symbol":sig["sym"],
                    "intervalTime":"4h","limit":10},
            timeout=10).json()["result"]["list"]
        if len(oi_list) < 4:
            return None
        oi_now = float(oi_list[0]["openInterest"])
        prev = [float(x["openInterest"]) for x in oi_list[1:7]]
        avg = sum(prev) / len(prev)
        if avg <= 0:
            return None
        oi_s = oi_now / avg
        sig["oi_spike"] = oi_s
        return sig if oi_s >= 1.2 else None
    except:
        return None

premium = []
with ThreadPoolExecutor(max_workers=10) as ex:
    for fut in as_completed([ex.submit(check_oi, s) for s in base_signals]):
        res = fut.result()
        if res:
            premium.append(res)

# Step 4: Score
def compute_score(sig):
    pts = 0
    v = sig["vol"]
    if v < 5:     pts += 3
    elif v < 15:  pts += 2
    elif v < 35:  pts += 1
    c = sig["chg"]
    if c < 15:   pts += 2
    elif c < 20: pts += 1
    if sig["oi_spike"] >= 2.0: pts += 1
    return pts

# Step 5: Send alerts
BUDGET = 3000
for sig in sorted(premium, key=lambda x: -x.get("oi_spike", 0)):
    score = compute_score(sig)
    sp = sig["spike"]
    oi_s = sig["oi_spike"]
    qr = sig["quiet"]
    signal_close = sig["signal_close"]
    confirmed = " ✅ CONFIRMED" if sig["candle_idx"] == 2 else ""

    if score >= 5:   stars = "🚀🚀🚀"; rating = "MEGA POTENTIAL"; conviction = "HIGH CONVICTION"
    elif score >= 3: stars = "🚀🚀";   rating = "STRONG SIGNAL";  conviction = "HIGH CONVICTION"
    else:            stars = "🚀";     rating = "GOOD SIGNAL";    conviction = "CONFIRMED"

    if qr >= 100:  qr_lbl = f"🔥 {qr:.0f}x — ΕΞΑΙΡΕΤΙΚΟ"
    elif qr >= 50: qr_lbl = f"🔥 {qr:.0f}x — Πολύ ισχυρό"
    elif qr >= 20: qr_lbl = f"✅ {qr:.0f}x — Ισχυρό"
    else:          qr_lbl = f"✅ {qr:.0f}x — Καλό"

    oi_lbl = "🔥" if oi_s >= 2.0 else "✅"
    limit_10 = signal_close * 0.90
    limit_15 = signal_close * 0.85
    tp_price = signal_close * 1.80
    sl_price = signal_close * 0.80
    half = BUDGET / 2
    dt = datetime.fromtimestamp(sig["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    msg  = f"{stars} <b>PREMIUM SIGNAL — {conviction}{confirmed}</b>\n"
    msg += f"<b>{sig['sym']}  —  {rating}</b>\n\n"
    msg += f"<b>── ENTRY (Budget: ${BUDGET:,}) ──</b>\n"
    msg += f"🎯 Limit 1: <b>${limit_10:.6f}</b>  (-10%)  → ${half:,.0f}\n"
    msg += f"🎯 Limit 2: <b>${limit_15:.6f}</b>  (-15%)  → ${half:,.0f}\n\n"
    msg += f"🟢 <b>TP:</b>  ${tp_price:.6f}  (+80%)\n"
    msg += f"📉 <b>Trail SL:</b> -35% από peak\n"
    msg += f"🛡 <b>SL:</b>  ${sl_price:.6f}  (-20%)\n\n"
    msg += f"<b>── SIGNAL ──</b>\n"
    msg += f"💰 Signal close: ${signal_close:.6f}\n"
    msg += f"📊 QR:     {qr_lbl}\n"
    msg += f"📈 Chg:    +{sig['chg']:.1f}%\n"
    msg += f"💵 Vol:    ${sig['vol']:.2f}M USDT\n"
    msg += f"🌀 Spike:  {sp:.1f}x ✅\n"
    msg += f"📐 OI:     {oi_s:.2f}x {oi_lbl}\n"
    msg += f"✅ Conf1 🟢 | Conf2 🟢\n\n"
    msg += f"<b>── SCORE: {score}/6 ──</b>\n"
    msg += f"⚠️ <i>Not financial advice — signal only</i>\n"
    msg += f"⏰ {dt}"

    send_telegram(msg)
    log_signal(sig, score, dt)

    with open(SENT_FILE, "a") as f:
        f.write(sig["dedup_key"] + "\n")
    sent.add(sig["dedup_key"])
    time.sleep(1)

print(f"Done. {len(premium)} premium signals sent.")
