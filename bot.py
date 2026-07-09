import pandas as pd
import numpy as np
import json, time, os, urllib.request, urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"

# =========================================================
# CONFIG
# =========================================================
ACTIFS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
JOURS_H4 = 90     # historique nécessaire pour resynchroniser le détecteur H4
JOURS_H1 = 45
JOURS_M5 = 8

MAX_ATTENTE_MINUTES = 1440       # délai max entre point H1 et confirmation M5
MAX_AGE_SIGNAL_MINUTES = 240     # on ignore les setups M5 trop vieux (> 4h)
RR_MIN = 2.0

# Plage horaire active PAR ACTIF (heure de Paris).
# Basé sur l'analyse du backtest : BTC/ETH/BNB montrent un meilleur WR sur 9h-22h,
# SOL ne montre pas de différence significative -> pas de restriction pour SOL.
# (échantillon limité, à réévaluer si le comportement live diverge)
PLAGES_ACTIVES = {
    "BTCUSDT": (9, 22),
    "ETHUSDT": (9, 22),
    "BNBUSDT": (9, 22),
    "SOLUSDT": (0, 24),   # pas de restriction
}

STATE_FILE = "state.json"
STATE_MAX_AGE_JOURS = 5           # purge des vieilles clés d'état

LOG_VERBOSE = True                # passe à False pour des logs plus courts

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def log_rejet(symbol, sens, ts_conf, raison, **details):
    if not LOG_VERBOSE:
        return
    date_str = datetime.fromtimestamp(ts_conf / 1000.0, tz=timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d %H:%M')
    extra = " ".join(f"{k}={v}" for k, v in details.items())
    print(f"   ✗ REJET {symbol} [{sens}] signal M5 {date_str} -> {raison} {extra}")


# =========================================================
# STRUCTURE DE DONNÉES CANDLE + DÉTECTEUR (identique à ton code)
# =========================================================
class Candle:
    def __init__(self, ts, o, h, l, c):
        self.ts, self.open, self.high, self.low, self.close = ts, o, h, l, c
    @property
    def is_bullish(self):
        return self.close > self.open
    @property
    def is_bearish(self):
        return self.close < self.open


class Det:
    def __init__(self):
        self.historique = []
        self.sens = None
        self.point_cle, self.point_cle_idx = None, None
        self.niveau_continuation, self.niveau_continuation_idx = None, None
        self.ext, self.ext_idx = None, None
        self.stage = 0
        self.stage_niveau_casse = None
        self.stage_extreme, self.stage_extreme_idx = None, None
        self.stage_retest = False
        self.cont_stage = 0
        self.tentative_haut, self.tentative_bas = None, None
        self.niveau_casse = None
        self.retest_confirme = False

    def update(self, candles):
        self.historique = []
        self.sens = None
        self.stage = 0
        self.cont_stage = 0
        self.tentative_haut = None
        self.tentative_bas = None
        for i, c in enumerate(candles):
            self._process(i, c)

    def _process(self, i, c):
        if self.sens is None:
            if c.is_bearish:
                self.sens = 'haussier'
                self.point_cle, self.point_cle_idx = c.low, i
                self.niveau_continuation, self.niveau_continuation_idx = c.high, i
                self.ext, self.ext_idx = c.low, i
            elif c.is_bullish:
                self.sens = 'baissier'
                self.point_cle, self.point_cle_idx = c.high, i
                self.niveau_continuation, self.niveau_continuation_idx = c.low, i
                self.ext, self.ext_idx = c.high, i
            return
        if self.sens == 'haussier':
            self._ph(i, c)
        else:
            self._pb(i, c)

    def _ph(self, i, c):
        if c.low < self.ext:
            self.ext, self.ext_idx = c.low, i
        if self.cont_stage == 0:
            if c.high > self.niveau_continuation:
                if self.tentative_haut is None or c.high > self.tentative_haut:
                    self.tentative_haut = c.high
                if c.is_bearish:
                    self.cont_stage = 1
                    self.niveau_casse = self.niveau_continuation
                    self.retest_confirme = False
        elif self.cont_stage == 1:
            if c.is_bearish and c.close <= self.niveau_casse:
                self.retest_confirme = True
            if c.high > self.tentative_haut:
                e = 'EPA' if self.retest_confirme else 'IPA'
                self.historique.append({'sens': 'haussier', 'A': self.ext, 'a_idx': self.ext_idx, 'B': c.high, 'b_idx': i, 'etat': e})
                self.point_cle, self.point_cle_idx = self.ext, self.ext_idx
                self.niveau_continuation, self.niveau_continuation_idx = c.high, i
                self.ext, self.ext_idx = c.low, i
                self.cont_stage = 0
                self.tentative_haut = None
                return
        if self.stage == 0:
            if c.low < self.point_cle:
                if self.stage_extreme is None or c.low < self.stage_extreme:
                    self.stage_extreme, self.stage_extreme_idx = c.low, i
                if c.is_bullish:
                    self.stage = 1
                    self.stage_niveau_casse = self.point_cle
                    self.stage_retest = False
        elif self.stage == 1:
            if c.is_bullish and c.close >= self.stage_niveau_casse:
                self.stage_retest = True
            if c.high > self.niveau_continuation:
                self.stage = 0
                self.stage_extreme = None
            elif c.low < self.stage_extreme:
                etat_ct = 'EPA' if self.stage_retest else 'IPA'
                self.historique.append({'sens': 'haussier', 'A': self.point_cle, 'a_idx': self.point_cle_idx, 'B': c.low, 'b_idx': i, 'etat': 'changement_tendance', 'sous_etat': etat_ct})
                self.sens = 'baissier'
                self.point_cle, self.point_cle_idx = self.niveau_continuation, self.niveau_continuation_idx
                self.niveau_continuation, self.niveau_continuation_idx = c.low, i
                self.ext, self.ext_idx = c.high, i
                self.stage = 0
                self.stage_extreme = None
                self.cont_stage = 0
                self.tentative_haut = None
                self.tentative_bas = None

    def _pb(self, i, c):
        if c.high > self.ext:
            self.ext, self.ext_idx = c.high, i
        if self.cont_stage == 0:
            if c.low < self.niveau_continuation:
                if self.tentative_bas is None or c.low < self.tentative_bas:
                    self.tentative_bas = c.low
                if c.is_bullish:
                    self.cont_stage = 1
                    self.niveau_casse = self.niveau_continuation
                    self.retest_confirme = False
        elif self.cont_stage == 1:
            if c.is_bullish and c.close >= self.niveau_casse:
                self.retest_confirme = True
            if c.low < self.tentative_bas:
                e = 'EPA' if self.retest_confirme else 'IPA'
                self.historique.append({'sens': 'baissier', 'A': self.ext, 'a_idx': self.ext_idx, 'B': c.low, 'b_idx': i, 'etat': e})
                self.point_cle, self.point_cle_idx = self.ext, self.ext_idx
                self.niveau_continuation, self.niveau_continuation_idx = c.low, i
                self.ext, self.ext_idx = c.high, i
                self.cont_stage = 0
                self.tentative_bas = None
                return
        if self.stage == 0:
            if c.high > self.point_cle:
                if self.stage_extreme is None or c.high > self.stage_extreme:
                    self.stage_extreme, self.stage_extreme_idx = c.high, i
                if c.is_bearish:
                    self.stage = 1
                    self.stage_niveau_casse = self.point_cle
                    self.stage_retest = False
        elif self.stage == 1:
            if c.is_bearish and c.close <= self.stage_niveau_casse:
                self.stage_retest = True
            if c.low < self.niveau_continuation:
                self.stage = 0
                self.stage_extreme = None
            elif c.high > self.stage_extreme:
                etat_ct = 'EPA' if self.stage_retest else 'IPA'
                self.historique.append({'sens': 'baissier', 'A': self.point_cle, 'a_idx': self.point_cle_idx, 'B': c.high, 'b_idx': i, 'etat': 'changement_tendance', 'sous_etat': etat_ct})
                self.sens = 'haussier'
                self.point_cle, self.point_cle_idx = self.niveau_continuation, self.niveau_continuation_idx
                self.niveau_continuation, self.niveau_continuation_idx = c.high, i
                self.ext, self.ext_idx = c.low, i
                self.stage = 0
                self.stage_extreme = None
                self.cont_stage = 0
                self.tentative_haut = None
                self.tentative_bas = None


# =========================================================
# FETCH BINANCE
# =========================================================
def fetch(symbol, interval, days):
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - days * 86400000
    all_c = []
    cur = start_ts
    attempts = 0
    while cur < end_ts:
        url = f"{BINANCE_URL}?symbol={symbol}&interval={interval}&startTime={cur}&endTime={end_ts}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            if not data:
                break
            for k in data:
                all_c.append(Candle(k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4])))
            last = data[-1][0]
            if last <= cur:
                break
            cur = last + 1
            attempts = 0
            time.sleep(0.1)
        except Exception:
            attempts += 1
            if attempts > 3:
                break
            time.sleep(1.5)
            continue
    return all_c


def construire_timeline_sens(candles, historique):
    timeline = []
    sens_courant = None
    for p in historique:
        if p['etat'] == 'changement_tendance':
            sens_courant = 'baissier' if p['sens'] == 'haussier' else 'haussier'
        else:
            sens_courant = p['sens']
        timeline.append((candles[p['b_idx']].ts, sens_courant))
    return timeline


def sens_a_la_date(timeline, ts_cible):
    resultat = "neutre"
    for ts, sens in timeline:
        if ts <= ts_cible:
            resultat = sens
        else:
            break
    return resultat


# =========================================================
# ETAT (anti-doublons entre les runs)
# =========================================================
def charger_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def sauver_state(state):
    now_ms = int(time.time() * 1000)
    max_age_ms = STATE_MAX_AGE_JOURS * 86400000
    state_purge = {k: v for k, v in state.items() if now_ms - v <= max_age_ms}
    with open(STATE_FILE, "w") as f:
        json.dump(state_purge, f)


# =========================================================
# TELEGRAM
# =========================================================
def envoyer_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants, message non envoyé :")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:
        print(f"Erreur envoi Telegram: {e}")


# =========================================================
# DETECTION DES SIGNAUX LIVE POUR UN ACTIF
# =========================================================
def detecter_signaux(symbol, candles_h4, candles_h1, candles_m5, state):
    if not candles_h4 or not candles_h1 or not candles_m5:
        return []

    d_h4 = Det(); d_h4.update(candles_h4)
    d_h1 = Det(); d_h1.update(candles_h1)
    d_m5 = Det(); d_m5.update(candles_m5)
    tl_h4 = construire_timeline_sens(candles_h4, d_h4.historique)

    now_ms = int(time.time() * 1000)
    nouveaux_signaux = []

    for p_h1 in d_h1.historique:
        if p_h1['etat'] != 'EPA':
            continue
        ts_val_h1 = candles_h1[p_h1['b_idx']].ts

        for p_conf in d_m5.historique:
            if p_conf['etat'] != 'EPA' or p_conf['sens'] != p_h1['sens']:
                continue
            ts_val_conf = candles_m5[p_conf['b_idx']].ts

            # signal M5 trop vieux -> on ignore (plus d'intérêt à alerter)
            if (now_ms - ts_val_conf) / 60000.0 > MAX_AGE_SIGNAL_MINUTES:
                log_rejet(symbol, p_h1['sens'], ts_val_conf, "trop vieux (> MAX_AGE_SIGNAL_MINUTES)")
                continue
            if not (0 <= (ts_val_conf - ts_val_h1) / 60000.0 <= MAX_ATTENTE_MINUTES):
                log_rejet(symbol, p_h1['sens'], ts_val_conf, "délai H1->M5 hors bornes", minutes=round((ts_val_conf - ts_val_h1) / 60000.0, 1))
                continue
            if sens_a_la_date(tl_h4, ts_val_conf) != p_h1['sens']:
                log_rejet(symbol, p_h1['sens'], ts_val_conf, "biais H4 différent", biais_h4=sens_a_la_date(tl_h4, ts_val_conf))
                continue

            h_paris = datetime.fromtimestamp(ts_val_conf / 1000.0, tz=timezone.utc).astimezone(PARIS).hour
            if 13 <= h_paris <= 17:
                type_entree = "Market_Impuls"
                prix_entree = candles_m5[p_conf['b_idx']].close
            else:
                type_entree = "Limit_Premium"
                prix_entree = (p_conf['A'] + p_conf['B']) / 2.0

            sl = p_conf['A']
            tp = p_h1['B']
            if None in [prix_entree, sl, tp]:
                log_rejet(symbol, p_h1['sens'], ts_val_conf, "prix_entree/sl/tp manquant")
                continue
            dist_sl = abs(prix_entree - sl)
            if dist_sl < (prix_entree * 0.0005):
                log_rejet(symbol, p_h1['sens'], ts_val_conf, "SL trop proche du prix d'entrée", dist_sl=round(dist_sl, 6))
                continue
            rr_theorique = min(abs(tp - prix_entree) / dist_sl, 30.0)
            if rr_theorique < RR_MIN:
                log_rejet(symbol, p_h1['sens'], ts_val_conf, "RR insuffisant", rr=round(rr_theorique, 2))
                continue

            # le setup est-il encore valide (SL/TP pas déjà touchés depuis la formation) ?
            statut = None
            ajustement = prix_entree * 0.0001
            for m in range(p_conf['b_idx'] + 1, len(candles_m5)):
                c_check = candles_m5[m]
                if p_h1['sens'] == 'haussier':
                    if type_entree == "Limit_Premium" and c_check.low > prix_entree and statut is None:
                        if c_check.high >= (tp - ajustement):
                            statut = "EXPIRE"
                            break
                        continue
                    if c_check.low <= (sl - ajustement):
                        statut = "EXPIRE"
                        break
                    if c_check.high >= (tp - ajustement):
                        statut = "EXPIRE"
                        break
                else:
                    if type_entree == "Limit_Premium" and c_check.high < prix_entree and statut is None:
                        if c_check.low <= (tp + ajustement):
                            statut = "EXPIRE"
                            break
                        continue
                    if c_check.high >= (sl + ajustement):
                        statut = "EXPIRE"
                        break
                    if c_check.low <= (tp + ajustement):
                        statut = "EXPIRE"
                        break
            if statut == "EXPIRE":
                log_rejet(symbol, p_h1['sens'], ts_val_conf, "SL ou TP déjà touché depuis la formation (signal caduc)")
                continue  # SL ou TP déjà touché -> signal caduc, on n'alerte pas

            cle = f"{symbol}_{p_h1['b_idx']}_{p_conf['b_idx']}"
            if cle in state:
                log_rejet(symbol, p_h1['sens'], ts_val_conf, "déjà alerté (présent dans state.json)")
                continue  # déjà alerté

            state[cle] = now_ms
            nouveaux_signaux.append({
                "symbol": symbol,
                "sens": p_h1['sens'],
                "type_entree": type_entree,
                "prix_entree": prix_entree,
                "sl": sl,
                "tp": tp,
                "rr": round(rr_theorique, 2),
                "ts_h1": ts_val_h1,
                "ts_m5": ts_val_conf,
            })

    return nouveaux_signaux


def formater_message(sig):
    emoji = "🟢" if sig["sens"] == "haussier" else "🔴"
    direction = "LONG" if sig["sens"] == "haussier" else "SHORT"
    date_str = datetime.fromtimestamp(sig["ts_m5"] / 1000.0, tz=timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d %H:%M')
    return (
        f"{emoji} <b>{sig['symbol']} - {direction}</b>\n"
        f"Type: {sig['type_entree']}\n"
        f"Entrée: {sig['prix_entree']:.4f}\n"
        f"SL: {sig['sl']:.4f}\n"
        f"TP: {sig['tp']:.4f}\n"
        f"R:R ≈ 1:{sig['rr']}\n"
        f"Détecté: {date_str} (Paris)"
    )


# =========================================================
# MAIN
# =========================================================
def dans_plage_active(symbol):
    debut, fin = PLAGES_ACTIVES.get(symbol, (0, 24))
    h_paris = datetime.now(timezone.utc).astimezone(PARIS).hour
    if debut <= fin:
        return debut <= h_paris < fin
    else:
        # plage qui traverse minuit
        return h_paris >= debut or h_paris < fin


def main():
    state = charger_state()
    tous_les_signaux = []
    h_paris = datetime.now(timezone.utc).astimezone(PARIS).hour

    for actif in ACTIFS:
        if not dans_plage_active(actif):
            debut, fin = PLAGES_ACTIVES.get(actif, (0, 24))
            print(f"⏸️ {actif} hors plage active ({debut}h-{fin}h Paris, il est {h_paris}h) — skip.")
            continue
        print(f"[🔎] Analyse {actif}...")
        c_h4 = fetch(actif, "4h", JOURS_H4)
        c_h1 = fetch(actif, "1h", JOURS_H1)
        c_m5 = fetch(actif, "5m", JOURS_M5)
        signaux = detecter_signaux(actif, c_h4, c_h1, c_m5, state)
        tous_les_signaux.extend(signaux)
        time.sleep(0.5)  # marge de sécurité anti rate-limit entre chaque actif

    if tous_les_signaux:
        for sig in tous_les_signaux:
            msg = formater_message(sig)
            print(msg)
            envoyer_telegram(msg)
    else:
        print("Aucun nouveau signal ce run.")

    sauver_state(state)


if __name__ == "__main__":
    main()
