"""
follow_logic.py - Logiques de sortie pour le suivi automatique du journal
=========================================================================

Deux fonctions, une par bot, avec la MEME logique que leur backtest respectif.
Chaque fonction recoit (trade, candles_recentes) et renvoie :
    (keep_open, exit_reason, r_result, updated_trade)

candles : liste de bougies M5 recentes, chacune un dict {ts, open, high, low, close}.
On ne regarde que les bougies APRES l'entree du trade.
"""


def _candles_after_entry(trade, candles):
    # entry_ts_utc est ISO ; on convertit en ts unix pour comparer
    from datetime import datetime
    entry_ts = datetime.fromisoformat(trade["entry_ts_utc"]).timestamp()
    return [c for c in candles if c["ts"] > entry_ts]


# ==================================================================
# BOT 1 : sortie simple -> SL (-1R) ou TP final (zone M5)
# ==================================================================
def follow_bot1(trade, candles):
    entry = trade["entry_price"]
    sl = trade["sl"]
    tp = trade["tp_final"]
    r_unit = trade["r_unit"]

    for c in _candles_after_entry(trade, candles):
        if c["low"] <= sl:
            return (False, "SL", -1.0, trade)
        if c["high"] >= tp:
            r_result = (tp - entry) / r_unit
            return (False, "TP_M5", r_result, trade)

    return (True, None, None, trade)   # toujours ouvert


# ==================================================================
# BOT 2 : 3 paliers (40% @ +5R -> BE, 30% @ +10R -> stop +5R, 30% runner -> point B)
# ==================================================================
TP1_R = 5.0
TP1_FRACTION = 0.4
TP2_R = 10.0
TP2_FRACTION = 0.3
RUNNER_FRACTION = 0.3
RUNNER_STOP_AFTER_TP2_R = 5.0


def follow_bot2(trade, candles):
    entry = trade["entry_price"]
    sl = trade["sl"]
    tp = trade["tp_final"]
    r_unit = trade["r_unit"]
    stage = trade.get("stage", 0)
    realized_r = trade.get("realized_r", 0.0)

    tp1_level = entry + TP1_R * r_unit
    tp2_level = entry + TP2_R * r_unit

    for c in _candles_after_entry(trade, candles):
        if stage == 0:
            if c["low"] <= sl:
                return (False, "SL", -1.0, trade)
            if c["high"] >= tp1_level:
                stage = 1
                realized_r += TP1_FRACTION * TP1_R
                sl = entry  # break-even
                trade["stage"], trade["realized_r"], trade["sl"] = stage, realized_r, sl
                continue
        elif stage == 1:
            if c["low"] <= sl:  # BE
                return (False, "TP1_puis_BE", realized_r, trade)
            if c["high"] >= tp2_level:
                stage = 2
                realized_r += TP2_FRACTION * TP2_R
                sl = entry + RUNNER_STOP_AFTER_TP2_R * r_unit  # stop a +5R
                trade["stage"], trade["realized_r"], trade["sl"] = stage, realized_r, sl
                continue
        elif stage == 2:
            if c["low"] <= sl:  # stop a +5R
                r_result = realized_r + RUNNER_FRACTION * RUNNER_STOP_AFTER_TP2_R
                return (False, "runner_stop_+5R", r_result, trade)
            if c["high"] >= tp:
                r_final = (tp - entry) / r_unit
                r_result = realized_r + RUNNER_FRACTION * r_final
                return (False, "TP_final_pointB", r_result, trade)

    # toujours ouvert : on renvoie le trade mis a jour (stage/realized_r peuvent avoir change)
    trade["stage"], trade["realized_r"], trade["sl"] = stage, realized_r, sl
    return (True, None, None, trade)


# ==================================================================
# BOT 2 (signal anticipe) : d'abord attendre que le prix TOUCHE l'OB
# (entree), puis appliquer les 3 paliers via follow_bot2.
# ==================================================================
# Delai max pour que le prix vienne toucher l'OB apres le signal.
# Au-dela, on considere le setup expire (ordre limite non rempli) -> annule.
PENDING_MAX_CANDLES_M5 = 288   # ~24h en M5 (288 * 5 min)


def follow_bot2_pending(trade, candles):
    extra = trade.get("extra", {})
    pending = extra.get("pending_entry", False)

    # --- Phase 1 : en attente d'entree (le prix doit toucher l'OB) ---
    if pending:
        ob_level = extra.get("ob_level", trade["entry_price"])
        after = _candles_after_entry(trade, candles)

        # expiration : trop de bougies sans remplissage -> setup annule (pas de trade)
        if len(after) > PENDING_MAX_CANDLES_M5:
            return (False, "expire_non_rempli", 0.0, trade)

        for c in after:
            if c["low"] <= ob_level:
                # ENTREE remplie : l'ordre limite est touche
                extra["pending_entry"] = False
                trade["extra"] = extra
                # on reinitialise l'horodatage d'entree a partir d'ici :
                # les paliers se comptent apres le remplissage. On stocke le ts
                # de remplissage pour que follow_bot2 ne compte que les bougies
                # posterieures a l'entree reelle.
                trade["entry_ts_utc"] = _ts_to_iso(c["ts"])
                # une meme bougie peut remplir ET declencher un palier/SL :
                # on delegue immediatement le reste a follow_bot2.
                return follow_bot2(trade, candles)
        # pas encore rempli
        trade["extra"] = extra
        return (True, None, None, trade)

    # --- Phase 2 : deja entre -> gestion normale des 3 paliers ---
    return follow_bot2(trade, candles)


def _ts_to_iso(ts_seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).isoformat()
