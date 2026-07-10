import json, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"

ACTIFS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
FENETRES_JOURS = [2000, 1000]  # on mesure sur les deux périodes demandées


# =========================================================
# CANDLE + DÉTECTEUR (identique aux scripts précédents)
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
# FETCH BINANCE (Weekly / Daily -> volumes légers, pas de pagination lourde)
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
            time.sleep(0.15)
        except Exception:
            attempts += 1
            if attempts > 5:
                break
            time.sleep(1.5)
            continue
    return all_c


# =========================================================
# MESURE DE SYNCHRONISATION WEEKLY -> DAILY
# =========================================================
def mesurer_synchronisation(symbol, candles_w, candles_d):
    d_w = Det(); d_w.update(candles_w)
    d_d = Det(); d_d.update(candles_d)

    deltas_jours = []
    invalidations = 0

    for p_w in d_w.historique:
        if p_w['etat'] != 'EPA':
            continue
        ts_w = candles_w[p_w['b_idx']].ts
        sens_w = p_w['sens']
        A_w = p_w['A']

        # Cherche la première confirmation Daily EPA dans le même sens, après ts_w,
        # sans que le prix Daily ne soit jamais revenu toucher le point A Weekly avant.
        confirmation_trouvee = False
        invalide = False

        for p_d in d_d.historique:
            if p_d['etat'] != 'EPA' or p_d['sens'] != sens_w:
                continue
            ts_d = candles_d[p_d['b_idx']].ts
            if ts_d <= ts_w:
                continue

            # Vérifie si le prix a touché le point A Weekly entre ts_w et ts_d
            touche_A = False
            for c in candles_d:
                if c.ts <= ts_w or c.ts >= ts_d:
                    continue
                if sens_w == 'haussier' and c.low <= A_w:
                    touche_A = True
                    break
                if sens_w == 'baissier' and c.high >= A_w:
                    touche_A = True
                    break

            if touche_A:
                invalide = True
                break  # cette tentative de synchro est invalidée, on arrête pour ce point Weekly

            delta_jours = (ts_d - ts_w) / 86400000.0
            deltas_jours.append(delta_jours)
            confirmation_trouvee = True
            break  # on ne garde que la première confirmation valide

        if invalide and not confirmation_trouvee:
            invalidations += 1

    return deltas_jours, invalidations


def stats(deltas):
    if not deltas:
        return None
    deltas_tries = sorted(deltas)
    n = len(deltas_tries)
    moyenne = sum(deltas_tries) / n
    mediane = deltas_tries[n // 2] if n % 2 == 1 else (deltas_tries[n // 2 - 1] + deltas_tries[n // 2]) / 2
    return {
        "n": n,
        "moyenne_jours": round(moyenne, 2),
        "mediane_jours": round(mediane, 2),
        "min_jours": round(deltas_tries[0], 2),
        "max_jours": round(deltas_tries[-1], 2),
    }


# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    for jours in FENETRES_JOURS:
        print(f"\n{'='*70}")
        print(f"📊 FENÊTRE : {jours} JOURS")
        print(f"{'='*70}")

        tous_les_deltas = []
        total_invalidations = 0

        for actif in ACTIFS:
            print(f"\n[🚀] {actif} — récupération Weekly + Daily sur {jours}j...")
            c_w = fetch(actif, "1w", jours)
            c_d = fetch(actif, "1d", jours)
            print(f"   ✓ {len(c_w)} bougies Weekly, {len(c_d)} bougies Daily")

            deltas, invalidations = mesurer_synchronisation(actif, c_w, c_d)
            total_invalidations += invalidations
            s = stats(deltas)

            if s:
                print(f"   → {s['n']} synchronisations valides | moyenne: {s['moyenne_jours']}j | médiane: {s['mediane_jours']}j | min: {s['min_jours']}j | max: {s['max_jours']}j")
            else:
                print(f"   → Aucune synchronisation valide trouvée")
            print(f"   → {invalidations} points Weekly invalidés avant confirmation Daily")

            tous_les_deltas.extend(deltas)
            time.sleep(0.3)

        print(f"\n{'-'*70}")
        print(f"📈 RÉSULTAT GLOBAL ({jours} jours, tous actifs confondus)")
        print(f"{'-'*70}")
        s_global = stats(tous_les_deltas)
        if s_global:
            print(f"Nombre de synchronisations valides : {s_global['n']}")
            print(f"Temps de synchronisation moyen      : {s_global['moyenne_jours']} jours")
            print(f"Temps de synchronisation médian      : {s_global['mediane_jours']} jours")
            print(f"Min / Max                           : {s_global['min_jours']}j / {s_global['max_jours']}j")
        print(f"Total points Weekly invalidés avant confirmation : {total_invalidations}")
