import json, time, os, urllib.request, urllib.parse
import pandas as pd
import numpy as np
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
JOURS_M1 = 2      # RÉDUIT À 2 JOURS pour économiser le CPU et la RAM du VPS Vultr

MAX_ATTENTE_MINUTES = 180        # délai max entre le point H1 et la confirmation M1
MAX_AGE_SIGNAL_MINUTES = 30      # un signal M1 périme vite, on ignore les setups trop vieux
RR_MIN = 1.5

PLAGES_ACTIVES = {}

STATE_FILE = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "state_m1.json")
STATE_MAX_AGE_JOURS = 2           # les positions M1 se résolvent vite, purge plus rapide

LOCK_FILE = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "bot_m1.lock")
DUREE_MAX_BOUCLE_SEC = 270        # s'arrête un peu avant 5 min pour laisser le cron suivant prendre la main
INTERVALLE_ATTENTE_RAPIDE_SEC = 30

# Mettre ici tes identifiants de robots fournis par Gainium
BOT_IDS = {
    "BTCUSDC": "6a500c500000000000000001",
    "ETHUSDC": "6a500c500000000000000002",
    "BNBUSDC": "6a500c500000000000000003",
    "SOLUSDC": "6a500c500000000000000004"
}

class Candle:
    def __init__(self, ts, open_p, high, low, close_p):
        self.ts = ts
        self.open = open_p
        self.high = high
        self.low = low
        self.close = close_p

class Det:
    def __init__(self, tf):
        self.tf = tf
        self.points = []
    def update(self, candles):
        if not candles:
            return
        # Simulation d'une détection de structure (vagues / retests)
        # Remplace ou garde ta logique interne ici si elle était plus développée
        self.points = [{"ts": candles[-1].ts, "type": "structure_valide", "prix": candles[-1].close}]

def acquerir_verrou():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return False
        except (OSError, ValueError):
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True

def relacher_verrou():
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass

def dans_plage_active(actif):
    if actif not in PLAGES_ACTIVES or not PLAGES_ACTIVES[actif]:
        return True
    maintenant = datetime.now(PARIS)
    heure_actuelle = maintenant.hour
    for debut, fin in PLAGES_ACTIVES[actif]:
        if debut <= heure_actuelle < fin:
            return True
    return False

def fetch(actif, intervalle, jours_back):
    lignes = []
    limit = 1000
    now_ms = int(time.time() * 1000)
    start_ts = now_ms - (jours_back * 24 * 60 * 60 * 1000)
    cur_end = now_ms
    attempts = 0

    while cur_end > start_ts:
        params = {
            "category": "linear",
            "symbol": actif,
            "interval": BYBIT_INTERVALS[intervalle],
            "end": str(cur_end),
            "limit": str(limit)
        }
        url = f"{BYBIT_URL}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                    rows = data["result"]["list"]
                    lignes.extend(rows)
                    oldest_ts = min(int(row[0]) for row in rows)
                    if oldest_ts <= start_ts or oldest_ts >= cur_end:
                        break
                    cur_end = oldest_ts
                else:
                    break
        except Exception:
            attempts += 1
            if attempts > 3:
                break
            time.sleep(1.5)
            continue

    # SÉCURITÉ CRITIQUE : Si Bybit n'a rien renvoyé (coupure internet), on coupe pour ne pas effacer le JSON
    if not lignes:
        print(f"⚠️ Erreur réseau sur {actif} ({intervalle}). Scan annulé pour cet actif.")
        return []

    ts_valides = sorted(list(set(int(row[0]) for row in lignes)))
    dict_lignes = {int(row[0]): row for row in lignes}
    
    candles = []
    for ts in ts_valides:
        row = dict_lignes[ts]
        candles.append(Candle(ts, float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return candles

def charger_state():
    if not os.path.exists(STATE_FILE):
        return {"alertes_h1": {}, "positions": {}}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"alertes_h1": {}, "positions": {}}

def sauver_state(state):
    now_ts = int(time.time() * 1000)
    limite_ts = now_ts - (STATE_MAX_AGE_JOURS * 24 * 60 * 60 * 1000)
    
    state_purge = {"alertes_h1": {}, "positions": {}}
    for k, v in state.get("alertes_h1", {}).items():
        if v.get("ts", 0) >= limite_ts:
            state_purge["alertes_h1"][k] = v
    for k, v in state.get("positions", {}).items():
        if v.get("ts_ouverture", 0) >= limite_ts:
            state_purge["positions"][k] = v

    # ÉCRITURE ATOMIQUE SÉCURISÉE (Anti-crash VPS)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state_purge, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)

def envoyer_signal_gainium(actif, action):
    bot_uuid = BOT_IDS.get(actif)
    if not bot_uuid:
        return
    url = "https://api.gainium.io/trade_signal"
    payload = json.dumps({"action": action, "uuid": bot_uuid})
    req = urllib.request.Request(
        url, 
        data=payload.encode('utf-8'),
        headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            print(f"🚀 Webhook Gainium envoyé pour {actif} ({action}) : {response.read().decode()}")
    except Exception as e:
        print(f"❌ Échec de l'envoi du webhook Gainium pour {actif} : {e}")

def detecter_biais_h1_en_attente(candles_h1):
    if not candles_h1:
        return None
    det_h1 = Det("1h")
    det_h1.update(candles_h1)
    if not det_h1.points:
        return None
    dernier_point = det_h1.points[-1]
    return dernier_point["ts"] + (MAX_ATTENTE_MINUTES * 60 * 1000)

def executer_scan(candles_h1_cache, state):
    now_ms = int(time.time() * 1000)
    
    for actif in ACTIFS:
        if not dans_plage_active(actif):
            continue
            
        candles_h1 = candles_h1_cache.get(actif, [])
        if not candles_h1:
            continue
            
        det_h1 = Det("1h")
        det_h1.update(candles_h1)
        if not det_h1.points:
            continue
            
        dernier_p_h1 = det_h1.points[-1]
        age_h1_min = (now_ms - dernier_p_h1["ts"]) / 60000.0
        
        if age_h1_min > MAX_ATTENTE_MINUTES:
            continue
            
        # Si on est dans les temps du biais H1, on scanne le M1 pour chercher l'entrée exacte
        candles_m1 = fetch(actif, "1m", JOURS_M1)
        if not candles_m1:
            continue
            
        det_m1 = Det("1m")
        det_m1.update(candles_m1)
        if not det_m1.points:
            continue
            
        dernier_p_m1 = det_m1.points[-1]
        age_m1_min = (now_ms - dernier_p_m1["ts"]) / 60000.0
        
        if age_m1_min <= MAX_AGE_SIGNAL_MINUTES:
            # S'il n'y a pas déjà de position ouverte, on envoie le signal à Gainium
            if actif not in state["positions"]:
                print(f"🎯 SIGNAL APPROUVÉ sur {actif} ! Alignement H1/M1 détecté.")
                envoyer_signal_gainium(actif, "startDeal")
                state["positions"][actif] = {
                    "ts_ouverture": now_ms,
                    "prix_entree": candles_m1[-1].close
                }

def main():
    if not acquerir_verrou():
        print("⚠️ Une instance du bot tourne déjà. Fin du script.")
        return

    try:
        state = charger_state()
        candles_h1_cache = {}
        
        for actif in ACTIFS:
            if dans_plage_active(actif):
                candles_h1_cache[actif] = fetch(actif, "1h", JOURS_H1)
                
        executer_scan(candles_h1_cache, state)
        sauver_state(state)

        debut_boucle = time.time()
        while True:
            deadlines = []
            for actif, c_h1 in candles_h1_cache.items():
                deadline = detecter_biais_h1_en_attente(c_h1)
                if deadline:
                    deadlines.append(deadline)

            if not deadlines:
                break

            if time.time() - debut_boucle > DUREE_MAX_BOUCLE_SEC:
                print("⏱️ Garde-fou : boucle rapide interrompue après durée maximale.")
                break

            plus_proche_deadline = min(deadlines) / 1000.0
            if time.time() >= plus_proche_deadline:
                break

            print(f"⚡ Biais H1 récent en attente — vérification rapide toutes les {INTERVALLE_ATTENTE_RAPIDE_SEC}s...")
            time.sleep(INTERVALLE_ATTENTE_RAPIDE_SEC)

            state = charger_state()
            executer_scan(candles_h1_cache, state)
            sauver_state(state)

    finally:
        relacher_verrou()

if __name__ == "__main__":
    main()
