# ==============================================================
# CARLA – Einstiegspunkt
# Startet den Flask-Webserver sofort, fragt parallel Daten ab.
# ==============================================================

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from flask import Flask
from routes import bp, start_background_fetch
from services import cache, metrics_worker, setup, backup
import config

app = Flask(__name__)
app.register_blueprint(bp)

print("\n" + "="*60)
print("[CARLA] Server gestartet!")
print("[CARLA] Webinterface ist SOFORT erreichbar.")

if setup.is_setup_done():
    # Setup ist abgeschlossen – normal starten
    metrics_worker.start_daemon()
    from services import updater
    updater.start_daemon()
    backup.start_scheduler()
    if not cache.has_entry("full_infrastructure"):
        print("[CARLA] Kein Cache vorhanden. Starte ersten Download parallel im Hintergrund...")
        start_background_fetch()
    else:
        print("[CARLA] SQL-Cache vorhanden – Ladevorgaenge sind blitzschnell.")
else:
    print("[CARLA] Kein Setup gefunden – Setup-Wizard wird im Browser angezeigt.")

print("="*60 + "\n")

if __name__ == "__main__":
    print(f"[CARLA] Dashboard: http://localhost:{config.PORT}")
    app.run(host=config.HOST, port=config.PORT)