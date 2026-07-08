"""
BOT LIVE SIGNAL-ONLY (EPA/IPA/FT) - pour GitHub Actions
========================================================

Reprend EXACTEMENT la logique validee au backtest :
  - Biais H4 : zone EPA (compute_two_bos_zones) - 2 cassures + cloture de rejet,
    zone active tant que le point A n'est pas recasse.
  - FVG H1 forme A L'INTERIEUR de la zone EPA.
  - OB M5 identifie comme zone d'entree potentielle.

DIFFERENCE CLE avec le backtest (demande explicite) :
  Le signal est envoye EN AVANCE, des qu'un OB M5 valide est identifie comme
  cible SOUS le prix courant -> l'utilisateur place un ordre limite sur l'OB
  et attend que le prix vienne le chercher. On N'ATTEND PAS le retest.

  Sequence de declenchement du signal :
    1. EPA H4 actif (zone valide)
    2. FVG H1 dans la zone EPA
    3. OB M5 identifie sous le prix courant
    4. Heure de Paris dans les plages autorisees (9-11h ou 16-17h)
    -> SIGNAL ENVOYE (ordre limite a preparer sur l'OB)

Gestion communiquee dans le signal (a executer par l'utilisateur) :
    - Entree : niveau de l'OB (ordre limite)
    - SL     : OB - buffer ATR
    - TP1    : +5R  (sortir 40%, passer au break-even)
    - TP2    : +10R (sortir 30%, stop du reste a +5R)
    - Runner : 30% jusqu'au point B (haut de la zone EPA H4)

Signal-only : aucun ordre passe. Notification Telegram uniquement.
Anti-doublon : un meme setup (meme OB, meme zone) n'est signale qu'une fois,
via un fichier d'etat leger commite dans le repo par le workflow.

100% Python standard (urllib), aucune dependance a installer.
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
import urllib.request
import urllib.parse
import bisect

# Module de journal automatique (meme dossier / repo)
import trade_journal
import follow_logic

# ============================================================
# CONFIGURATION
# ============================================================
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
BASE_URL = https://api.bybit.com/v5/market/kline

# On ne recupere que ce qu'il faut pour evaluer l'etat courant (pas 400 jours)
LOOKBACK_H4 = 400     # bougies H4 (~66 jours)
LOOKBACK_H1 = 500     # bougies H1 (~20 jours)
LOOKBACK_M5 = 500     # bougies M5 (~1.7 jour)

SWING_LEN_HTF = 2
ATR_LEN = 14
SL_BUFFER_ATR_MULT = 0.15
OB_LOOKBACK = 8

# Plages horaires d'entree (heure de Paris) : 9-11h (heures 9,10) et 16-17h (heure 16)
ENTRY_HOURS_PARIS = {9, 10, 16}

# Gestion de sortie (communiquee dans le signal)
TP1_R = 5.0
TP1_FRACTION = 0.4
TP2_R = 10.0
TP2_FRACTION = 0.3
RUNNER_FRACTION = 0.3
RUNNER_STOP_AFTER_TP2_R = 5.0

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signaled_state.json")

BINANCE_INTERVAL = {"Min5": "5m", "Min60": "1h", "Hour4": "4h"}


# ============================================================
# RECUPERATION DES DONNEES (Binance Futures, urllib pur)
# ============================================================
def fetch_klines(symbol, interval, limit):
    binance_interval = BINANCE_INTERVAL[interval]
    url = f"{BASE_URL}?symbol={symbol}&interval={binance_interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read().decode())
    if isinstance(raw, dict) and raw.get("code"):
        raise RuntimeError(f"Erreur API Binance {symbol} {interval}: {raw}")
    candles = []
    for row in raw:
        candles.append({
            "ts": row[0] // 1000,
            "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]),
        })
    return candles


# ============================================================
# INDICATEURS (identiques au backtest)
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


def compute_two_bos_zones(candles, length):
    """Zone EPA (identique au backtest)."""
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
                    if idx < 0:
                        break
                    if candles[idx]["close"] < candles[idx]["open"]:
                        ob_bull_signal[i] = candles[idx]["low"]
                        break
    return ob_bull_signal


def align_last(target_ts, source_candles, source_values):
    """Valeur source active au timestamp target_ts (dernier <= target_ts)."""
    source_ts = [c["ts"] for c in source_candles]
    idx = bisect.bisect_right(source_ts, target_ts) - 1
    return source_values[idx] if idx >= 0 else None


# ============================================================
# HEURE PARIS
# ============================================================
def cet_offset_hours(dt_utc):
    year = dt_utc.year
    march_sundays = [datetime(year, 3, d, tzinfo=timezone.utc) for d in range(25, 32) if datetime(year, 3, d).weekday() == 6]
    oct_sundays = [datetime(year, 10, d, tzinfo=timezone.utc) for d in range(25, 32) if datetime(year, 10, d).weekday() == 6]
    dst_start = march_sundays[-1].replace(hour=1)
    dst_end = oct_sundays[-1].replace(hour=1)
    return 2 if dst_start <= dt_utc < dst_end else 1


def to_paris(ts_seconds):
    dt_utc = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
    return dt_utc + timedelta(hours=cet_offset_hours(dt_utc))


# ============================================================
# ETAT (anti-doublon)
# ============================================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        print(f"[WARN] impossible d'ecrire l'etat: {e}")


# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram non configure (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"[ERROR] envoi Telegram: {e}")


# ============================================================
# DETECTION DU SETUP (signal anticipe, avant le touch de l'OB)
# ============================================================
def check_symbol(symbol, state):
    h4 = fetch_klines(symbol, "Hour4", LOOKBACK_H4)
    h1 = fetch_klines(symbol, "Min60", LOOKBACK_H1)
    m5 = fetch_klines(symbol, "Min5", LOOKBACK_M5)

    h4_zbt, h4_zbb = compute_two_bos_zones(h4, SWING_LEN_HTF)
    fvg_bull = compute_fvg_bull(h1)
    ob_bull = compute_ob_bull_signals(m5, SWING_LEN_HTF, OB_LOOKBACK)
    atr_m5 = compute_atr(m5)

    # On evalue sur la DERNIERE bougie M5 cloturee (indice -2 pour eviter la bougie en cours)
    i = len(m5) - 2
    if i < 0:
        return
    c = m5[i]
    ts = c["ts"]

    # 1) Heure de Paris dans les plages autorisees
    if to_paris(ts).hour not in ENTRY_HOURS_PARIS:
        return

    # 2) EPA H4 actif a cet instant
    zone_top = align_last(ts, h4, h4_zbt)
    zone_bottom = align_last(ts, h4, h4_zbb)
    if zone_top is None or zone_bottom is None:
        return

    # 3) FVG H1 dans la zone EPA
    fvg = align_last(ts, h1, fvg_bull)
    if fvg is None:
        return
    gb, gt = fvg
    fvg_in_zone = gb < zone_top and gt > zone_bottom
    if not fvg_in_zone:
        return

    # 4) OB M5 identifie SOUS le prix courant (cible d'entree potentielle)
    ob_level = ob_bull[i]
    if ob_level is None:
        return
    price_now = c["close"]
    if ob_level >= price_now:
        # l'OB doit etre sous le prix (on veut un ordre limite d'achat en-dessous)
        return

    if atr_m5[i] is None:
        return

    # --- Calcul des niveaux du plan de trade ---
    entry = ob_level
    sl = entry - atr_m5[i] * SL_BUFFER_ATR_MULT
    r_unit = entry - sl
    tp_final = zone_top  # point B / haut zone EPA H4
    if not (tp_final > entry and sl < entry and r_unit > 0):
        return

    tp1 = entry + TP1_R * r_unit
    tp2 = entry + TP2_R * r_unit

    # --- Anti-doublon : cle = symbole + niveau OB arrondi + zone_top arrondie ---
    sig_key = f"{symbol}:{round(entry, 2)}:{round(tp_final, 2)}"
    if state.get(symbol) == sig_key:
        return  # deja signale ce meme setup

    state[symbol] = sig_key

    msg = (
        f"SIGNAL (anticipe) - {symbol}\n"
        f"Structure EPA haussiere active | FVG H1 dans la zone\n"
        f"Plage horaire OK ({to_paris(ts).strftime('%H:%M')} Paris)\n"
        f"\n"
        f"-> Place un ordre LIMITE d'achat sur l'OB M5 :\n"
        f"   Entree (OB) : {entry:.2f}\n"
        f"   SL          : {sl:.2f}  (1R = {r_unit:.2f})\n"
        f"   TP1 (+5R)   : {tp1:.2f}  -> sortir 40%, passer BE\n"
        f"   TP2 (+10R)  : {tp2:.2f}  -> sortir 30%, stop du reste a +5R\n"
        f"   Runner 30%  : jusqu'au point B {tp_final:.2f}\n"
        f"\n"
        f"Prix actuel : {price_now:.2f} (l'OB est sous le prix, attends le retour)"
    )
    send_telegram(msg)
    print(msg)

    # --- Journal : enregistrer le signal ANTICIPE (en attente d'entree sur l'OB) ---
    # Comme le bot 2 signale AVANT que le prix touche l'OB, on stocke l'ordre
    # limite en attente. Le suivi (follow_bot2_pending) verifiera d'abord si le
    # prix a touche l'OB (entree) avant d'appliquer les paliers.
    dedup_key = f"bot2:{symbol}:{round(entry, 2)}:{round(tp_final, 2)}"
    trade_journal.register_signal(
        bot="bot2", symbol=symbol, direction="long",
        entry_price=entry, sl=sl, tp_final=tp_final,
        r_unit=r_unit,
        extra={"pending_entry": True, "ob_level": entry},
        dedup_key=dedup_key,
    )


def main():
    state = load_state()
    m5_by_symbol = {}
    for symbol in SYMBOLS:
        try:
            check_symbol(symbol, state)
        except Exception as e:
            print(f"[{symbol}] Erreur: {e}")
        # on recupere les M5 recentes pour le suivi des trades ouverts
        try:
            m5_by_symbol[symbol] = fetch_klines(symbol, "Min5", LOOKBACK_M5)
        except Exception as e:
            print(f"[{symbol}] Erreur fetch M5 pour suivi: {e}")
        time.sleep(0.3)
    save_state(state)

    # --- Journal : suivre les trades bot2 (entree en attente + paliers) ---
    try:
        trade_journal.update_open_trades("bot2", m5_by_symbol, follow_logic.follow_bot2_pending)
    except Exception as e:
        print(f"Erreur suivi journal bot2 : {e}")


if __name__ == "__main__":
    main()
