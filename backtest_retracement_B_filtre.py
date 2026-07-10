import json, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"

ACTIFS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
JOURS_H4 = 2500   # maximum raisonnable (BTC/ETH listés sur Binance Futures depuis fin 2019/2020)
JOURS_H1 = 2500
JOURS_M5 = 2500

# Fenêtre de recherche du point H1 inverse après la formation du B en H4
MAX_ATTENTE_H1_JOURS = 7
# Fenêtre de recherche de la confirmation M5 après le point H1 inverse
MAX_ATTENTE_M5_MINUTES = 240
MAX_DUREE_TRADE_MINUTES = 4320  # 3 jours max en position (cohérent avec l'hypothèse "1 à 3 jours")
RR_MIN = 1.0  # pas de filtre RR strict ici, on veut d'abord voir le comportement brut

# Ne considérer que les points de qualité EPA (retest confirmé) pour le signal H1 inverse
EXIGER_EPA_SUR_H1_INVERSE = True


# =========================================================
# CANDLE + DÉTECTEUR
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
    total_estime = days * {"4h": 6, "1h": 24, "5m": 288}.get(interval, 1)
    print(f"   → récupération {symbol} {interval} sur {days}j (~{total_estime:,} bougies attendues)")
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
            time.sleep(0.15)
        except Exception:
            attempts += 1
            if attempts > 5:
                break
            time.sleep(1.5)
            continue
    print(f"   ✓ {symbol} {interval} : {len(all_c):,} bougies récupérées")
    return all_c


# =========================================================
# DÉTECTION + SIMULATION DE LA STRATÉGIE RETRACEMENT
# =========================================================
def backtest_retracement(symbol, candles_h4, candles_h1, candles_m5):
    d_h4 = Det(); d_h4.update(candles_h4)
    d_h1 = Det(); d_h1.update(candles_h1)
    d_m5 = Det(); d_m5.update(candles_m5)

    trades = []
    points_h4_scannes = 0
    aucun_h1_inverse_trouve = 0
    aucune_confirmation_m5 = 0

    for p_h4 in d_h4.historique:
        if p_h4['etat'] not in ('EPA', 'IPA'):
            continue
        points_h4_scannes += 1

        ts_b4 = candles_h4[p_h4['b_idx']].ts
        sens_h4 = p_h4['sens']
        sens_inverse = 'baissier' if sens_h4 == 'haussier' else 'haussier'
        B4 = p_h4['B']

        # --- FILTRE : la bougie H4 suivante ne doit pas déjà annuler la cassure ---
        # (sinon le prix est revenu trop vite en zone d'efficience, risque de chevauchement avec Protocole A)
        idx_suivant = p_h4['b_idx'] + 1
        if idx_suivant < len(candles_h4):
            close_suivant = candles_h4[idx_suivant].close
            if sens_h4 == 'haussier' and close_suivant <= B4:
                continue  # cassure annulée en une bougie, setup invalidé
            if sens_h4 == 'baissier' and close_suivant >= B4:
                continue  # cassure annulée en une bougie, setup invalidé
        else:
            continue  # pas encore de bougie suivante disponible, on ne peut pas valider

        # --- Étape 1 : chercher le premier H1 inverse dans la fenêtre ---
        h1_trouve = None
        for p_h1 in d_h1.historique:
            if EXIGER_EPA_SUR_H1_INVERSE and p_h1['etat'] != 'EPA':
                continue
            if p_h1['etat'] not in ('EPA', 'IPA') or p_h1['sens'] != sens_inverse:
                continue
            ts_h1 = candles_h1[p_h1['b_idx']].ts
            if ts_h1 <= ts_b4:
                continue
            if (ts_h1 - ts_b4) / 86400000.0 > MAX_ATTENTE_H1_JOURS:
                continue
            h1_trouve = p_h1
            ts_h1_trouve = ts_h1
            break

        if not h1_trouve:
            aucun_h1_inverse_trouve += 1
            continue

        # --- Étape 2 : confirmation M5 dans le même sens que le H1 inverse ---
        m5_trouve = None
        for p_m5 in d_m5.historique:
            if p_m5['etat'] != 'EPA' or p_m5['sens'] != sens_inverse:
                continue
            ts_m5 = candles_m5[p_m5['b_idx']].ts
            if ts_m5 <= ts_h1_trouve:
                continue
            if (ts_m5 - ts_h1_trouve) / 60000.0 > MAX_ATTENTE_M5_MINUTES:
                continue
            m5_trouve = p_m5
            ts_m5_trouve = ts_m5
            break

        if not m5_trouve:
            aucune_confirmation_m5 += 1
            continue

        # --- Simulation du trade ---
        prix_entree = candles_m5[m5_trouve['b_idx']].close
        sl = m5_trouve['A']
        tp = B4  # la cible est le point B H4 qu'on vient de casser, PAS un nouveau calcul

        dist_sl = abs(prix_entree - sl)
        if dist_sl < (prix_entree * 0.0005):
            continue
        rr_theorique = min(abs(tp - prix_entree) / dist_sl, 30.0)
        if rr_theorique < RR_MIN:
            continue

        statut = None
        ajustement = prix_entree * 0.0001
        for m in range(m5_trouve['b_idx'] + 1, len(candles_m5)):
            c_check = candles_m5[m]
            if (c_check.ts - ts_m5_trouve) / 60000.0 > MAX_DUREE_TRADE_MINUTES:
                statut = "TIMEOUT"
                break
            if sens_inverse == 'haussier':
                if c_check.low <= (sl - ajustement):
                    statut = "SL"
                    break
                if c_check.high >= (tp - ajustement):
                    statut = "TP"
                    break
            else:
                if c_check.high >= (sl + ajustement):
                    statut = "SL"
                    break
                if c_check.low <= (tp + ajustement):
                    statut = "TP"
                    break

        if statut:
            trades.append({
                "symbol": symbol,
                "sens_h4_origine": sens_h4,
                "sens_trade": sens_inverse,
                "ts_b4": ts_b4,
                "ts_entree": ts_m5_trouve,
                "rr": round(rr_theorique, 2),
                "statut": statut,
            })

    return trades, points_h4_scannes, aucun_h1_inverse_trouve, aucune_confirmation_m5


def afficher_resultats(symbol, trades, points_h4, sans_h1, sans_m5):
    print(f"\n{'='*70}")
    print(f"📜 RÉSULTATS RETRACEMENT — {symbol}")
    print(f"{'='*70}")
    print(f"Points H4 (B formé) scannés          : {points_h4}")
    print(f"  → sans H1 inverse dans la fenêtre  : {sans_h1}")
    print(f"  → sans confirmation M5             : {sans_m5}")
    print(f"  → trades simulés                   : {len(trades)}")

    if not trades:
        return

    gains = sum(1 for t in trades if t["statut"] == "TP")
    pertes = sum(1 for t in trades if t["statut"] == "SL")
    timeouts = sum(1 for t in trades if t["statut"] == "TIMEOUT")
    wr = round(gains / len(trades) * 100, 1)
    rr_moyen = round(sum(t["rr"] for t in trades) / len(trades), 2)

    print(f"\nWin rate            : {wr}% ({gains} TP / {pertes} SL / {timeouts} TIMEOUT)")
    print(f"R:R moyen théorique  : {rr_moyen}")

    print(f"\nDétail des trades :")
    jours_semaine = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    for t in trades:
        dt_paris = datetime.fromtimestamp(t["ts_entree"] / 1000.0, tz=timezone.utc).astimezone(PARIS)
        date_str = dt_paris.strftime('%Y-%m-%d %H:%M')
        jour_str = jours_semaine[dt_paris.weekday()]
        dt_b4 = datetime.fromtimestamp(t["ts_b4"] / 1000.0, tz=timezone.utc).astimezone(PARIS)
        delai_h4_entree_h = round((t["ts_entree"] - t["ts_b4"]) / 3600000.0, 1)
        emoji = "✅" if t["statut"] == "TP" else ("❌" if t["statut"] == "SL" else "⏱️")
        print(f"  {date_str} ({jour_str:<9}) | B H4 formé le {dt_b4.strftime('%Y-%m-%d %H:%M')} (+{delai_h4_entree_h}h) | H4={t['sens_h4_origine']:<8} -> trade {t['sens_trade']:<8} | R:R 1:{t['rr']:<5} | {emoji} {t['statut']}")


# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    tous_les_trades = []

    for actif in ACTIFS:
        print(f"\n[🚀] {actif}...")
        c_h4 = fetch(actif, "4h", JOURS_H4)
        c_h1 = fetch(actif, "1h", JOURS_H1)
        c_m5 = fetch(actif, "5m", JOURS_M5)

        trades, points_h4, sans_h1, sans_m5 = backtest_retracement(actif, c_h4, c_h1, c_m5)
        afficher_resultats(actif, trades, points_h4, sans_h1, sans_m5)
        tous_les_trades.extend(trades)
        time.sleep(0.3)

    print(f"\n{'='*70}")
    print(f"📊 RÉSULTAT GLOBAL (tous actifs confondus)")
    print(f"{'='*70}")
    if tous_les_trades:
        gains = sum(1 for t in tous_les_trades if t["statut"] == "TP")
        wr = round(gains / len(tous_les_trades) * 100, 1)
        rr_moyen = round(sum(t["rr"] for t in tous_les_trades) / len(tous_les_trades), 2)
        print(f"Trades totaux : {len(tous_les_trades)}")
        print(f"Win rate      : {wr}%")
        print(f"R:R moyen     : {rr_moyen}")

        # Ventilation par jour de la semaine
        jours_semaine = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
        par_jour = {j: {"n": 0, "tp": 0} for j in jours_semaine}
        par_heure = {}
        for t in tous_les_trades:
            dt_paris = datetime.fromtimestamp(t["ts_entree"] / 1000.0, tz=timezone.utc).astimezone(PARIS)
            j = jours_semaine[dt_paris.weekday()]
            par_jour[j]["n"] += 1
            if t["statut"] == "TP":
                par_jour[j]["tp"] += 1
            h = dt_paris.hour
            par_heure.setdefault(h, {"n": 0, "tp": 0})
            par_heure[h]["n"] += 1
            if t["statut"] == "TP":
                par_heure[h]["tp"] += 1

        print(f"\n📅 Ventilation par jour de la semaine (heure d'entrée, Paris) :")
        for j in jours_semaine:
            d = par_jour[j]
            if d["n"] > 0:
                wr_j = round(d["tp"] / d["n"] * 100, 1)
                print(f"   {j:<10} : {d['n']} trades | WR {wr_j}%")

        print(f"\n🕐 Ventilation par heure d'entrée (Paris) :")
        for h in sorted(par_heure.keys()):
            d = par_heure[h]
            wr_h = round(d["tp"] / d["n"] * 100, 1)
            print(f"   {h:02d}h : {d['n']} trades | WR {wr_h}%")
    else:
        print("Aucun trade simulé sur l'ensemble des actifs.")
