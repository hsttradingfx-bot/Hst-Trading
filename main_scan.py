"""
main_scan.py - UN scan des deux bots, puis on s'arrete.
========================================================

Concu pour le CRON SCHEDULE de Railway (econome en credit) :
Railway lance ce fichier toutes les 15 min, il fait un seul passage des deux
bots (bot 1 + bot 2), puis se termine. Entre deux passages, rien ne tourne,
donc la consommation de credit est minimale (quelques secondes toutes les
15 min au lieu de 24h/24).

Contrairement aux fichiers run_botX_loop.py (boucle infinie), ici PAS de
boucle : un seul scan, puis sortie. C'est le Cron de Railway qui relance.

Les deux bots sont appeles l'un apres l'autre, chacun dans un try/except pour
que si l'un plante, l'autre tourne quand meme.
"""

import traceback
from datetime import datetime, timezone

print(f"\n=== main_scan {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC ===")

# --- BOT 1 ---
try:
    import live_bot_mexc
    print("[main_scan] Lancement bot 1...")
    live_bot_mexc.check_all_symbols()
    print("[main_scan] Bot 1 termine.")
except Exception:
    print("[main_scan] ERREUR bot 1 (on continue avec le bot 2) :")
    traceback.print_exc()

# --- BOT 2 ---
try:
    import bot2
    print("[main_scan] Lancement bot 2...")
    bot2.main()
    print("[main_scan] Bot 2 termine.")
except Exception:
    print("[main_scan] ERREUR bot 2 :")
    traceback.print_exc()

print("[main_scan] Scan complet termine. A dans 15 min (via le Cron Railway).")
