"""
BOT 2 (LIVE) - STRATÉGIE COMBINÉE EPA / FVG / OB - MEXC Futures
===================================================================
Recherche de setups : Zone EPA H4 + FVG H1 + Retest M5 + OB M5.
Alerte envoyée à la clôture de la bougie de confirmation pour 
permettre le placement d'un Ordre Limite à l'avance.
Aucune clé API requise (données publiques du marché).
"""

import json
import time
import urllib.request
import urllib.parse
import os
import bisect
from datetime import datetime, timedelta, timezone

# ============================================================
# TELEGRAM & CONFIGURATION DU COMPTE
# ============================================================
TELEGRAM_BOT_TOKEN = "8831744843:AAE0-QkkzoglBIjte54M1xSKEVUYJVWdy_4"
TELEGRAM_CHAT_ID = "8356059748"

BOT_NAME = "🤖 BOT 2 (EPA/FVG/OB - 10%)"
BASE_URL = "https://contract.mexc.com"
SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]

# Gestion stricte des risques (Demande : 10% par position)
RISK_PCT = 10.0
CAPITAL_ESTIME = 100.0  

# Paramètres techniques issus du Backtest
SWING_LEN_HTF = 2
SWING_LEN_H1 = 2
ATR_LEN = 14
SL_BUFFER_ATR_MULT = 0.15
OB_LOOKBACK = 8

# Objectifs de sortie en R-Multiple
TP1_R = 5.0
TP2_R = 10.0

# Plages horaires d'entrée autorisées (Heure de Paris)
ENTRY_HOURS_PARIS = {9, 10, 16}

# ============================================================
# ENVOI TELEGRAM
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"Erreur envoi Telegram : {e}")

# ============================================================
# RÉCUPÉRATION DES DONNÉES MEXC
# ============================================================
def fetch_klines(symbol, interval, limit_days=10):
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - (limit_days * 86400 * 1000)
    
    url = f"{BASE_URL}/api/v1/contract/kline/{symbol}?interval={interval}&start={start_ts}&end={end_ts}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
        
        if "data" not in raw or not raw["data"] or "time" not in raw["data"]:
            return []
            
        d = raw["data"]
        candles = []
        for i in range(len(d["time"])):
            candles.append({
                "ts": d["time"][i] // 1000,  # Conversion en secondes pour le moteur mathématique
                "open": float(d["open"][i]), 
                "high": float(d["high"][i]),
                "low": float(d["low"][i]), 
                "close": float(d["close"][i]),
            })
        return candles
    except Exception as e:
        print(f"Erreur klines {symbol} ({interval}) : {e}")
        return []

# ============================================================
# MOTEUR TECHNIQUE & INDICATEURS (100% Fidèle au Backtest)
# ============================================================
def compute_atr(candles, length=ATR_LEN):
    atr_vals = [None] * len(candles)
    trs = []
    for i in range(len(candles)):
        tr = candles[i]["high"] - candles[i]["low"] if i == 0 else max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]))
        trs.append(tr)
        if i >= length - 1:
            atr_vals[i] = sum(trs[i - length + 1:i + 1]) / length
    return atr_vals

def pivot_highs(candles, length):
    result = [None] * len(candles)
    for i in range(length, len(candles) - length):
        window = [candles[j]["high"] for j in range(i - length, i + length + 1)]
        center = candles[i]["high"]
        if center == max(window) and window.count(center) == 1:
            result[i + length] = center
    return result

def pivot_lows(candles, length):
    result = [None] * len(candles)
    for i in range(length, len(candles) - length):
        window = [candles[j]["low"] for j in range(i - length, i + length + 1)]
        center = candles[i]["low"]
        if center == min(window) and window.count(center) == 1:
            result[i + length] = center
    return result

def pivot_highs_raw(candles, length):
    result = {}
    for i in range(length, len(candles) - length):
        window = [candles[j]["high"] for j in range(i - length, i + length + 1)]
        center = candles[i]["high"]
        if center == max(window) and window.count(center) == 1:
            result[i] = center
    return result

def pivot_lows_raw(candles, length):
    result = {}
    for i in range(length, len(candles) - length):
        window = [candles[j]["low"] for j in range(i - length, i + length + 1)]
        center = candles[i]["low"]
        if center == min(window) and window.count(center) == 1:
            result[i] = center
    return result

def pl_last_before(pl, i):
    for k in range(i, -1, -1):
        if pl[k] is not None:
            return pl[k]
    return None

def ph_last_before(ph, i):
    for k in range(i, -1, -1):
        if ph[k] is not None:
            return ph[k]
    return None

def compute_two_bos_zones(candles, length):
    ph, pl = pivot_highs(candles, length), pivot_lows(candles, length)
    n = len(candles)
    zone_bull_top, zone_bull_bottom = [None] * n, [None] * n
    last_ph = last_pl = None
    bull_stage, bull_creux, bull_sommet1 = 0, None, None
    bull_waiting_confirm, bull_pending_level, bull_sommet2_candidate = False, None, None
    bull_zone_active, bull_zone_top, bull_zone_bottom = False, None, None

    for i in range(n):
        c, o = candles[i]["close"], candles[i]["open"]
        if last_ph is not None and c > last_ph and not bull_waiting_confirm and bull_stage in (0, 1):
            bull_waiting_confirm, bull_pending_level = True, last_ph
            if bull_stage == 0:
                bull_creux = pl_last_before(pl, i)

        if bull_zone_active and bull_zone_top is not None and c > bull_zone_top and not bull_waiting_confirm:
            bull_waiting_confirm, bull_pending_level = True, bull_zone_top
            bull_sommet1, bull_creux, bull_stage = bull_zone_top, pl_last_before(pl, i), 1
            bull_sommet2_candidate = None

        if bull_waiting_confirm and c < o:
            if bull_stage == 0:
                bull_sommet1, bull_stage = bull_pending_level, 1
            elif bull_stage == 1:
                bull_zone_top = bull_sommet2_candidate if bull_sommet2_candidate is not None else bull_pending_level
                bull_zone_bottom = bull_creux
                bull_zone_active = bull_zone_bottom is not None and bull_zone_top is not None and bull_zone_top > bull_zone_bottom
                bull_stage = 2 if bull_zone_active else 0
                if not bull_zone_active:
                    bull_sommet1 = bull_sommet2_candidate = bull_creux = None
            bull_waiting_confirm = False

        if bull_stage == 1 and ph[i] is not None and bull_sommet1 is not None and ph[i] > bull_sommet1:
            bull_sommet2_candidate = ph[i]

        if bull_zone_active and bull_zone_bottom is not None and c < bull_zone_bottom:
            bull_zone_active, bull_stage = False, 0
            bull_sommet1 = bull_sommet2_candidate = bull_creux = None

        zone_bull_top[i] = bull_zone_top if bull_zone_active else None
        zone_bull_bottom[i] = bull_zone_bottom if bull_zone_active else None

        if ph[i] is not None: last_ph = ph[i]
        if pl[i] is not None: last_pl = pl[i]

    return zone_bull_top, zone_bull_bottom

def compute_fvg_bull(candles):
    n = len(candles)
    fvg_bull_bounds = [None] * n
    active_bull = None
    for i in range(2, n):
        c1, c3 = candles[i - 2], candles[i]
        if c3["low"] > c1["high"]:
            active_bull = (c1["high"], c3["low"])
        if active_bull is not None and candles[i]["close"] < active_bull[0]:
            active_bull = None
        fvg_bull_bounds[i] = active_bull
    return fvg_bull_bounds

def compute_ob_bull_signals(candles, length, ob_lookback=OB_LOOKBACK):
    pl_raw = pivot_lows_raw(candles, length)
    ph = pivot_highs(candles, length)
    n = len(candles)
    ob_bull_signal = [None] * n
    for i in range(n):
        if ph[i] is not None:
            low_keys = [k for k in pl_raw.keys() if k <= i]
            if low_keys:
                swing_bar = max(low_keys)
                for m in range(0, ob_lookback + 1):
                    idx = swing_bar - m
                    if idx < 0: break
                    if candles[idx]["close"] < candles[idx]["open"]:
                        ob_bull_signal[i] = candles[idx]["low"]
                        break
    return ob_bull_signal

def align(target_candles, source_candles, source_values):
    source_ts = [c["ts"] for c in source_candles]
    aligned = []
    for c in target_candles:
        idx = bisect.bisect_right(source_ts, c["ts"]) - 1
        aligned.append(source_values[idx] if idx >= 0 else None)
    return aligned

# ============================================================
# CONVERSION GESTION HORAIRE PARIS
# ============================================================
def cet_offset_hours(dt_utc):
    year = dt_utc.year
    march_sundays = [datetime(year, 3, d, tzinfo=timezone.utc) for d in range(25, 32) if datetime(year, 3, d).weekday() == 6]
    oct_sundays = [datetime(year, 10, d, tzinfo=timezone.utc) for d in range(25, 32) if datetime(year, 10, d).weekday() == 6]
    return 2 if march_sundays[-1].replace(hour=1) <= dt_utc < oct_sundays[-1].replace(hour=1) else 1

def to_cet_datetime(ts_seconds):
    dt_utc = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
    return dt_utc + timedelta(hours=cet_offset_hours(dt_utc))

# ============================================================
# SCANNEUR DU MARCHÉ TEMP RÉEL
# ============================================================
def analyze_asset(symbol):
    print(f"🔍 Scan Bot 2 en cours : {symbol}...")
    h4 = fetch_klines(symbol, "Hour4", limit_days=250)
    h1 = fetch_klines(symbol, "Min60", limit_days=80)
    m5 = fetch_klines(symbol, "Min5", limit_days=6)

    if not h4 or not h1 or not m5:
        return

    h4_zbt, h4_zbb = compute_two_bos_zones(h4, SWING_LEN_HTF)
    fvg_bull = compute_fvg_bull(h1)
    ob_bull = compute_ob_bull_signals(m5, SWING_LEN_HTF, OB_LOOKBACK)
    atr_m5 = compute_atr(m5)

    h4_zbt_m5 = align(m5, h4, h4_zbt)
    h4_zbb_m5 = align(m5, h4, h4_zbb)
    fvg_bull_m5 = align(m5, h1, fvg_bull)

    # Index de la dernière bougie m5 close
    i = len(m5) - 2 

    if h4_zbt_m5[i] is None or fvg_bull_m5[i] is None or atr_m5[i] is None:
        return

    # 1. Filtre horaire strict (Paris)
    if to_cet_datetime(m5[i]["ts"]).hour not in ENTRY_HOURS_PARIS:
        return

    # 2. Vérification des alignements structurels
    macro_bull = h4_zbt_m5[i] is not None
    gb, gt = fvg_bull_m5[i]
    fvg_in_h4_zone = gb < h4_zbt_m5[i] and gt > h4_zbb_m5[i]
    retest_fvg = m5[i]["low"] <= gt and m5[i]["high"] >= gb
    ob_present = ob_bull[i] is not None

    if macro_bull and fvg_in_h4_zone and retest_fvg and ob_present:
        entry_price = ob_bull[i]
        atr_val = atr_m5[i]
        sl = entry_price - atr_val * SL_BUFFER_ATR_MULT
        r_unit = entry_price - sl
        
        tp1 = entry_price + (TP1_R * r_unit)
        tp2 = entry_price + (TP2_R * r_unit)
        tp_final = h4_zbt_m5[i]

        current_price = m5[-1]["close"]
        
        # Envoi de l'alerte uniquement si le prix actuel permet de poser la limite à l'avance
        if current_price > entry_price and tp_final > entry_price and sl < entry_price and r_unit > 0:
            risk_amount = CAPITAL_ESTIME * (RISK_PCT / 100)
            qty = round(risk_amount / r_unit, 3)
            ticker_name = symbol.split("_")[0]

            msg = (f"{BOT_NAME}\n"
                   f"🟢 *ORDRE LIMITE CONFIGURÉ - {symbol}*\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"⏳ _Setup validé ! Place ton ordre passif._\n\n"
                   f"🔹 *Type* : `LIMIT (Achat / Long)`\n"
                   f"🔹 *Prix d'Entrée (OB)* : `{entry_price}`\n"
                   f"🛑 *Stop Loss* : `{sl}`\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"🎯 *PLAN DE SORTIE (3 Paliers) :*\n"
                   f"▪️ *Take Profit 1 (+5R)* : `{tp1}` _(Prendre 40% & BE)_\n"
                   f"▪️ *Take Profit 2 (+10R)* : `{tp2}` _(Prendre 30% & SL à +5R)_\n"
                   f"🎯 *TP Final (H4 Top)* : `{tp_final}` _(Runner 30%)_\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"📊 *Gestion des risques (RISQUE 10%) :*\n"
                   f"▪️ Risque total engagé : {risk_amount} USDT\n"
                   f"🧮 *Taille à saisir* : `{qty} {ticker_name}`")
            
            send_telegram(msg)

def run_signal_check():
    print(f"[{datetime.now()}] 🚀 Lancement du scan global Bot 2 sur : {', '.join(SYMBOLS)}...")
    for symbol in SYMBOLS:
        analyze_asset(symbol)
    print(f"[{datetime.now()}] ✅ Fin du scan.")

if __name__ == "__main__":
    # Message de démarrage automatique pour valider le canal
    print("🚀 Initialisation du Bot 2...")
    send_telegram("🔔 **BOT 2 (EPA/FVG/OB) ACTIVÉ** : Version Live 10% Risque opérationnelle sur BTC, ETH et SOL !")
    run_signal_check()
