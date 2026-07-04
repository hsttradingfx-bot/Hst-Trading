"""
BOT LIVE - MEXC Futures - Strategie H4/H1/M5 (SOL_USDT)
===========================================================
100% Python standard (urllib, hmac, hashlib) - aucune installation requise.

!!! IMPORTANT - A LIRE AVANT TOUTE CHOSE !!!
- MEXC N'A PAS D'ENVIRONNEMENT DE TEST (sandbox). L'API se connecte directement
  au marche reel. DRY_RUN = True par defaut : le bot calcule tout et affiche
  ce qu'il AURAIT fait, mais n'envoie AUCUN ordre reel.
- Les valeurs exactes des champs `side` et `type` de l'endpoint order/submit
  ne sont PAS garanties a 100% dans ce script (documentation contradictoire
  selon les sources). NE PASSE PAS DRY_RUN A False avant d'avoir verifie ces
  valeurs toi-meme (voir section VERIFICATION ci-dessous).
- Il faut avoir complete le KYC sur MEXC pour activer les permissions de
  trading futures sur ta cle API.

VERIFICATION AVANT DE DESACTIVER DRY_RUN :
1. Cree une cle API sur MEXC (Futures -> API Management), avec permission
   de trading, et idealement une whitelist d'IP (celle de ton VPS).
2. Renseigne API_KEY et API_SECRET ci-dessous.
3. Lance ce script en laissant DRY_RUN = True pendant plusieurs jours, et
   compare mentalement les signaux avec ce que tu observes toi-meme sur le
   graphique MEXC.
4. Avant de passer en reel, place UN ordre minuscule (taille minimale) a la
   main via ce script en mode non-DRY_RUN pour verifier que side/type/vol
   correspondent bien a ce que tu attends (achat = achat, pas vente), en
   verifiant immediatement sur l'app MEXC que la position ouverte est correcte.
5. Documentation officielle a consulter : https://www.mexc.com/api-docs/futures/account-and-trading-endpoints
"""

import hmac
import hashlib
import json
import time
import urllib.request
import urllib.error
import urllib.parse
import os
from datetime import datetime, timedelta, timezone

# ============================================================
# TELEGRAM (notifications)
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TON_TOKEN_ICI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TON_CHAT_ID_ICI")


def send_telegram(message):
    if TELEGRAM_BOT_TOKEN == "TON_TOKEN_ICI":
        print(f"[TELEGRAM non configure] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"Erreur envoi Telegram : {e}")

# ============================================================
# CONFIGURATION
# ============================================================
API_KEY = "TA_CLE_API_ICI"
API_SECRET = "TON_SECRET_API_ICI"
BASE_URL = "https://contract.mexc.com"

SYMBOL = "SOL_USDT"
DRY_RUN = True   # NE PASSE PAS A False SANS AVOIR LU LES AVERTISSEMENTS CI-DESSUS

INITIAL_CAPITAL_OVERRIDE = None  # None = utilise le solde reel du compte ; sinon force une valeur (utile en DRY_RUN)
RISK_PCT = 1.0
LEVERAGE = 3
POLL_INTERVAL_SECONDS = 60  # frequence de verification (1 minute, aligne sur le M5 en pratique)

SWING_LEN_H4 = 2
SWING_LEN_H1 = 2
SWING_LEN_EXEC = 3
FVG_FILL_LEVEL = 0.5
ATR_LEN = 14
SL_BUFFER_ATR_MULT = 0.1
BE_TRIGGER_R = 2.0
PARTIAL_TP_FRACTION = 0.70
PARTIAL_TP_R = 5.0


# ============================================================
# SIGNATURE ET REQUETES (API Futures MEXC)
# ============================================================
def _sign(params_string, timestamp):
    to_sign = f"{API_KEY}{timestamp}{params_string}"
    return hmac.new(API_SECRET.encode(), to_sign.encode(), hashlib.sha256).hexdigest()


def _private_get(path, params=None):
    params = params or {}
    timestamp = str(int(time.time() * 1000))
    sorted_items = sorted(params.items())
    params_string = "&".join(f"{k}={v}" for k, v in sorted_items)
    signature = _sign(params_string, timestamp)
    url = f"{BASE_URL}{path}"
    if params_string:
        url += f"?{params_string}"
    headers = {
        "ApiKey": API_KEY,
        "Request-Time": timestamp,
        "Signature": signature,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _private_post(path, body):
    timestamp = str(int(time.time() * 1000))
    body_string = json.dumps(body, separators=(",", ":"))
    signature = _sign(body_string, timestamp)
    url = f"{BASE_URL}{path}"
    headers = {
        "ApiKey": API_KEY,
        "Request-Time": timestamp,
        "Signature": signature,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=body_string.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_account_balance(currency="USDT"):
    """Solde du compte futures. Endpoint prive - necessite une cle API valide."""
    try:
        result = _private_get("/api/v1/private/account/asset/" + currency)
        return float(result["data"]["availableBalance"])
    except Exception as e:
        print(f"Impossible de recuperer le solde reel ({e}), utilisation de INITIAL_CAPITAL_OVERRIDE.")
        return None


def get_contract_detail(symbol):
    """Endpoint public - infos du contrat (taille min, precision, etc.)."""
    url = f"{BASE_URL}/api/v1/contract/detail?symbol={symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())["data"]


def place_order(symbol, side, order_type, vol, price=None, leverage=LEVERAGE, open_type=1):
    """
    ATTENTION : side et order_type ne sont pas garantis a 100%.
    Convention utilisee ici (a verifier avant tout ordre reel) :
      side : 1 = ouvrir long, 2 = fermer short, 3 = ouvrir short, 4 = fermer long
      order_type : 1 = limite, 5 = marche
      open_type : 1 = isole, 2 = croise
    """
    body = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "vol": vol,
        "openType": open_type,
        "leverage": leverage,
    }
    if price is not None:
        body["price"] = price

    if DRY_RUN:
        print(f"[DRY_RUN] Ordre qui AURAIT ete envoye : {body}")
        return {"dry_run": True, "body": body}

    print(f"[REEL] Envoi de l'ordre : {body}")
    return _private_post("/api/v1/private/order/submit", body)


# ============================================================
# RECUPERATION DES DONNEES DE MARCHE (public, reutilise du backtest)
# ============================================================
def fetch_klines(symbol, interval, limit_days=10):
    interval_sec = {"Min1": 60, "Min5": 300, "Min60": 3600, "Hour4": 14400}[interval]
    end_ts = int(time.time())
    start_ts = end_ts - limit_days * 86400
    url = f"{BASE_URL}/api/v1/contract/kline/{symbol}?interval={interval}&start={start_ts}&end={end_ts}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode())
    d = raw["data"]
    candles = []
    for i in range(len(d["time"])):
        candles.append({
            "ts": d["time"][i], "open": float(d["open"][i]), "high": float(d["high"][i]),
            "low": float(d["low"][i]), "close": float(d["close"][i]),
        })
    return candles


# ============================================================
# INDICATEURS ET LOGIQUE (identiques au backtest valide)
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
# BOUCLE PRINCIPALE (live)
# ============================================================
def get_capital():
    if INITIAL_CAPITAL_OVERRIDE is not None:
        return INITIAL_CAPITAL_OVERRIDE
    bal = get_account_balance()
    return bal if bal is not None else 20.0


def run_signal_check():
    """Un seul passage : verifie s'il y a un signal MAINTENANT, notifie sur Telegram si oui.
    Concu pour etre appele par une tache planifiee (GitHub Actions, cron, etc.),
    pas pour tourner en boucle infinie soi-meme."""
    print(f"[{datetime.now()}] Verification en cours sur {SYMBOL}...")

    h4 = fetch_klines(SYMBOL, "Hour4", limit_days=250)
    h1 = fetch_klines(SYMBOL, "Min60", limit_days=80)
    m5 = fetch_klines(SYMBOL, "Min5", limit_days=6)

    zbt, zbb, zrt, zrb = compute_h4_bias_zones(h4, SWING_LEN_H4)
    fvg_bull_epa, fvg_bear_epa, fvg_bull_bounds, fvg_bear_bounds = compute_h1_fvg_epa(h1)
    ob_bull_signal, ob_bear_signal = compute_ob_signals(m5, SWING_LEN_EXEC)
    atr_m5 = compute_atr(m5)

    i = len(m5) - 1
    import bisect
    h4_ts = [c["ts"] for c in h4]
    h1_ts = [c["ts"] for c in h1]
    idx_h4 = bisect.bisect_right(h4_ts, m5[i]["ts"]) - 1
    idx_h1 = bisect.bisect_right(h1_ts, m5[i]["ts"]) - 1

    zone_top_bull = zbt[idx_h4] if idx_h4 >= 0 else None
    zone_bottom_bull = zbb[idx_h4] if idx_h4 >= 0 else None
    epa_bull_bounds = fvg_bull_bounds[idx_h1] if idx_h1 >= 0 else None
    atr_val = atr_m5[i]

    fvg_in_zone = (zone_top_bull is not None and epa_bull_bounds is not None and
                   epa_bull_bounds[0] < zone_top_bull and epa_bull_bounds[1] > zone_bottom_bull)

    if fvg_in_zone and ob_bull_signal[i] is not None and atr_val is not None:
        entry_price = ob_bull_signal[i]
        sl = entry_price - atr_val * SL_BUFFER_ATR_MULT
        tp = zone_top_bull
        if tp > entry_price and sl < entry_price:
            capital = get_capital()
            risk_amount = capital * (RISK_PCT / 100)
            sl_dist = entry_price - sl
            qty = round(risk_amount / sl_dist, 4)
            msg = (f"SIGNAL ACHAT {SYMBOL}\n"
                   f"Entree: {entry_price}\nSL: {sl}\nTP: {tp}\nQte suggeree: {qty}")
            print(msg)
            send_telegram(msg)
            return

    print(f"[{datetime.now()}] Aucun signal pour l'instant.")


if __name__ == "__main__":
    run_signal_check()
