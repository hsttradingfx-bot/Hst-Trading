"""
trade_journal.py - Journal automatique des trades (partage par bot 1 et bot 2)
==============================================================================

Role :
  - Enregistrer chaque signal comme un trade OUVERT (persiste entre les
    executions GitHub Actions via un fichier JSON commite dans le repo).
  - A chaque execution, re-verifier les trades ouverts contre les prix
    recents et cloturer ceux dont le SL / TP (ou palier) est atteint.
  - Ecrire chaque trade CLOTURE dans un CSV lisible (ouvrable dans Excel/
    Sheets) pour analyse et comparaison avec le backtest.

Chaque bot passe SA PROPRE fonction de suivi (logique de sortie differente) :
  - Bot 1 : SL simple (-1R) ou TP = zone M5 -> R calcule directement.
  - Bot 2 : 3 paliers (40% a +5R avec BE, 30% a +10R avec stop +5R,
            30% runner jusqu'au point B).

Format des fichiers (dans le repo) :
  - open_trades.json  : liste des trades encore ouverts (etat persistant)
  - trades_journal.csv: historique de tous les trades cloturuis

Conception volontairement simple et sans dependance externe (json + csv std).
"""

import os
import json
import csv
from datetime import datetime, timezone

# Emplacement des fichiers (a cote du script, donc a la racine du repo)
_BASE = os.path.dirname(os.path.abspath(__file__))
OPEN_TRADES_FILE = os.path.join(_BASE, "open_trades.json")
JOURNAL_CSV = os.path.join(_BASE, "trades_journal.csv")
SEEN_KEYS_FILE = os.path.join(_BASE, "seen_keys.json")

CSV_HEADER = [
    "bot", "symbol", "direction",
    "entry_ts_utc", "entry_price", "sl", "tp_final",
    "r_unit", "exit_ts_utc", "exit_reason", "r_result",
]


# ------------------------------------------------------------------
# Lecture / ecriture de l'etat des trades ouverts
# ------------------------------------------------------------------
def load_open_trades():
    if os.path.exists(OPEN_TRADES_FILE):
        try:
            with open(OPEN_TRADES_FILE, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
    return []


def save_open_trades(open_trades):
    try:
        with open(OPEN_TRADES_FILE, "w") as f:
            json.dump(open_trades, f, indent=2)
    except OSError as e:
        print(f"[journal][WARN] ecriture open_trades impossible: {e}")


def _load_seen_keys():
    if os.path.exists(SEEN_KEYS_FILE):
        try:
            with open(SEEN_KEYS_FILE, "r") as f:
                return set(json.load(f))
        except (OSError, json.JSONDecodeError):
            return set()
    return set()


def _save_seen_keys(seen):
    try:
        with open(SEEN_KEYS_FILE, "w") as f:
            json.dump(sorted(seen), f, indent=2)
    except OSError as e:
        print(f"[journal][WARN] ecriture seen_keys impossible: {e}")


# ------------------------------------------------------------------
# Ecriture d'un trade cloture dans le CSV
# ------------------------------------------------------------------
def append_closed_trade(row):
    file_exists = os.path.exists(JOURNAL_CSV)
    try:
        with open(JOURNAL_CSV, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except OSError as e:
        print(f"[journal][WARN] ecriture CSV impossible: {e}")


# ------------------------------------------------------------------
# Enregistrer un nouveau signal comme trade ouvert
# ------------------------------------------------------------------
def register_signal(bot, symbol, direction, entry_price, sl, tp_final,
                    r_unit, extra=None, dedup_key=None):
    """
    Ajoute un trade ouvert s'il n'existe pas deja (anti-doublon via dedup_key).
    'extra' : dict libre pour stocker des infos propres au bot (ex: paliers).
    Retourne True si un nouveau trade a ete enregistre, False si doublon.

    Anti-doublon renforce : on garde une trace des dedup_key deja vues (ouvertes
    OU cloturees) dans un petit fichier seen_keys.json, pour ne jamais
    re-enregistrer deux fois le meme setup, meme apres cloture.
    """
    seen = _load_seen_keys()
    if dedup_key is not None and dedup_key in seen:
        return False

    open_trades = load_open_trades()
    trade = {
        "bot": bot,
        "symbol": symbol,
        "direction": direction,
        "entry_ts_utc": datetime.now(timezone.utc).isoformat(),
        "entry_price": entry_price,
        "sl": sl,
        "tp_final": tp_final,
        "r_unit": r_unit,
        "stage": 0,
        "realized_r": 0.0,
        "extra": extra or {},
        "dedup_key": dedup_key,
    }
    open_trades.append(trade)
    save_open_trades(open_trades)

    if dedup_key is not None:
        seen.add(dedup_key)
        _save_seen_keys(seen)
    return True


# ------------------------------------------------------------------
# Cloturer un trade : ecrit dans le CSV et le retire des ouverts
# ------------------------------------------------------------------
def _close_trade(trade, exit_reason, r_result):
    row = {
        "bot": trade["bot"],
        "symbol": trade["symbol"],
        "direction": trade["direction"],
        "entry_ts_utc": trade["entry_ts_utc"],
        "entry_price": round(trade["entry_price"], 6),
        "sl": round(trade["sl"], 6),
        "tp_final": round(trade["tp_final"], 6),
        "r_unit": round(trade["r_unit"], 6),
        "exit_ts_utc": datetime.now(timezone.utc).isoformat(),
        "exit_reason": exit_reason,
        "r_result": round(r_result, 4),
    }
    append_closed_trade(row)


# ------------------------------------------------------------------
# Suivi : re-evalue tous les trades ouverts d'un bot donne
# ------------------------------------------------------------------
def update_open_trades(bot, price_data_by_symbol, follow_fn):
    """
    bot                 : "bot1" ou "bot2" (ne suit que les trades de ce bot)
    price_data_by_symbol: dict {symbol: candles_recentes} pour verifier SL/TP
    follow_fn           : fonction(trade, candles) -> (still_open, exit_reason, r_result, updated_trade)
                          Chaque bot fournit sa propre logique de sortie.
    """
    open_trades = load_open_trades()
    still_open = []

    for trade in open_trades:
        if trade["bot"] != bot:
            still_open.append(trade)      # pas a nous, on garde tel quel
            continue

        candles = price_data_by_symbol.get(trade["symbol"])
        if not candles:
            still_open.append(trade)      # pas de data -> on garde ouvert
            continue

        keep_open, exit_reason, r_result, updated = follow_fn(trade, candles)
        if keep_open:
            still_open.append(updated)
        else:
            _close_trade(updated, exit_reason, r_result)
            print(f"[journal] {bot} {trade['symbol']} CLOTURE {exit_reason} -> {r_result:+.2f}R")

    save_open_trades(still_open)
