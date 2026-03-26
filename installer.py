# ==============================================================
# CARLA – Auto-Installer
# Installiert fehlende Abhängigkeiten automatisch.
# ==============================================================

import subprocess
import sys
import importlib.util


REQUIRED_PACKAGES = [
    "flask",
    "requests",
    "paramiko",
    "urllib3",
    "websocket-client",
    "python-socketio[client]",
    "cryptography",
]


def check_and_install_dependencies():
    for package in REQUIRED_PACKAGES:
        dist_name = package.split('[')[0]
        spec = importlib.util.find_spec(dist_name.replace('-', '_'))
        if spec is None:
            print(f"[*] Paket '{package}' fehlt. Installiere...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
            except Exception as e:
                print(f"[!] Fehler bei der Installation von {package}: {e}")
