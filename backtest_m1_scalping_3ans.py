import json, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"

# =========================================================
# CONFIG STRATEGIE SCALPING M1
# =========================================================
ACTIFS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
JOURS_H1 = 200      # historique H1 (biais + niveau cible)
JOURS_M1 = 60       # historique M1 (entrée + SL) - déjà volumineux en nb de bougies

MAX_ATTENTE_MINUTES = 180   # délai max entre le point H1 et la confirmation M1
MAX_DUREE_TRADE_MINUTES = 180  # au-delà, on considère le trade en TIMEOUT
RR_MIN = 1.5


# =========================================================
# CANDLE + DETECTEUR (identique à bot.py)
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


# =========================================================
# BACKTEST STRATEGIE SCALPING M1 (biais+cible H1, exécution M1)
# =========================================================
def executer_backtest_actif(symbol, candles_h1, candles_m1):
    if not candles_h1 or not candles_m1:
        return 0, 0.0, 0.0, 10000.0

    d_h1 = Det(); d_h1.update(candles_h1)
    d_m1 = Det(); d_m1.update(candles_m1)

    capital = 10000.0
    risque_pourcent = 0.01
    total_trades = 0
    gains = 0
    rr_list = []
    ts_fin_dernier_trade = 0

    print(f"\n=======================================================")
    print(f"📜 LISTE DES TRADES SCALPING M1 POUR {symbol}")
    print("=======================================================")

    for p_h1 in d_h1.historique:
        if p_h1['etat'] != 'EPA':
            continue
        ts_val_h1 = candles_h1[p_h1['b_idx']].ts

        for p_conf in d_m1.historique:
            if p_conf['etat'] != 'EPA' or p_conf['sens'] != p_h1['sens']:
                continue
            ts_val_conf = candles_m1[p_conf['b_idx']].ts

            if ts_val_conf < ts_fin_dernier_trade:
                continue
            if not (0 <= (ts_val_conf - ts_val_h1) / 60000.0 <= MAX_ATTENTE_MINUTES):
                continue

            prix_entree = candles_m1[p_conf['b_idx']].close  # entrée Market uniquement
            sl = p_conf['A']
            tp = p_h1['B']

            if None in [prix_entree, sl, tp]:
                continue
            dist_sl = abs(prix_entree - sl)
            if dist_sl < (prix_entree * 0.0005):
                continue

            rr_theorique = min(abs(tp - prix_entree) / dist_sl, 30.0)
            if rr_theorique < RR_MIN:
                continue

            statut = None
            ajustement = prix_entree * 0.0001
            for m in range(p_conf['b_idx'] + 1, len(candles_m1)):
                c_check = candles_m1[m]
                if (c_check.ts - ts_val_conf) / 60000.0 > MAX_DUREE_TRADE_MINUTES:
                    statut = "⏱️ TIMEOUT"
                    break
                if p_h1['sens'] == 'haussier':
                    if c_check.low <= (sl - ajustement):
                        statut = "❌ SL"
                        break
                    if c_check.high >= (tp - ajustement):
                        statut = "✅ TP"
                        break
                else:
                    if c_check.high >= (sl + ajustement):
                        statut = "❌ SL"
                        break
                    if c_check.low <= (tp + ajustement):
                        statut = "✅ TP"
                        break

            if statut:
                total_trades += 1
                pnl = (capital * risque_pourcent * rr_theorique) if "TP" in statut else (-capital * risque_pourcent)
                capital += pnl if "TIMEOUT" not in statut else 0
                if "TP" in statut:
                    gains += 1
                rr_list.append(rr_theorique)
                ts_fin_dernier_trade = candles_m1[m].ts
                date_str = datetime.fromtimestamp(ts_val_conf / 1000.0, tz=timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d %H:%M')
                print(f"Trade #{total_trades:<3} | {date_str} | R:R: 1:{round(rr_theorique,1):<4} | {statut}")

    return total_trades, round((gains/total_trades)*100 if total_trades else 0, 1), round(sum(rr_list)/len(rr_list) if rr_list else 0, 1), round(capital, 2)


# =========================================================
# RUN GLOBAL
# =========================================================
if __name__ == "__main__":
    resultats_globaux = {}
    for actif in ACTIFS:
        print(f"\n[🚀] Récupération des données pour {actif}...")
        c_h1 = fetch(actif, "1h", JOURS_H1)
        c_m1 = fetch(actif, "1m", JOURS_M1)

        trades, wr, rr, cap_final = executer_backtest_actif(actif, c_h1, c_m1)
        resultats_globaux[actif] = {"trades": trades, "wr": wr, "rr": rr, "cap_final": cap_final}

    print("\n=======================================================")
    print(f"📊 SYNTHÈSE SCALPING M1 (H1 biais+cible / M1 exécution) — {JOURS_M1}J")
    print("=======================================================")
    print(f"{'Actif':<12} | {'Trades':<8} | {'Win Rate':<10} | {'R:R Moyen':<10} | {'Capital Final':<15}")
    print("-" * 65)
    for actif, res in resultats_globaux.items():
        if res["trades"] > 0:
            print(f"{actif:<12} | {res['trades']:<8} | {res['wr']}%     | {res['rr']:<10} | {res['cap_final']}$")
    print("=======================================================")
                
