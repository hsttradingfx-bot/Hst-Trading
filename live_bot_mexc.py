"""
BOT 1 - ALERTES MULTI-CRYPTO - MEXC Futures (BTC, ETH, SOL)
===========================================================
Générateur de signaux Telegram pour exécution manuelle.
Aucune clé API MEXC requise (données de marché publiques).
"""

import hmac
import hashlib
import json
import time
import urllib.request
import urllib.error
import urllib.parse
import os
import bisect
from datetime import datetime

# ============================================================
# TELEGRAM (Identifiants configurés)
# ============================================================
TELEGRAM_BOT_TOKEN = "8831744843:AAE0-QkkzoglBIjte54M1xSKEVUYJVWdy_4"
TELEGRAM_CHAT_ID = "8831744843"


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
# CONFIGURATION DU SYSTEME
# ============================================================
BOT_NAME = "🤖 BOT 1 (H4/H1/M5)"
BASE_URL = "https://contract.mexc.com"

# Les 3 paires analysées simultanément
SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]

# Gestion du risque (Sprint initial)
RISK_PCT = 10.0
CAPITAL_ESTIME = 100.0  

# Paramètres techniques de la stratégie Bot 1
SWING_LEN_H4 = 2
SWING_LEN_H1 = 2
SWING_LEN_EXEC = 3
FVG_FILL_LEVEL = 0.5
ATR_LEN = 14
SL_BUFFER_ATR_MULT = 0.1

# ============================================================
# RECUPERATION DES DONNEES DE MARCHE
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
                "ts": d["time"][i], 
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
# INDICATEURS ET LOGIQUE DE STRATEGIE
# ============================================================
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


def compute_h4_bias_zones(candles, length):
    ph, pl = pivot_highs(candles, length), pivot_lows(candles, length)
    n = len(candles)
    zone_bull_top, zone_bull_bottom = [None] * n, [None] * n
    zone_bear_top, zone_bear_bottom = [None] * n, [None] * n
    last_ph = last_pl = None
    bull_stage, bull_creux, bull_sommet1 = 0, None, None
    bull_waiting_confirm, bull_pending_level, bull_sommet2_candidate = False, None, None
    bull_zone_active, bull_zone_top, bull_zone_bottom = False, None, None
    bear_stage, bear_sommet, bear_sommet1 = 0, None, None
    bear_waiting_confirm, bear_pending_level, bear_creux2_candidate = False, None, None
    bear_zone_active, bear_zone_top, bear_zone_bottom = False, None, None

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

    for i in range(n):
        c, o = candles[i]["close"], candles[i]["open"]
        if last_ph is not None and c > last_ph and not bull_waiting_confirm and bull_stage in (0, 1):
            bull_waiting_confirm, bull_pending_level = True, last_ph
            if bull_stage == 0:
                bull_creux = pl_last_before(pl, i)
        if bull_waiting_confirm and c < o:
            if bull_stage == 0:
                bull_sommet1, bull_stage = bull_pending_level, 1
            elif bull_stage == 1:
                bull_zone_top = bull_sommet2_candidate if bull_sommet2_candidate is not None else bull_pending_level
                bull_zone_bottom = bull_creux
                bull_zone_active = bull_zone_bottom is not None and bull_zone_top is not None and bull_zone_top > bull_zone_bottom
                if bull_zone_active:
                    bull_stage = 2
                else:
                    bull_stage, bull_sommet1, bull_sommet2_candidate, bull_creux = 0, None, None, None
            bull_waiting_confirm = False
        if bull_stage == 1 and ph[i] is not None and ph[i] > bull_sommet1:
            bull_sommet2_candidate = ph[i]
        if bull_zone_active and bull_zone_bottom is not None and c < bull_zone_bottom:
            bull_zone_active, bull_stage = False, 0
            bull_sommet1 = bull_sommet2_candidate = bull_creux = None
        zone_bull_top[i] = bull_zone_top if bull_zone_active else None
        zone_bull_bottom[i] = bull_zone_bottom if bull_zone_active else None

        if last_pl is not None and c < last_pl and not bear_waiting_confirm and bear_stage in (0, 1):
            bear_waiting_confirm, bear_pending_level = True, last_pl
            if bear_stage == 0:
                bear_sommet = ph_last_before(ph, i)
        if bear_waiting_confirm and c > o:
            if bear_stage == 0:
                bear_sommet1, bear_stage = bear_pending_level, 1
            elif bear_stage == 1:
                bear_zone_bottom = bear_creux2_candidate if bear_creux2_candidate is not None else bear_pending_level
                bear_zone_top = bear_sommet
                bear_zone_active = bear_zone_top is not None and bear_zone_bottom is not None and bear_zone_top > bear_zone_bottom
                if bear_zone_active:
                    bear_stage = 2
                else:
                    bear_stage, bear_sommet1, bear_creux2_candidate, bear_sommet = 0, None, None, None
            bear_waiting_confirm = False
        if bear_stage == 1 and pl[i] is not None and pl[i] < bear_sommet1:
            bear_creux2_candidate = pl[i]
        if bear_zone_active and bear_zone_top is not None and c > bear_zone_top:
            bear_zone_active, bear_stage = False, 0
            bear_sommet1 = bear_creux2_candidate = bear_sommet = None
        zone_bear_top[i] = bear_zone_top if bear_zone_active else None
        zone_bear_bottom[i] = bear_zone_bottom if bear_zone_active else None

        if ph[i] is not None:
            last_ph = ph[i]
        if pl[i] is not None:
            last_pl = pl[i]

    return zone_bull_top, zone_bull_bottom, zone_bear_top, zone_bear_bottom


def compute_h1_fvg_epa(candles):
    n = len(candles)
    fvg_bull_epa, fvg_bear_epa = [None] * n, [None] * n
    fvg_bull_bounds, fvg_bear_bounds = [None] * n, [None] * n
    active_bull_fvg = active_bear_fvg = None
    for i in range(2, n):
        c1, c3 = candles[i - 2], candles[i]
        if c3["low"] > c1["high"]:
            gap_bottom, gap_top = c1["high"], c3["low"]
            active_bull_fvg = (gap_bottom, gap_top, gap_bottom + (gap_top - gap_bottom) * FVG_FILL_LEVEL)
        if c3["high"] < c1["low"]:
            gap_top, gap_bottom = c1["low"], c3["high"]
            active_bear_fvg = (gap_bottom, gap_top, gap_bottom + (gap_top - gap_bottom) * (1 - FVG_FILL_LEVEL))
        if active_bull_fvg is not None and candles[i]["close"] < active_bull_fvg[0]:
            active_bull_fvg = None
        if active_bear_fvg is not None and candles[i]["close"] > active_bear_fvg[1]:
            active_bear_fvg = None
        fvg_bull_epa[i] = active_bull_fvg[2] if active_bull_fvg else None
        fvg_bear_epa[i] = active_bear_fvg[2] if active_bear_fvg else None
        fvg_bull_bounds[i] = (active_bull_fvg[0], active_bull_fvg[1]) if active_bull_fvg else None
        fvg_bear_bounds[i] = (active_bear_fvg[0], active_bear_fvg[1]) if active_bear_fvg else None
    return fvg_bull_epa, fvg_bear_epa, fvg_bull_bounds, fvg_bear_bounds


def compute_ob_signals(candles, length, ob_lookback=8):
    ph_raw, pl_raw = pivot_highs_raw(candles, length), pivot_lows_raw(candles, length)
    ph, pl = pivot_highs(candles, length), pivot_lows(candles, length)
    n = len(candles)
    ob_bull_signal, ob_bear_signal = [None] * n, [None] * n
    for i in range(n):
        if ph[i] is not None:
            low_keys = [k for k in pl_raw.keys() if k <= i]
            if low_keys:
                swing_bar = max(low_keys)
                for m in range(0, ob_lookback + 1):
                    idx = swing_bar - m
                    if idx < 0:
                        break
                    if candles[idx]["close"] < candles[idx]["open"]:
                        ob_bull_signal[i] = candles[idx]["low"]
                        break
        if pl[i] is not None:
            high_keys = [k for k in ph_raw.keys() if k <= i]
            if high_keys:
                swing_bar = max(high_keys)
                for m in range(0, ob_lookback + 1):
                    idx = swing_bar - m
                    if idx < 0:
                        break
                    if candles[idx]["close"] > candles[idx]["open"]:
                        ob_bear_signal[i] = candles[idx]["high"]
                        break
    return ob_bull_signal, ob_bear_signal


# ============================================================
# EXÉCUTION DE L'ANALYSE
# ============================================================
def analyze_asset(symbol):
    print(f"🔍 Scan en cours : {symbol}...")
    h4 = fetch_klines(symbol, "Hour4", limit_days=250)
    h1 = fetch_klines(symbol, "Min60", limit_days=80)
    m5 = fetch_klines(symbol, "Min5", limit_days=6)

    if not h4 or not h1 or not m5:
        return

    zbt, zbb, zrt, zrb = compute_h4_bias_zones(h4, SWING_LEN_H4)
    fvg_bull_epa, fvg_bear_epa, fvg_bull_bounds, fvg_bear_bounds = compute_h1_fvg_epa(h1)
    ob_bull_signal, ob_bear_signal = compute_ob_signals(m5, SWING_LEN_EXEC)
    atr_m5 = compute_atr(m5)

    i = len(m5) - 1
    h4_ts = [c["ts"] for c in h4]
    h1_ts = [c["ts"] for c in h1]
    
    idx_h4 = bisect.bisect_right(h4_ts, m5[i]["ts"]) - 1
    idx_h1 = bisect.bisect_right(h1_ts, m5[i]["ts"]) - 1

    if idx_h4 < 0 or idx_h1 < 0 or idx_h4 >= len(zbt) or idx_h1 >= len(fvg_bull_bounds):
        return

    zone_top_bull = zbt[idx_h4]
    zone_bottom_bull = zbb[idx_h4]
    epa_bull_bounds = fvg_bull_bounds[idx_h1]
    atr_val = atr_m5[i]

    fvg_in_zone = (zone_top_bull is not None and epa_bull_bounds is not None and
                   epa_bull_bounds[0] < zone_top_bull and epa_bull_bounds[1] > zone_bottom_bull)

    if fvg_in_zone and ob_bull_signal[i] is not None and atr_val is not None:
        entry_price = ob_bull_signal[i]
        sl = entry_price - atr_val * SL_BUFFER_ATR_MULT
        tp = zone_top_bull
        
        if tp > entry_price and sl < entry_price:
            risk_amount = CAPITAL_ESTIME * (RISK_PCT / 100)
            sl_dist = entry_price - sl
            qty = round(risk_amount / sl_dist, 4)
            ticker_name = symbol.split("_")[0]
            
            msg = (f"{BOT_NAME} - 🟢 *SIGNAL D'ACHAT {symbol}*\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"🔹 *Entrée (OB)* : `{entry_price}`\n"
                   f"🛑 *Stop Loss* : `{sl}`\n"
                   f"🎯 *Take Profit (H4 Top)* : `{tp}`\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"📊 *Gestion des risques :*\n"
                   f"▪️ Risque configuré : {RISK_PCT}%\n"
                   f"▪️ Capital virtuel : {CAPITAL_ESTIME} USDT\n"
                   f"🧮 *Taille de position estimée* : `{qty} {ticker_name}`")
                   
            send_telegram(msg)


def run_signal_check():
    print(f"[{datetime.now()}] 🚀 Lancement du scan global sur : {', '.join(SYMBOLS)}...")
    for symbol in SYMBOLS:
        analyze_asset(symbol)
    print(f"[{datetime.now()}] ✅ Fin du scan.")


if __name__ == "__main__":
    # --- TEST DE CONNEXION TELEGRAM AUTOMATIQUE ---
    print("🚀 Envoi d'un message de test à Telegram...")
    send_telegram("🔔 **TEST BOT 1** : Si tu reçois ce message, ton Token et ton Chat ID fonctionnent à 100% ! Prêt pour le scan.")
    
    # --- LANCEMENT DE L'ANALYSE ---
    run_signal_check()
