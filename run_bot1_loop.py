"""
run_bot1_loop.py - Fait tourner le BOT 1 en continu sur Railway
================================================================

Railway garde un programme vivant tant qu'il ne se termine pas. Contrairement
a GitHub Actions (qui relance le script via cron), ici c'est A NOUS de boucler.

Ce fichier appelle la logique existante du bot 1 (live_bot_mexc.py) toutes les
SCAN_INTERVAL_MINUTES, indefiniment. Il ne modifie pas le bot : il l'importe
et appelle sa fonction principale en boucle.

Robustesse : si un cycle plante (reseau, API MEXC down...), on logge l'erreur
et on continue au cycle suivant au lieu de tout arreter.
"""

import time
import traceback
from datetime import datetime, timezone

import live_bot_mexc  # ton bot 1 existant (inchange)

SCAN_INTERVAL_MINUTES = 15
SCAN_INTERVAL_SECONDS = SCAN_INTERVAL_MINUTES * 60


def main():
    print(f"[run_bot1_loop] Demarrage. Scan toutes les {SCAN_INTERVAL_MINUTES} min.")
    while True:
        start = datetime.now(timezone.utc)
        print(f"\n[run_bot1_loop] === Cycle {start.strftime('%Y-%m-%d %H:%M:%S')} UTC ===")
        try:
            live_bot_mexc.check_all_symbols()
        except Exception:
            print("[run_bot1_loop] ERREUR pendant le cycle (on continue) :")
            traceback.print_exc()

        # On dort jusqu'au prochain scan, en tenant compte du temps deja passe
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        to_sleep = max(5, SCAN_INTERVAL_SECONDS - elapsed)
        print(f"[run_bot1_loop] Cycle fini en {elapsed:.0f}s. Pause {to_sleep:.0f}s.")
        time.sleep(to_sleep)


if __name__ == "__main__":
    main()
