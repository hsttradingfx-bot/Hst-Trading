"""
BOT 1 - Bot de signal LIVE - Strategie H4 (biais 2 cassures) + H1 (1 cassure+FVG) + M5 (2 cassures+retest OB)
==========================================================================================================
Version ACHAT UNIQUEMENT, SANS Daily (H4 seul comme filtre macro), sur 3 paires : BTC, ETH, SOL.
Validee sur ~981 trades combines (3 actifs, walk-forward), win rate 52-67% selon l'actif et la periode.
Signal-only : envoie une notification Telegram par paire, ne passe AUCUN ordre.

Donnees via l'API publique MEXC Futures (meme exchange que celui utilise pour executer les trades,
donc plus besoin de comparer un prix Binance a un prix MEXC au moment d'agir sur un signal).

A executer via GitHub Actions toutes les 15 minutes (ou en local).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
import urllib.request
import bisect

# Module de journal automatique (doit etre dans le meme dossier / repo)
import trade_journal
import follow_logic

# ============================================================
# CONFIGURATION (identique a la version validee en backtest)
# ============================================================
SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]   # notation MEXC (underscore, pas comme Binance)
BASE_URL = "https://contract.mexc.com/api/v1/contract/kline/"

DAYS_BACK_H4 = 250
DAYS_BACK_H1 = 80
DAYS_BACK_M5 = 15

SWING_LEN_HTF = 2
SWING_LEN_H1 = 2
ATR_LEN = 14
SL_BUFFER_ATR_MULT = 0.15
OB_LOOKBACK = 8

# Message de confirmation TEMPORAIRE a chaque scan (pour verifier que le bot tourne).
# Mets a False (ou supprime ces 2 lignes + le bloc dans __main__) quand tu es rassure.
SEND_HEARTBEAT = False

MEXC_INTERVAL = {"Min5": "Min5", "Min60": "Min60", "Hour4": "Hour4"}
INTERVAL_SECONDS = {"Min5": 300, "Min60": 3600, "Hour4": 14400}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ============================================================
# RECUPERATION DES DONNEES (MEXC Futures - timestamps en SECONDES)
# ============================================================
def fetch_klines_batch(symbol, interval, start=None, end=None):
    mexc_interval = MEXC_INTERVAL[interval]
    url = f"{BASE_URL}{symbol}?interval={mexc_interval}"
    if start is not None:
        url += f"&start={start}"
    if end is not None:
        url += f"&end={end}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode())
    if not raw.get("success"):
        raise RuntimeError(f"Erreur API MEXC : {raw}")
    d = raw["data"]
    candles = []
    for i in range(len(d["time"])):
        candles.append({
            "ts": d["time"][i], "open": float(d["open"][i]), "high": float(d["high"][i]),
            "low": float(d["low"][i]), "close": float(d["close"][i]),
        })
    candles.sort(key=lambda c: c["ts"])
    return candles


def fetch_klines(symbol, interval, days_back):
    interval_sec = INTERVAL_SECONDS[interval]
    end_ts = int(time.time())
    start_ts = end_ts - days_back * 86400
    all_candles = []
    cur_end = end_ts
    safety_counter = 0
    max_batches = 200

    while safety_counter < max_batches:
        safety_counter += 1
        batch = fetch_klines_batch(symbol, interval, start=start_ts, end=cur_end)
        if not batch:
            break
        all_candles = batch + all_candles
        earliest = batch[0]["ts"]
        if earliest <= start_ts or len(batch) < 2:
            break
        cur_end = earliest - interval_sec
        time.sleep(0.15)

    seen = {c["ts"]: c for c in all_candles}
    result = sorted(seen.values(), key=lambda c: c["ts"])
    return [c for c in result if c["ts"] >= start_ts]


# ============================================================
# INDICATEURS
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
    ph = pivot_highs(candles, length)
    pl = pivot_lows(candles, length)
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
            bull_waiting_confirm = True
            bull_pending_level = bull_zone_top
            bull_sommet1 = bull_zone_top
            bull_creux = pl_last_before(pl, i)
            bull_stage = 1
            bull_sommet2_candidate = None

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

        if bull_stage == 1 and ph[i] is not None and bull_sommet1 is not None and ph[i] > bull_sommet1:
            bull_sommet2_candidate = ph[i]

        if bull_zone_active and bull_zone_bottom is not None and c < bull_zone_bottom:
            bull_zone_active, bull_stage = False, 0
            bull_sommet1 = bull_sommet2_candidate = bull_creux = None

        zone_bull_top[i] = bull_zone_top if bull_zone_active else None
        zone_bull_bottom[i] = bull_zone_bottom if bull_zone_active else None

        if ph[i] is not None:
            last_ph = ph[i]
        if pl[i] is not None:
            last_pl = pl[i]

    return zone_bull_top, zone_bull_bottom


def compute_single_bos_bull(candles, length):
    ph = pivot_highs(candles, length)
    pl = pivot_lows(candles, length)
    n = len(candles)
    bias_bull = [False] * n

    last_ph = last_pl = None
    waiting_bull, pending_bull = False, None
    waiting_bear, pending_bear = False, None
    current_bias = None

    for i in range(n):
        c, o = candles[i]["close"], candles[i]["open"]

        if last_ph is not None and c > last_ph and not waiting_bull:
            waiting_bull, pending_bull = True, last_ph
        if waiting_bull and c < o and c < pending_bull:
            current_bias = "bull"
            waiting_bull = False

        if last_pl is not None and c < last_pl and not waiting_bear:
            waiting_bear, pending_bear = True, last_pl
        if waiting_bear and c > o and c > pending_bear:
            current_bias = "bear"
            waiting_bear = False

        bias_bull[i] = current_bias == "bull"

        if ph[i] is not None:
            last_ph = ph[i]
        if pl[i] is not None:
            last_pl = pl[i]

    return bias_bull


def compute_h1_fvg_bull(candles):
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
                    if idx < 0:
                        break
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
# HEURE PARIS (CET/CEST)
# ============================================================
def cet_offset_hours(dt_utc):
    year = dt_utc.year
    march_sundays = [datetime(year, 3, d, tzinfo=timezone.utc) for d in range(25, 32) if datetime(year, 3, d).weekday() == 6]
    oct_sundays = [datetime(year, 10, d, tzinfo=timezone.utc) for d in range(25, 32) if datetime(year, 10, d).weekday() == 6]
    dst_start = march_sundays[-1].replace(hour=1)
    dst_end = oct_sundays[-1].replace(hour=1)
    return 2 if dst_start <= dt_utc < dst_end else 1


def now_paris_str():
    now_utc = datetime.now(timezone.utc)
    now_paris = now_utc + timedelta(hours=cet_offset_hours(now_utc))
    return now_paris.strftime("%H:%M")


# ============================================================
# TELEGRAM
# ============================================================
def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ATTENTION : TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID non configure. Message non envoye :")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print("Message Telegram envoye.")
    except Exception as e:
        print(f"Erreur envoi Telegram : {e}")


# ============================================================
# VERIFICATION DU SIGNAL (etat actuel uniquement, derniere bougie M5)
# ============================================================
def check_signal_for_symbol(symbol):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] Verification {symbol} (H4+H1+M5, sans Daily, achat uniquement)...")

    h4 = fetch_klines(symbol, "Hour4", DAYS_BACK_H4)
    h1 = fetch_klines(symbol, "Min60", DAYS_BACK_H1)
    m5 = fetch_klines(symbol, "Min5", DAYS_BACK_M5)

    if len(m5) < 50:
        print(f"  {symbol} : pas assez de bougies M5 recuperees, on abandonne ce cycle.")
        return None

    h4_zbt, h4_zbb = compute_two_bos_zones(h4, SWING_LEN_HTF)
    h1_bull_bos = compute_single_bos_bull(h1, SWING_LEN_H1)
    fvg_bull = compute_h1_fvg_bull(h1)
    m5_zbt, m5_zbb = compute_two_bos_zones(m5, SWING_LEN_HTF)
    ob_bull = compute_ob_bull_signals(m5, SWING_LEN_HTF, OB_LOOKBACK)
    atr_m5 = compute_atr(m5)

    h4_zbt_m5 = align(m5, h4, h4_zbt)
    h4_zbb_m5 = align(m5, h4, h4_zbb)
    h1_bull_bos_m5 = align(m5, h1, h1_bull_bos)
    fvg_bull_m5 = align(m5, h1, fvg_bull)

    i = len(m5) - 1

    macro_bull = h4_zbt_m5[i] is not None

    fvg_in_zone = False
    if h4_zbt_m5[i] is not None and fvg_bull_m5[i] is not None:
        gb, gt = fvg_bull_m5[i]
        fvg_in_zone = gb < h4_zbt_m5[i] and gt > h4_zbb_m5[i]

    signal_ok = (macro_bull and h1_bull_bos_m5[i] and fvg_in_zone
                 and m5_zbt[i] is not None and ob_bull[i] is not None and atr_m5[i] is not None)

    if signal_ok:
        entry_price = ob_bull[i]
        sl = entry_price - atr_m5[i] * SL_BUFFER_ATR_MULT
        tp = m5_zbt[i]
        rr_ratio = (tp - entry_price) / (entry_price - sl)
        epa_bas, epa_haut = fvg_bull_m5[i]

        message = (
            f"🚨 *SIGNAL STRATÉGIE EPA/OB – {symbol}* 🚨\n"
            f"---\n"
            f"📈 *Direction* : BUY / LONG ONLY 🟢\n"
            f"🕒 *Session* : {now_paris_str()} (Vérification Horaire OK ✅)\n\n"
            f"🎯 *Entrée (OB M5)* : {entry_price:.4f} USDT\n"
            f"🛑 *Stop Loss (ATR)* : {sl:.4f} USDT\n"
            f"🏁 *Take Profit (H4)* : {tp:.4f} USDT\n"
            f"📐 *Ratio R:R Théorique* : 1:{rr_ratio:.1f}\n\n"
            f"🔍 _Contexte Technique_ : \n"
            f"Prix situé au cœur de l'EPA H1 [{epa_bas:.4f} - {epa_haut:.4f}]. Biais Macro H4 Confirmé.\n"
            f"---"
        )
        print(message)
        send_telegram_message(message)

        # --- Journal : enregistrer ce signal comme trade ouvert ---
        # dedup_key = symbole + timestamp de la bougie M5 d'entree (unique par setup)
        dedup_key = f"bot1:{symbol}:{m5[i]['ts']}"
        r_unit = entry_price - sl
        registered = trade_journal.register_signal(
            bot="bot1", symbol=symbol, direction="long",
            entry_price=entry_price, sl=sl, tp_final=tp,
            r_unit=r_unit, dedup_key=dedup_key,
        )
        if registered:
            print(f"  {symbol} : trade enregistre au journal (bot1).")
    else:
        print(f"  {symbol} : aucun signal pour l'instant.")

    # On renvoie les bougies M5 pour permettre le suivi des trades ouverts
    return m5


def check_all_symbols():
    m5_by_symbol = {}
    for symbol in SYMBOLS:
        try:
            m5 = check_signal_for_symbol(symbol)
            if m5:
                m5_by_symbol[symbol] = m5
        except Exception as e:
            print(f"  {symbol} : erreur pendant la verification -> {e}")

    # --- Journal : suivre les trades bot1 encore ouverts avec les prix recents ---
    try:
        trade_journal.update_open_trades("bot1", m5_by_symbol, follow_logic.follow_bot1)
    except Exception as e:
        print(f"  Erreur suivi journal bot1 : {e}")


if __name__ == "__main__":
    # --- Message de confirmation TEMPORAIRE (a enlever une fois rassure) ---
    # Il confirme, a chaque scan, que le bot tourne bien meme s'il n'y a pas de signal.
    if SEND_HEARTBEAT:
        send_telegram_message(f"🔍 BOT 1 : scan lancé à {now_paris_str()} (Paris), je vérifie BTC/ETH/SOL...")

    check_all_symbols()
