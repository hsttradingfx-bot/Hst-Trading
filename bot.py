import pandas as pd
import numpy as np
import json, time, os, hmac, hashlib, urllib.request, urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
BYBIT_URL = "https://api.bybit.eu/v5/market/kline"

# Bybit utilise des codes d'intervalle différents de Binance
BYBIT_INTERVALS = {
    "4h": "240",
    "1h": "60",
    "5m": "5",
    "1m": "1",
}

# =========================================================
# CONFIG
# =========================================================
ACTIFS = ["BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC"]
JOURS_H4 = 90     # historique nécessaire pour resynchroniser le détecteur H4
JOURS_H1 = 45
JOURS_M5 = 8

MAX_ATTENTE_MINUTES = 1440       # délai max entre point H1 et confirmation M5
MAX_AGE_SIGNAL_MINUTES = 240     # on ignore les setups M5 trop vieux (> 4h)
RR_MIN = 2.0

# Plage(s) horaire(s) active(s) PAR ACTIF (heure de Paris).
# Chaque actif a une LISTE de plages (debut, fin) - une ou plusieurs.
PLAGES_ACTIVES = {
    "BTCUSDC": [(9, 12), (14, 16)],
    "ETHUSDC": [(9, 12), (14, 16)],
    "BNBUSDC": [(14, 16)],  # Exclu le matin, actif l'après-midi
    "SOLUSDC": [(14, 16)]   # Exclu le matin, actif l'après-midi
}


STATE_FILE = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "state.json")
STATE_MAX_AGE_JOURS = 5           # purge des vieilles clés d'état

LOG_VERBOSE = True                # passe à False pour des logs plus courts

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# =========================================================
# EXÉCUTION VIA GAINIUM (webhooks) — plus d'accès direct à l'API Bybit
# =========================================================
GAINIUM_WEBHOOK_URL = "https://api.gainium.io/trade_signal"
EXECUTION_REELLE = True

# Un bot Gainium = une direction fixe (Long ou Short) pour un actif donné.
# On a donc 2 bots par actif : haussier -> bot Long, baissier -> bot Short.
BOT_IDS = {
    "BTCUSDC": {"haussier": "6a500c50ebbae511728a486b", "baissier": "6a5011baebbae5117291ec93"},
    "ETHUSDC": {"haussier": "6a500ea2ebbae511728d9589", "baissier": "6a501190ebbae5117291b4a3"},
    "SOLUSDC": {"haussier": "6a500fa2ebbae511728ef791", "baissier": "6a501512ebbae5117296aa9b"},
    "BNBUSDC": {"haussier": "6a5010f4ebbae5117290da6f", "baissier": "6a5015e6ebbae5117297d951"},
}

RISQUE_PAR_PLAGE = {
    (9, 12): 0.02,
    (14, 16): 0.05,
}
RISQUE_PAR_DEFAUT = 0.02


def log_rejet(symbol, sens, ts_conf, raison, **details):
    if not LOG_VERBOSE:
        return
    date_str = datetime.fromtimestamp(ts_conf / 1000.0, tz=timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d %H:%M')
    extra = " ".join(f"{k}={v}" for k, v in details.items())
    print(f"   ✗ REJET {symbol} [{sens}] signal M5 {date_str} -> {raison} {extra}")


# =========================================================
# STRUCTURE DE DONNÉES CANDLE + DÉTECTEUR
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
# FETCH BYBIT
# =========================================================
def fetch(symbol, interval, days):
    """
    Récupère des bougies depuis Bybit EU (category=spot, cohérent avec l'exécution des ordres).
    `interval` reste au format Binance ("4h", "1h", "5m", "1m") pour ne pas
    changer le reste du code — la conversion vers le code Bybit se fait ici.
    """
    interval_code = BYBIT_INTERVALS[interval]
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - days * 86400000
    lignes = {}  # dédoublonnage par timestamp
    cur_end = end_ts
    attempts = 0

    while cur_end > start_ts:
        url = f"{BYBIT_URL}?category=spot&symbol={symbol}&interval={interval_code}&end={cur_end}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            if data.get("retCode") != 0:
                raise Exception(data.get("retMsg", "erreur Bybit"))
            rows = data.get("result", {}).get("list", [])
            if not rows:
                break
            for row in rows:
                ts = int(row[0])
                lignes[ts] = row
            oldest_ts = min(int(row[0]) for row in rows)
            if oldest_ts <= start_ts or oldest_ts >= cur_end:
                break
            cur_end = oldest_ts - 1
            attempts = 0
            time.sleep(0.15)
        except Exception:
            attempts += 1
            if attempts > 3:
                break
            time.sleep(1.5)
            continue

    ts_valides = sorted(ts for ts in lignes if start_ts <= ts <= end_ts)
    candles = []
    for ts in ts_valides:
        row = lignes[ts]
        candles.append(Candle(ts, float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return candles


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
    state_purge = {}
    for k, v in state.items():
        if k == "_positions_ouvertes":
            state_purge[k] = v  # structure différente, pas de purge par timestamp
        elif now_ms - v <= max_age_ms:
            state_purge[k] = v
    with open(STATE_FILE, "w") as f:
        json.dump(state_purge, f)


# =========================================================
# WEBHOOKS GAINIUM (exécution des trades)
# =========================================================
def envoyer_webhook_gainium(action, bot_uuid):
    payload = json.dumps({"action": action, "uuid": bot_uuid}).encode()
    req = urllib.request.Request(GAINIUM_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            reponse = r.read().decode()
            return {"succes": True, "reponse": reponse}
    except Exception as e:
        return {"succes": False, "erreur": str(e)}


def ouvrir_position_gainium(symbol, sens):
    bot_uuid = BOT_IDS.get(symbol, {}).get(sens)
    if not bot_uuid:
        return {"succes": False, "erreur": f"aucun bot Gainium configuré pour {symbol}/{sens}"}
    return envoyer_webhook_gainium("startDeal", bot_uuid)


def fermer_position_gainium(symbol, sens):
    bot_uuid = BOT_IDS.get(symbol, {}).get(sens)
    if not bot_uuid:
        return {"succes": False, "erreur": f"aucun bot Gainium configuré pour {symbol}/{sens}"}
    return envoyer_webhook_gainium("closeDeal", bot_uuid)


def surveiller_positions_ouvertes(state, candles_par_symbole):
    """
    Vérifie les positions ouvertes (suivies dans state) contre les dernières bougies
    récupérées, et déclenche la clôture via webhook si le SL ou le TP est touché.
    Pas d'accès direct à Bybit ici : on se base uniquement sur les prix publics déjà
    récupérés pour la détection.
    """
    positions = state.get("_positions_ouvertes", {})
    a_supprimer = []

    for cle, pos in positions.items():
        symbol = pos["symbol"]
        candles_m5 = candles_par_symbole.get(symbol)
        if not candles_m5:
            continue

        touche = None
        for c in candles_m5:
            if c.ts <= pos["ts_ouverture"]:
                continue
            if pos["sens"] == "haussier":
                if c.low <= pos["sl"]:
                    touche = "SL"
                    break
                if c.high >= pos["tp"]:
                    touche = "TP"
                    break
            else:
                if c.high >= pos["sl"]:
                    touche = "SL"
                    break
                if c.low <= pos["tp"]:
                    touche = "TP"
                    break

        if touche:
            resultat = fermer_position_gainium(symbol, pos["sens"])
            emoji = "🔴" if touche == "SL" else "🟢"
            if resultat["succes"]:
                envoyer_telegram(f"{emoji} {symbol} [{pos['sens']}] : {touche} touché, clôture envoyée à Gainium.")
            else:
                envoyer_telegram(f"⚠️ {symbol} [{pos['sens']}] : {touche} touché mais échec de clôture Gainium ({resultat['erreur']}) — vérifie manuellement.")
            a_supprimer.append(cle)

    for cle in a_supprimer:
        del positions[cle]
    state["_positions_ouvertes"] = positions


def obtenir_risque_pct(symbol, h_paris):
    for debut, fin in PLAGES_ACTIVES.get(symbol, []):
        if debut <= h_paris < fin:
            return RISQUE_PAR_PLAGE.get((debut, fin), RISQUE_PAR_DEFAUT)
    return None


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
                continue

            cle = f"{symbol}_{p_h1['b_idx']}_{p_conf['b_idx']}"
            if cle in state:
                log_rejet(symbol, p_h1['sens'], ts_val_conf, "déjà alerté (présent dans state.json)")
                continue

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


def formater_message(sig, execution=None):
    emoji = "🟢" if sig["sens"] == "haussier" else "🔴"
    direction = "LONG" if sig["sens"] == "haussier" else "SHORT"
    date_str = datetime.fromtimestamp(sig["ts_m5"] / 1000.0, tz=timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d %H:%M')
    base = (
        f"{emoji} <b>{sig['symbol']} - {direction}</b>\n"
        f"Type: {sig['type_entree']}\n"
        f"Entrée: {sig['prix_entree']:.4f}\n"
        f"SL: {sig['sl']:.4f}\n"
        f"TP: {sig['tp']:.4f}\n"
        f"R:R ≈ 1:{sig['rr']}\n"
        f"Détecté: {date_str} (Paris)"
    )
    if execution is None:
        return base
    if execution.get("ordre_place"):
        risque_txt = f" (tranche à {execution['risque_pct']*100:.0f}% de risque visé)" if execution.get("risque_pct") else ""
        base += f"\n\n✅ <b>DEAL OUVERT SUR GAINIUM</b>{risque_txt}"
    else:
        base += f"\n\n⛔ <b>NON EXÉCUTÉ</b> — {execution.get('raison', 'raison inconnue')}"
    return base


# =========================================================
# MAIN
# =========================================================
def dans_plage_active(symbol):
    plages = PLAGES_ACTIVES.get(symbol, [(0, 24)])
    h_paris = datetime.now(timezone.utc).astimezone(PARIS).hour
    for debut, fin in plages:
        if debut <= fin:
            if debut <= h_paris < fin:
                return True
        else:
            if h_paris >= debut or h_paris < fin:
                return True
    return False


def main():
    state = charger_state()
    tous_les_signaux = []
    candles_par_symbole = {}  # pour surveiller les positions ouvertes après coup
    h_paris = datetime.now(timezone.utc).astimezone(PARIS).hour

    for actif in ACTIFS:
        if not dans_plage_active(actif):
            plages = PLAGES_ACTIVES.get(actif, [(0, 24)])
            print(f"⏸️ {actif} hors plage active ({plages} Paris, il est {h_paris}h) — skip.")
            continue
        print(f"[🔎] Analyse {actif}...")
        c_h4 = fetch(actif, "4h", JOURS_H4)
        c_h1 = fetch(actif, "1h", JOURS_H1)
        c_m5 = fetch(actif, "5m", JOURS_M5)
        candles_par_symbole[actif] = c_m5
        signaux = detecter_signaux(actif, c_h4, c_h1, c_m5, state)
        tous_les_signaux.extend(signaux)
        time.sleep(0.5)

    if EXECUTION_REELLE:
        surveiller_positions_ouvertes(state, candles_par_symbole)

    if tous_les_signaux:
        for sig in tous_les_signaux:
            execution = None
            if EXECUTION_REELLE:
                risque_pct = obtenir_risque_pct(sig["symbol"], h_paris)
                resultat = ouvrir_position_gainium(sig["symbol"], sig["sens"])
                if resultat["succes"]:
                    execution = {"ordre_place": True, "risque_pct": risque_pct}
                    positions = state.setdefault("_positions_ouvertes", {})
                    positions[f"{sig['symbol']}_{sig['sens']}_{sig['ts_m5']}"] = {
                        "symbol": sig["symbol"],
                        "sens": sig["sens"],
                        "sl": sig["sl"],
                        "tp": sig["tp"],
                        "ts_ouverture": sig["ts_m5"],
                    }
                else:
                    execution = {"ordre_place": False, "raison": f"échec webhook Gainium: {resultat['erreur']}"}

            msg = formater_message(sig, execution)
            print(msg)
            envoyer_telegram(msg)
    else:
        print("Aucun nouveau signal ce run.")

    sauver_state(state)


if __name__ == "__main__":
    main()
