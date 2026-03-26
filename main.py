# ==============================================================
# CARLA – Einstiegspunkt
# Startet den Flask-Webserver sofort, fragt parallel Daten ab.
# ==============================================================

from installer import check_and_install_dependencies
check_and_install_dependencies()

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from flask import Flask
from routes import bp, start_background_fetch
from services import cache, metrics_worker
import config

app = Flask(__name__)
app.register_blueprint(bp)

print("\n" + "="*60)
print("🚀 [CARLA] Server gestartet!")
print("🌐 [CARLA] Webinterface ist SOFORT erreichbar.")

# Starte Worker für historische System-Metriken logging
metrics_worker.start_daemon()

# Starte asynchrone Erst-Abfrage, falls noch kein lokaler SQL-Cache existiert.
if not cache.has_entry("full_infrastructure"):
    print("📡 [CARLA] Kein Cache vorhanden. Starte ersten Download parallel im Hintergrund...")
    start_background_fetch()
else:
    print("⏭️ [CARLA] SQL-Cache vorhanden – Ladevorgänge sind blitzschnell.")

print("="*60 + "\n")

if __name__ == "__main__":
    print(f"[CARLA] Dashboard: http://localhost:{config.PORT}")
    app.run(host=config.HOST, port=config.PORT)