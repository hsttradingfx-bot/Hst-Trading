import json, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"

# =========================================================
# CONFIG STRATEGIE SCALPING M1
# =========================================================
ACTIFS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
JOURS_H1 = 1200
JOURS_M1 = 1095

MAX_ATTENTE_MINUTES = 180
MAX_DUREE_TRADE_MINUTES = 180
RR_MIN = 1.5

# --- Réalisme du portefeuille ---
CAPITAL_INITIAL = 10000.0
RISQUE_POURCENT = 0.01          # 1% du capital courant (partagé) par trade
MAX_POSITIONS_CONCURRENTES = 4   # nb max de trades ouverts en même temps, tous actifs confondus
FRAIS_POURCENT_DU_RISQUE = 0.10  # approximation : frais + slippage aller-retour ≈ 10% du montant risqué
                                  # (les frais réels sont proportionnels au notionnel, pas au risque ;
                                  # ce backtest ne trackant pas le notionnel/levier réel, c'est une
                                  # approximation prudente plutôt qu'un calcul exact)


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
    total_bougies_estimees = days * (1440 if interval == "1m" else 24)
    print(f"   → récupération {symbol} {interval} sur {days}j (~{total_bougies_estimees:,} bougies attendues)")
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
            if len(all_c) % 50000 < 1000:
                print(f"      ... {len(all_c):,} bougies récupérées")
            time.sleep(0.12)
        except Exception:
            attempts += 1
            if attempts > 3:
                break
            time.sleep(1.5)
            continue
    print(f"   ✓ {symbol} {interval} : {len(all_c):,} bougies récupérées au total")
    return all_c


# =========================================================
# DETECTION DES TRADES (sans exécution) POUR UN ACTIF
# =========================================================
def detecter_trades_actif(symbol, candles_h1, candles_m1):
    if not candles_h1 or not candles_m1:
        return []

    d_h1 = Det(); d_h1.update(candles_h1)
    d_m1 = Det(); d_m1.update(candles_m1)

    trades = []
    ts_fin_dernier_trade = 0

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

            prix_entree = candles_m1[p_conf['b_idx']].close
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
            ts_sortie = None
            ajustement = prix_entree * 0.0001
            for m in range(p_conf['b_idx'] + 1, len(candles_m1)):
                c_check = candles_m1[m]
                if (c_check.ts - ts_val_conf) / 60000.0 > MAX_DUREE_TRADE_MINUTES:
                    statut = "TIMEOUT"
                    ts_sortie = c_check.ts
                    break
                if p_h1['sens'] == 'haussier':
                    if c_check.low <= (sl - ajustement):
                        statut = "SL"
                        ts_sortie = c_check.ts
                        break
                    if c_check.high >= (tp - ajustement):
                        statut = "TP"
                        ts_sortie = c_check.ts
                        break
                else:
                    if c_check.high >= (sl + ajustement):
                        statut = "SL"
                        ts_sortie = c_check.ts
                        break
                    if c_check.low <= (tp + ajustement):
                        statut = "TP"
                        ts_sortie = c_check.ts
                        break

            if statut:
                trades.append({
                    "symbol": symbol,
                    "sens": p_h1['sens'],
                    "ts_entree": ts_val_conf,
                    "ts_sortie": ts_sortie,
                    "rr": rr_theorique,
                    "statut": statut,
                })
                ts_fin_dernier_trade = ts_sortie

    return trades


# =========================================================
# SIMULATION PORTEFEUILLE PARTAGÉ (frais + concurrence + drawdown)
# =========================================================
def simuler_portefeuille(tous_les_trades):
    trades_tries = sorted(tous_les_trades, key=lambda t: t["ts_entree"])

    capital = CAPITAL_INITIAL
    positions_ouvertes = []  # liste des ts_sortie des trades actuellement ouverts
    equity_curve = [capital]
    peak = capital
    max_drawdown_pct = 0.0
    trades_executes = 0
    trades_ignores_concurrence = 0
    gains = 0
    frais_total = 0.0

    print(f"\n=======================================================")
    print(f"📜 SIMULATION PORTEFEUILLE PARTAGÉ (capital initial: {CAPITAL_INITIAL}$)")
    print("=======================================================")

    for t in trades_tries:
        # libère les créneaux des trades déjà clôturés avant l'entrée de celui-ci
        positions_ouvertes = [ts for ts in positions_ouvertes if ts > t["ts_entree"]]

        if len(positions_ouvertes) >= MAX_POSITIONS_CONCURRENTES:
            trades_ignores_concurrence += 1
            continue

        risque_montant = capital * RISQUE_POURCENT
        frais_montant = risque_montant * FRAIS_POURCENT_DU_RISQUE

        if t["statut"] == "TP":
            pnl = risque_montant * t["rr"]
            gains += 1
        elif t["statut"] == "SL":
            pnl = -risque_montant
        else:  # TIMEOUT
            pnl = 0.0

        capital += pnl - frais_montant
        frais_total += frais_montant
        trades_executes += 1
        positions_ouvertes.append(t["ts_sortie"])

        equity_curve.append(capital)
        peak = max(peak, capital)
        drawdown_pct = (peak - capital) / peak * 100 if peak > 0 else 0
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

        date_str = datetime.fromtimestamp(t["ts_entree"] / 1000.0, tz=timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d %H:%M')
        emoji = "✅" if t["statut"] == "TP" else ("❌" if t["statut"] == "SL" else "⏱️")
        print(f"{date_str} | {t['symbol']:<9} [{t['sens'][:4]}] | R:R 1:{round(t['rr'],1):<4} | {emoji} {t['statut']:<7} | Capital: {round(capital,2)}$")

    win_rate = round((gains / trades_executes) * 100, 1) if trades_executes else 0

    print("\n=======================================================")
    print("📊 RÉSULTAT RÉALISTE (capital partagé, frais inclus, positions limitées)")
    print("=======================================================")
    print(f"Trades exécutés          : {trades_executes}")
    print(f"Trades ignorés (concurrence pleine) : {trades_ignores_concurrence}")
    print(f"Win rate                 : {win_rate}%")
    print(f"Capital final            : {round(capital, 2)}$")
    print(f"Frais totaux payés (approx) : {round(frais_total, 2)}$")
    print(f"Drawdown maximum          : -{round(max_drawdown_pct, 1)}%")
    print("=======================================================")

    return {
        "trades_executes": trades_executes,
        "trades_ignores": trades_ignores_concurrence,
        "win_rate": win_rate,
        "capital_final": round(capital, 2),
        "frais_total": round(frais_total, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 1),
    }


# =========================================================
# RUN GLOBAL
# =========================================================
if __name__ == "__main__":
    tous_les_trades = []
    for actif in ACTIFS:
        print(f"\n[🚀] Récupération des données pour {actif}...")
        c_h1 = fetch(actif, "1h", JOURS_H1)
        c_m1 = fetch(actif, "1m", JOURS_M1)
        trades_actif = detecter_trades_actif(actif, c_h1, c_m1)
        print(f"   → {len(trades_actif)} trades détectés sur {actif}")
        tous_les_trades.extend(trades_actif)

    resultat = simuler_portefeuille(tous_les_trades)
              
