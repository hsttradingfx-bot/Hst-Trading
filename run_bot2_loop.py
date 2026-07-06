"""
run_bot2_loop.py - Fait tourner le BOT 2 en continu sur Railway
================================================================

Meme principe que run_bot1_loop.py, mais pour le bot 2 (bot2.py).
Appelle bot2.main() toutes les SCAN_INTERVAL_MINUTES, indefiniment,
en continuant meme si un cycle plante.
"""

import time
import traceback
from datetime import datetime, timezone

import bot2  # ton bot 2 existant (inchange)

SCAN_INTERVAL_MINUTES = 15
SCAN_INTERVAL_SECONDS = SCAN_INTERVAL_MINUTES * 60


def main():
    print(f"[run_bot2_loop] Demarrage. Scan toutes les {SCAN_INTERVAL_MINUTES} min.")
    while True:
        start = datetime.now(timezone.utc)
        print(f"\n[run_bot2_loop] === Cycle {start.strftime('%Y-%m-%d %H:%M:%S')} UTC ===")
        try:
            bot2.main()
        except Exception:
            print("[run_bot2_loop] ERREUR pendant le cycle (on continue) :")
            traceback.print_exc()

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        to_sleep = max(5, SCAN_INTERVAL_SECONDS - elapsed)
        print(f"[run_bot2_loop] Cycle fini en {elapsed:.0f}s. Pause {to_sleep:.0f}s.")
        time.sleep(to_sleep)


if __name__ == "__main__":
    main()
