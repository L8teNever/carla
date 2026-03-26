# ==============================================================
# CARLA – Cache Service
# Speichert Infrastruktur-Daten verschlüsselt in einer SQLite-DB.
# Verschlüsselung: Fernet (AES-128 CBC + HMAC-SHA256)
# ==============================================================

import sqlite3
import json
import os
from datetime import datetime
from cryptography.fernet import Fernet, InvalidToken
import config


def _get_fernet() -> Fernet:
    key = config.CACHE_ENCRYPTION_KEY.encode()
    return Fernet(key)


def _get_connection() -> sqlite3.Connection:
    """Öffnet eine SQLite-Verbindung, legt die DB-Datei bei Bedarf an."""
    os.makedirs(os.path.dirname(config.CACHE_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.CACHE_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key     TEXT PRIMARY KEY,
            payload BLOB NOT NULL,
            updated TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------

def save(key: str, data: dict) -> None:
    """Verschlüsselt `data` und speichert es unter `key` in der DB."""
    f = _get_fernet()
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    encrypted = f.encrypt(raw)
    updated = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = _get_connection()
    conn.execute(
        "INSERT INTO cache (key, payload, updated) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated=excluded.updated",
        (key, encrypted, updated),
    )
    conn.commit()
    conn.close()


def load(key: str) -> tuple[dict | None, str | None]:
    """
    Lädt und entschlüsselt den Eintrag unter `key`.
    Gibt (data, timestamp) zurück, oder (None, None) wenn kein Eintrag.
    """
    conn = _get_connection()
    row = conn.execute(
        "SELECT payload, updated FROM cache WHERE key = ?", (key,)
    ).fetchone()
    conn.close()

    if row is None:
        return None, None

    try:
        f = _get_fernet()
        raw = f.decrypt(row[0])
        return json.loads(raw.decode("utf-8")), row[1]
    except (InvalidToken, Exception) as e:
        print(f"[Cache] Entschlüsselung fehlgeschlagen: {e}")
        return None, None


def has_entry(key: str) -> bool:
    """Prüft, ob ein Eintrag unter `key` existiert."""
    conn = _get_connection()
    row = conn.execute(
        "SELECT 1 FROM cache WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row is not None


def clear(key: str) -> None:
    """Löscht den Eintrag unter `key`."""
    conn = _get_connection()
    conn.execute("DELETE FROM cache WHERE key = ?", (key,))
    conn.commit()
    conn.close()
