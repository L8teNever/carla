# ==============================================================
# CARLA – Konfiguration
# Laedt Einstellungen aus dem Setup-Wizard (data/setup.json).
# Fallback auf Umgebungsvariablen fuer Rueckwaertskompatibilitaet.
# ==============================================================

import os
from services.setup import load_setup, is_setup_done, get_encryption_key

_setup = load_setup()

# --- GitHub ---
GITHUB_TOKEN = _setup.get("github_token") or os.environ.get("CARLA_GITHUB_TOKEN", "")

# --- Cloudflare ---
CF_API_TOKEN = _setup.get("cf_api_token") or os.environ.get("CARLA_CF_API_TOKEN", "")
CF_ACCOUNT_ID = _setup.get("cf_account_id") or os.environ.get("CARLA_CF_ACCOUNT_ID", "")

# --- Google Drive ---
GDRIVE_CLIENT_ID = _setup.get("gdrive_client_id") or os.environ.get("CARLA_GDRIVE_CLIENT_ID", "")
GDRIVE_CLIENT_SECRET = _setup.get("gdrive_client_secret") or os.environ.get("CARLA_GDRIVE_CLIENT_SECRET", "")
GDRIVE_REFRESH_TOKEN = _setup.get("gdrive_refresh_token") or os.environ.get("CARLA_GDRIVE_REFRESH_TOKEN", "")

# --- Verschluesselung ---
CACHE_ENCRYPTION_KEY = get_encryption_key()
CACHE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache.db")

# --- Flask Server ---
HOST = "0.0.0.0"
PORT = 8080


def reload():
    """Laedt die Konfiguration nach dem Setup neu."""
    global GITHUB_TOKEN, CF_API_TOKEN, CF_ACCOUNT_ID
    global GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN
    _s = load_setup()
    GITHUB_TOKEN = _s.get("github_token") or os.environ.get("CARLA_GITHUB_TOKEN", "")
    CF_API_TOKEN = _s.get("cf_api_token") or os.environ.get("CARLA_CF_API_TOKEN", "")
    CF_ACCOUNT_ID = _s.get("cf_account_id") or os.environ.get("CARLA_CF_ACCOUNT_ID", "")
    GDRIVE_CLIENT_ID = _s.get("gdrive_client_id") or os.environ.get("CARLA_GDRIVE_CLIENT_ID", "")
    GDRIVE_CLIENT_SECRET = _s.get("gdrive_client_secret") or os.environ.get("CARLA_GDRIVE_CLIENT_SECRET", "")
    GDRIVE_REFRESH_TOKEN = _s.get("gdrive_refresh_token") or os.environ.get("CARLA_GDRIVE_REFRESH_TOKEN", "")
