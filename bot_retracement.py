import json, time, os, urllib.request, urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
BYBIT_URL = "https://api.bybit.eu/v5/market/kline"

BYBIT_INTERVALS = {
    "1h": "60",
    "1m": "1",
}

# =========================================================
# CONFIG
# =========================================================
ACTIFS = ["BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC"]
JOURS_H1 = 45     # historique nécessaire pour resynchroniser le détecteur H1
JOURS_M1 = 5      # fenêtre M1 pour chercher la confirmation inverse

MAX_ATTENTE_MINUTES = 24 * 60     # 24h max entre la formation du B (H1) et la confirmation M1 inverse
MAX_AGE_SIGNAL_MINUTES = 30       # un signal M1 périme vite, on ignore les setups trop vieux
RR_MIN = 1.5
EXIGER_EPA_SUR_M1_INVERSE = True  # ne considérer que les points de qualité EPA pour le signal inverse

# Pas de filtre horaire par défaut (le backtest tournait 24h/24).
# Ajoute des plages ici si tu veux restreindre, ex: "BTCUSDC": [(9, 22)]
PLAGES_ACTIVES = {}

STATE_FILE = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "state_retracement.json")
STATE_MAX_AGE_JOURS = 2           # les positions M1 se résolvent vite, purge plus rapide

LOCK_FILE = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "bot_retracement.lock")
INTERVALLE_ATTENTE_RAPIDE_SEC = 30   # fréquence de revérification pendant une fenêtre de confirmation active
DUREE_MAX_BOUCLE_SEC = 200 * 60      # garde-fou absolu (un peu au-dessus de MAX_ATTENTE_MINUTES)

LOG_VERBOSE = False               # False par défaut ici : trop de comparaisons H1×M1 sur plusieurs jours,
                                   # passe à True seulement si tu dois débugger un run précis

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# =========================================================
# EXÉCUTION VIA GAINIUM (webhooks) — bots séparés du 1er robot
# =========================================================
GAINIUM_WEBHOOK_URL = "https://api.gainium.io/trade_signal"
EXECUTION_REELLE = True

BOT_IDS = {
    "BTCUSDC": {"haussier": "6a514fd08475e9cdd4b7d8d7", "baissier": "6a514ffc8475e9cdd4b819ea"},
    "ETHUSDC": {"haussier": "6a51506e8475e9cdd4b8c10d", "baissier": "6a51508b8475e9cdd4b8e8d0"},
    "BNBUSDC": {"haussier": "6a5150bc8475e9cdd4b93382", "baissier": "6a5150d08475e9cdd4b94f29"},
    "SOLUSDC": {"haussier": "6a51502e8475e9cdd4b8676d", "baissier": "6a51503e8475e9cdd4b87f27"},
}


def acquerir_verrou():
    """Empêche deux exécutions simultanées (le cron externe pourrait se déclencher
    pendant que la boucle rapide interne tourne encore)."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                ancien_pid = int(f.read().strip())
            os.kill(ancien_pid, 0)  # ne tue rien, vérifie juste si le processus existe encore
            print(f"⏸️ Une autre exécution (PID {ancien_pid}) tourne déjà — on quitte proprement.")
            return False
        except (ValueError, ProcessLookupError, OSError):
            pass  # le verrou est périmé (processus mort), on l'ignore et on le remplace
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def liberer_verrou():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


def detecter_biais_h1_en_attente(candles_h1):
    """Retourne l'horodatage limite (deadline) au-delà duquel il n'est plus utile
    d'attendre une confirmation M1 pour ce symbole, ou None si rien de récent."""
    d_h1 = Det(); d_h1.update(candles_h1)
    now_ms = int(time.time() * 1000)
    deadline_la_plus_tardive = None
    for p_h1 in d_h1.historique:
        if p_h1['etat'] not in ('EPA', 'IPA'):
            continue
        ts_val_h1 = candles_h1[p_h1['b_idx']].ts
        deadline = ts_val_h1 + MAX_ATTENTE_MINUTES * 60000
        if now_ms < deadline:
            if deadline_la_plus_tardive is None or deadline > deadline_la_plus_tardive:
                deadline_la_plus_tardive = deadline
    return deadline_la_plus_tardive


def log_rejet(symbol, sens, ts_conf, raison, **details):
    if not LOG_VERBOSE:
        return
    date_str = datetime.fromtimestamp(ts_conf / 1000.0, tz=timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d %H:%M')
    extra = " ".join(f"{k}={v}" for k, v in details.items())
    print(f"   ✗ REJET {symbol} [{sens}] signal M1 {date_str} -> {raison} {extra}")


# =========================================================
# CANDLE + DÉTECTEUR (identique au bot principal)
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
    interval_code = BYBIT_INTERVALS[interval]
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - days * 86400000
    lignes = {}
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


# =========================================================
# ÉTAT (anti-doublons + positions ouvertes)
# =========================================================
def gerer_heartbeat_quotidien(state):
    """Envoie un message Telegram une fois par jour pour confirmer que le bot tourne,
    même s'il n'y a aucun signal à ce moment-là."""
    today_str = datetime.now(timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d')
    dernier = state.get("_dernier_heartbeat_date")
    runs = state.get("_runs_depuis_heartbeat", 0) + 1

    if dernier != today_str:
        if dernier is not None:  # pas de message au tout premier lancement après déploiement
            envoyer_telegram(f"✅ [RETRACEMENT] Bot actif — {runs} vérifications effectuées depuis hier, aucun souci technique.")
        state["_dernier_heartbeat_date"] = today_str
        state["_runs_depuis_heartbeat"] = 1
    else:
        state["_runs_depuis_heartbeat"] = runs


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
        if k in ("_positions_ouvertes", "_dernier_heartbeat_date", "_runs_depuis_heartbeat"):
            state_purge[k] = v
        elif now_ms - v <= max_age_ms:
            state_purge[k] = v
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state_purge, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


# =========================================================
# WEBHOOKS GAINIUM
# =========================================================
def envoyer_webhook_gainium(action, bot_uuid):
    payload = json.dumps({"action": action, "uuid": bot_uuid}).encode()
    req = urllib.request.Request(GAINIUM_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"succes": True, "reponse": r.read().decode()}
    except Exception as e:
        return {"succes": False, "erreur": str(e)}


def ouvrir_position_gainium(symbol, sens):
    bot_uuid = BOT_IDS.get(symbol, {}).get(sens)
    if not bot_uuid or bot_uuid.startswith("REMPLACE_MOI"):
        return {"succes": False, "erreur": f"bot_id non configuré pour {symbol}/{sens}"}
    return envoyer_webhook_gainium("startDeal", bot_uuid)


def fermer_position_gainium(symbol, sens):
    bot_uuid = BOT_IDS.get(symbol, {}).get(sens)
    if not bot_uuid or bot_uuid.startswith("REMPLACE_MOI"):
        return {"succes": False, "erreur": f"bot_id non configuré pour {symbol}/{sens}"}
    return envoyer_webhook_gainium("closeDeal", bot_uuid)


def surveiller_positions_ouvertes(state, candles_par_symbole):
    positions = state.get("_positions_ouvertes", {})
    a_supprimer = []

    for cle, pos in positions.items():
        symbol = pos["symbol"]
        candles_m1 = candles_par_symbole.get(symbol)
        if not candles_m1:
            continue

        touche = None
        for c in candles_m1:
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
                envoyer_telegram(f"{emoji} [M1] {symbol} [{pos['sens']}] : {touche} touché, clôture envoyée à Gainium.")
            else:
                envoyer_telegram(f"⚠️ [M1] {symbol} [{pos['sens']}] : {touche} touché mais échec de clôture Gainium ({resultat['erreur']}) — vérifie manuellement.")
            a_supprimer.append(cle)

    for cle in a_supprimer:
        del positions[cle]
    state["_positions_ouvertes"] = positions


def dans_plage_active(symbol):
    plages = PLAGES_ACTIVES.get(symbol)
    if not plages:
        return True  # pas de restriction par défaut pour la stratégie M1
    h_paris = datetime.now(timezone.utc).astimezone(PARIS).hour
    for debut, fin in plages:
        if debut <= fin:
            if debut <= h_paris < fin:
                return True
        else:
            if h_paris >= debut or h_paris < fin:
                return True
    return False


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
# DÉTECTION DES SIGNAUX LIVE (H1 biais+cible / M1 exécution)
# =========================================================
def detecter_signaux(symbol, candles_h1, candles_m1, state):
    if not candles_h1 or not candles_m1:
        return []

    d_h1 = Det(); d_h1.update(candles_h1)
    d_m1 = Det(); d_m1.update(candles_m1)

    now_ms = int(time.time() * 1000)
    nouveaux_signaux = []

    for p_h1 in d_h1.historique:
        if p_h1['etat'] not in ('EPA', 'IPA'):
            continue
        ts_b1 = candles_h1[p_h1['b_idx']].ts
        sens_h1 = p_h1['sens']
        sens_inverse = 'baissier' if sens_h1 == 'haussier' else 'haussier'
        B1 = p_h1['B']

        for p_m1 in d_m1.historique:
            if EXIGER_EPA_SUR_M1_INVERSE and p_m1['etat'] != 'EPA':
                continue
            if p_m1['etat'] not in ('EPA', 'IPA') or p_m1['sens'] != sens_inverse:
                continue
            ts_m1 = candles_m1[p_m1['b_idx']].ts

            if (now_ms - ts_m1) / 60000.0 > MAX_AGE_SIGNAL_MINUTES:
                log_rejet(symbol, sens_inverse, ts_m1, "trop vieux (> MAX_AGE_SIGNAL_MINUTES)")
                continue
            if not (0 <= (ts_m1 - ts_b1) / 60000.0 <= MAX_ATTENTE_MINUTES):
                log_rejet(symbol, sens_inverse, ts_m1, "délai B(H1)->M1 hors bornes")
                continue

            prix_entree = candles_m1[p_m1['b_idx']].close  # toujours Market
            sl = p_m1['A']
            tp = B1  # cible = le point B H1 qu'on vient de casser, jamais recalculé

            if None in [prix_entree, sl, tp]:
                continue
            dist_sl = abs(prix_entree - sl)
            if dist_sl < (prix_entree * 0.0005):
                log_rejet(symbol, sens_inverse, ts_m1, "SL trop proche du prix d'entrée")
                continue
            rr_theorique = min(abs(tp - prix_entree) / dist_sl, 30.0)
            if rr_theorique < RR_MIN:
                log_rejet(symbol, sens_inverse, ts_m1, "RR insuffisant", rr=round(rr_theorique, 2))
                continue

            # Signal expiré si SL/TP déjà touché depuis sa formation
            statut = None
            ajustement = prix_entree * 0.0001
            for m in range(p_m1['b_idx'] + 1, len(candles_m1)):
                c_check = candles_m1[m]
                if sens_inverse == 'haussier':
                    if c_check.low <= (sl - ajustement) or c_check.high >= (tp - ajustement):
                        statut = "EXPIRE"
                        break
                else:
                    if c_check.high >= (sl + ajustement) or c_check.low <= (tp + ajustement):
                        statut = "EXPIRE"
                        break
            if statut == "EXPIRE":
                log_rejet(symbol, sens_inverse, ts_m1, "signal caduc (SL/TP déjà touché)")
                continue

            cle = f"{symbol}_{p_h1['b_idx']}_{p_m1['b_idx']}"
            if cle in state:
                log_rejet(symbol, sens_inverse, ts_m1, "déjà alerté")
                continue

            state[cle] = now_ms
            nouveaux_signaux.append({
                "symbol": symbol,
                "sens": sens_inverse,
                "prix_entree": prix_entree,
                "sl": sl,
                "tp": tp,
                "rr": round(rr_theorique, 2),
                "ts_m1": ts_m1,
            })

    return nouveaux_signaux


def formater_message(sig, execution=None):
    emoji = "🟢" if sig["sens"] == "haussier" else "🔴"
    direction = "LONG" if sig["sens"] == "haussier" else "SHORT"
    date_str = datetime.fromtimestamp(sig["ts_m1"] / 1000.0, tz=timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d %H:%M')
    base = (
        f"{emoji} <b>[RETRACEMENT] {sig['symbol']} - {direction}</b>\n"
        f"Entrée: {sig['prix_entree']:.4f}\n"
        f"SL: {sig['sl']:.4f}\n"
        f"TP: {sig['tp']:.4f}\n"
        f"R:R ≈ 1:{sig['rr']}\n"
        f"Détecté: {date_str} (Paris)"
    )
    if execution is None:
        return base
    if execution.get("ordre_place"):
        base += "\n\n✅ <b>DEAL OUVERT SUR GAINIUM</b>"
    else:
        base += f"\n\n⛔ <b>NON EXÉCUTÉ</b> — {execution.get('raison', 'raison inconnue')}"
    return base


# =========================================================
# MAIN
# =========================================================
def executer_scan(candles_h1_cache, state):
    """Un passage complet : fetch M1 (H1 réutilisé depuis le cache), détection, exécution."""
    tous_les_signaux = []
    candles_par_symbole = {}

    for actif in ACTIFS:
        if not dans_plage_active(actif):
            continue
        c_h1 = candles_h1_cache[actif]
        c_m1 = fetch(actif, "1m", JOURS_M1)
        candles_par_symbole[actif] = c_m1
        signaux = detecter_signaux(actif, c_h1, c_m1, state)
        tous_les_signaux.extend(signaux)
        time.sleep(0.3)

    if EXECUTION_REELLE:
        surveiller_positions_ouvertes(state, candles_par_symbole)

    if tous_les_signaux:
        for sig in tous_les_signaux:
            execution = None
            if EXECUTION_REELLE:
                resultat = ouvrir_position_gainium(sig["symbol"], sig["sens"])
                if resultat["succes"]:
                    execution = {"ordre_place": True}
                    positions = state.setdefault("_positions_ouvertes", {})
                    positions[f"{sig['symbol']}_{sig['sens']}_{sig['ts_m1']}"] = {
                        "symbol": sig["symbol"],
                        "sens": sig["sens"],
                        "sl": sig["sl"],
                        "tp": sig["tp"],
                        "ts_ouverture": sig["ts_m1"],
                    }
                else:
                    execution = {"ordre_place": False, "raison": f"échec webhook Gainium: {resultat['erreur']}"}

            msg = formater_message(sig, execution)
            print(msg)
            envoyer_telegram(msg)
    else:
        print("Aucun nouveau signal M1 ce run.")

    return candles_h1_cache


def main():
    if not acquerir_verrou():
        return

    try:
        state = charger_state()
        gerer_heartbeat_quotidien(state)

        # 1er passage : fetch H1 (une fois) + M1, détection normale
        candles_h1_cache = {}
        for actif in ACTIFS:
            if dans_plage_active(actif):
                candles_h1_cache[actif] = fetch(actif, "1h", JOURS_H1)
        executer_scan(candles_h1_cache, state)
        sauver_state(state)

        # Vérifie s'il faut passer en mode "boucle rapide" (biais H1 récent en attente de confirmation)
        debut_boucle = time.time()
        while True:
            deadlines = []
            for actif, c_h1 in candles_h1_cache.items():
                deadline = detecter_biais_h1_en_attente(c_h1)
                if deadline:
                    deadlines.append(deadline)

            if not deadlines:
                break  # aucun biais récent en attente -> on repasse la main au cron normal (5 min)

            if time.time() - debut_boucle > DUREE_MAX_BOUCLE_SEC:
                print("⏱️ Garde-fou : boucle rapide interrompue après durée maximale.")
                break

            plus_proche_deadline = min(deadlines) / 1000.0
            if time.time() >= plus_proche_deadline:
                break  # la fenêtre d'attente est passée, rien de plus à surveiller rapidement

            print(f"⚡ Biais H1 récent en attente de confirmation M1 — vérification rapide (toutes les {INTERVALLE_ATTENTE_RAPIDE_SEC}s)...")
            time.sleep(INTERVALLE_ATTENTE_RAPIDE_SEC)

            state = charger_state()
            executer_scan(candles_h1_cache, state)
            sauver_state(state)

    finally:
        liberer_verrou()


if __name__ == "__main__":
    main()
