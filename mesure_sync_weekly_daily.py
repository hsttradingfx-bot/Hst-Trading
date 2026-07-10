import json, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"

ACTIFS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
JOURS_WEEKLY_DAILY = 2000       # fenêtre principale demandée
JOURS_FENETRE_M1 = 20           # fenêtre de recherche M1 après chaque confirmation Daily
                                 # (large marge : la synchro M1 devrait se faire en heures, pas en semaines)


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
def fetch(symbol, interval, start_ts=None, end_ts=None, days=None):
    if end_ts is None:
        end_ts = int(time.time() * 1000)
    if start_ts is None:
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
# ÉTAGE 1 : WEEKLY -> DAILY
# =========================================================
def mesurer_weekly_vers_daily(symbol, candles_w, candles_d):
    d_w = Det(); d_w.update(candles_w)
    d_d = Det(); d_d.update(candles_d)

    confirmations = []  # liste des tuples validés pour l'étage suivant
    deltas_jours = []
    invalidations = 0

    for p_w in d_w.historique:
        if p_w['etat'] != 'EPA':
            continue
        ts_w = candles_w[p_w['b_idx']].ts
        sens_w = p_w['sens']
        A_w = p_w['A']
        B_w = p_w['B']

        for p_d in d_d.historique:
            if p_d['etat'] != 'EPA' or p_d['sens'] != sens_w:
                continue
            ts_d = candles_d[p_d['b_idx']].ts
            if ts_d <= ts_w:
                continue

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
                invalidations += 1
                break

            delta_jours = (ts_d - ts_w) / 86400000.0
            deltas_jours.append(delta_jours)
            confirmations.append({
                "symbol": symbol,
                "sens": sens_w,
                "ts_w": ts_w,
                "A_w": A_w,
                "B_w": B_w,
                "ts_d": ts_d,
                "A_d": p_d['A'],
            })
            break

    return confirmations, deltas_jours, invalidations


# =========================================================
# ÉTAGE 2 : DAILY -> M1
# =========================================================
def mesurer_daily_vers_m1(confirmation):
    symbol = confirmation["symbol"]
    sens = confirmation["sens"]
    ts_d = confirmation["ts_d"]
    A_d = confirmation["A_d"]

    end_fenetre = ts_d + JOURS_FENETRE_M1 * 86400000
    candles_m1 = fetch(symbol, "1m", start_ts=ts_d, end_ts=end_fenetre)
    if not candles_m1:
        return None, "pas_de_donnees"

    d_m1 = Det(); d_m1.update(candles_m1)

    for p_m1 in d_m1.historique:
        if p_m1['etat'] != 'EPA' or p_m1['sens'] != sens:
            continue
        ts_m1 = candles_m1[p_m1['b_idx']].ts
        if ts_m1 <= ts_d:
            continue

        touche_A = False
        for c in candles_m1:
            if c.ts <= ts_d or c.ts >= ts_m1:
                continue
            if sens == 'haussier' and c.low <= A_d:
                touche_A = True
                break
            if sens == 'baissier' and c.high >= A_d:
                touche_A = True
                break

        if touche_A:
            return None, "invalide"

        delta_heures = (ts_m1 - ts_d) / 3600000.0
        return delta_heures, "ok"

    return None, "aucune_confirmation_dans_la_fenetre"


def stats(valeurs):
    if not valeurs:
        return None
    v = sorted(valeurs)
    n = len(v)
    moyenne = sum(v) / n
    mediane = v[n // 2] if n % 2 == 1 else (v[n // 2 - 1] + v[n // 2]) / 2
    return {"n": n, "moyenne": round(moyenne, 2), "mediane": round(mediane, 2), "min": round(v[0], 2), "max": round(v[-1], 2)}


# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    toutes_confirmations = []
    tous_deltas_wd = []
    total_invalidations_wd = 0

    print("="*70)
    print(f"ÉTAGE 1 : WEEKLY -> DAILY (fenêtre {JOURS_WEEKLY_DAILY}j)")
    print("="*70)

    for actif in ACTIFS:
        print(f"\n[🚀] {actif} — Weekly + Daily...")
        c_w = fetch(actif, "1w", days=JOURS_WEEKLY_DAILY)
        c_d = fetch(actif, "1d", days=JOURS_WEEKLY_DAILY)
        confirmations, deltas, invalidations = mesurer_weekly_vers_daily(actif, c_w, c_d)
        print(f"   → {len(confirmations)} confirmations Daily valides, {invalidations} invalidées")
        toutes_confirmations.extend(confirmations)
        tous_deltas_wd.extend(deltas)
        total_invalidations_wd += invalidations
        time.sleep(0.3)

    s_wd = stats(tous_deltas_wd)
    print(f"\n📈 STATS ÉTAGE 1 (Weekly->Daily, en jours) :")
    if s_wd:
        print(f"   n={s_wd['n']} | moyenne={s_wd['moyenne']}j | médiane={s_wd['mediane']}j | min={s_wd['min']}j | max={s_wd['max']}j")
    print(f"   invalidations : {total_invalidations_wd}")

    print("\n" + "="*70)
    print(f"ÉTAGE 2 : DAILY -> M1 (fenêtre de recherche {JOURS_FENETRE_M1}j après chaque confirmation)")
    print("="*70)

    deltas_dm1 = []
    invalidations_dm1 = 0
    sans_confirmation = 0

    for i, conf in enumerate(toutes_confirmations, 1):
        date_str = datetime.fromtimestamp(conf['ts_d'] / 1000.0, tz=timezone.utc).astimezone(PARIS).strftime('%Y-%m-%d')
        print(f"[{i}/{len(toutes_confirmations)}] {conf['symbol']} [{conf['sens']}] confirmé le {date_str} — recherche M1...")
        delta_h, statut = mesurer_daily_vers_m1(conf)
        if statut == "ok":
            deltas_dm1.append(delta_h)
            print(f"      ✓ confirmation M1 après {round(delta_h, 1)}h")
        elif statut == "invalide":
            invalidations_dm1 += 1
            print(f"      ✗ invalidé (prix revenu sur le point A du Daily)")
        else:
            sans_confirmation += 1
            print(f"      ⏱️ aucune confirmation M1 trouvée dans la fenêtre de {JOURS_FENETRE_M1}j")
        time.sleep(0.5)

    s_dm1 = stats(deltas_dm1)
    print(f"\n📈 STATS ÉTAGE 2 (Daily->M1, en heures) :")
    if s_dm1:
        print(f"   n={s_dm1['n']} | moyenne={s_dm1['moyenne']}h | médiane={s_dm1['mediane']}h | min={s_dm1['min']}h | max={s_dm1['max']}h")
    print(f"   invalidations : {invalidations_dm1} | sans confirmation dans la fenêtre : {sans_confirmation}")

    print("\n" + "="*70)
    print("📊 RÉCAPITULATIF GLOBAL DE LA CASCADE WEEKLY -> DAILY -> M1")
    print("="*70)
    print(f"Points Weekly EPA de départ           : {len(tous_deltas_wd) + total_invalidations_wd}")
    print(f"  → confirmés en Daily                : {len(toutes_confirmations)}")
    print(f"  → invalidés avant confirmation Daily : {total_invalidations_wd}")
    print(f"  → confirmés en M1 (trade complet)   : {len(deltas_dm1)}")
    print(f"  → invalidés avant confirmation M1    : {invalidations_dm1}")
    print(f"  → sans confirmation M1 (fenêtre expirée) : {sans_confirmation}")
    if toutes_confirmations:
        taux_final = round(len(deltas_dm1) / len(toutes_confirmations) * 100, 1)
        print(f"\nTaux de conversion Daily -> trade complet (M1) : {taux_final}%")
