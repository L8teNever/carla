# ==============================================================
# CARLA – Setup Service
# Verwaltet die Ersteinrichtung über eine verschlüsselte JSON-Datei.
# Bei erstem Start wird ein Setup-Wizard im Browser angezeigt.
# ==============================================================

import os
import json
from cryptography.fernet import Fernet

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
SETUP_FILE = os.path.join(DATA_DIR, "setup.json")
KEY_FILE = os.path.join(DATA_DIR, ".encryption_key")


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _get_or_create_key() -> bytes:
    """Liest oder erzeugt den Fernet-Schlüssel."""
    _ensure_data_dir()
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    return key


def get_encryption_key() -> str:
    """Gibt den Encryption Key als String zurück (für config/cache)."""
    return _get_or_create_key().decode()


def is_setup_done() -> bool:
    """Prüft ob die Ersteinrichtung abgeschlossen ist."""
    return os.path.exists(SETUP_FILE)


def save_setup(data: dict) -> None:
    """Speichert die Setup-Daten verschlüsselt."""
    _ensure_data_dir()
    f = Fernet(_get_or_create_key())
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    encrypted = f.encrypt(raw)
    with open(SETUP_FILE, "wb") as fp:
        fp.write(encrypted)


def load_setup() -> dict:
    """Lädt die Setup-Daten. Gibt leeres Dict zurück wenn nicht vorhanden."""
    if not os.path.exists(SETUP_FILE):
        return {}
    try:
        f = Fernet(_get_or_create_key())
        with open(SETUP_FILE, "rb") as fp:
            encrypted = fp.read()
        raw = f.decrypt(encrypted)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"[Setup] Fehler beim Laden: {e}")
        return {}


def delete_setup() -> None:
    """Löscht die Setup-Daten (für Reset)."""
    if os.path.exists(SETUP_FILE):
        os.remove(SETUP_FILE)
