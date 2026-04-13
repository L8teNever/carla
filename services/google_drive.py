# ==============================================================
# CARLA – Google Drive Backup Service
# Upload, Download und Verwaltung von Backups auf Google Drive.
# Verwendet OAuth2 mit Client ID, Client Secret und Refresh Token.
# ==============================================================

import os
import io
import json
import shutil
import tempfile
import threading
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER_NAME = "CARLA-Backups"

_folder_id_cache = None

# Progress-Tracking (gleiche Struktur wie backup.py)
_gdrive_progress = {
    "running": False,
    "operation": "",      # "upload" oder "download"
    "backup_id": "",
    "bytes_done": 0,
    "bytes_total": 0,
    "percent": 0
}


def get_progress() -> dict:
    return dict(_gdrive_progress)


def _reset_progress():
    _gdrive_progress.update({
        "running": False,
        "operation": "",
        "backup_id": "",
        "bytes_done": 0,
        "bytes_total": 0,
        "percent": 0
    })


# ---------------------------------------------------------------
# Auth & Service
# ---------------------------------------------------------------

def build_drive_service(client_id: str, client_secret: str, refresh_token: str):
    """Erstellt einen Google Drive API v3 Service."""
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def test_connection(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Testet die Verbindung und gibt Nutzer-Info + Speicher zurueck."""
    try:
        service = build_drive_service(client_id, client_secret, refresh_token)
        about = service.about().get(fields="user,storageQuota").execute()
        user = about.get("user", {})
        quota = about.get("storageQuota", {})
        return {
            "ok": True,
            "email": user.get("emailAddress", ""),
            "display_name": user.get("displayName", ""),
            "storage_used": int(quota.get("usage", 0)),
            "storage_total": int(quota.get("limit", 0)),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------
# Folder Management
# ---------------------------------------------------------------

def _ensure_folder(service) -> str:
    """Findet oder erstellt den CARLA-Backups Ordner. Cached die ID."""
    global _folder_id_cache
    if _folder_id_cache:
        # Pruefen ob er noch existiert
        try:
            f = service.files().get(fileId=_folder_id_cache, fields="id,trashed").execute()
            if not f.get("trashed"):
                return _folder_id_cache
        except Exception:
            pass
        _folder_id_cache = None

    # Suchen
    q = f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = results.get("files", [])
    if files:
        _folder_id_cache = files[0]["id"]
        return _folder_id_cache

    # Erstellen
    meta = {
        "name": FOLDER_NAME,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = service.files().create(body=meta, fields="id").execute()
    _folder_id_cache = folder["id"]
    return _folder_id_cache


# ---------------------------------------------------------------
# Upload
# ---------------------------------------------------------------

def upload_backup(client_id: str, client_secret: str, refresh_token: str,
                  backup_dir: str, backup_id: str) -> dict:
    """
    Packt ein Backup-Verzeichnis als tar.gz und laedt es auf Google Drive hoch.
    backup_dir: Basis-Verzeichnis (z.B. /backup/carla)
    backup_id: Zeitstempel-Ordner (z.B. 20240101_030000)
    """
    source_path = os.path.join(backup_dir, backup_id)
    if not os.path.isdir(source_path):
        return {"ok": False, "error": f"Backup-Verzeichnis nicht gefunden: {source_path}"}

    _gdrive_progress.update({
        "running": True,
        "operation": "upload",
        "backup_id": backup_id,
        "bytes_done": 0,
        "bytes_total": 0,
        "percent": 0,
    })

    tmp_file = None
    try:
        service = build_drive_service(client_id, client_secret, refresh_token)
        folder_id = _ensure_folder(service)

        # Tar.gz erstellen
        tmp_dir = tempfile.mkdtemp()
        archive_name = f"{backup_id}"
        archive_path = shutil.make_archive(
            os.path.join(tmp_dir, archive_name), "gztar", backup_dir, backup_id
        )
        tmp_file = archive_path
        file_size = os.path.getsize(archive_path)
        _gdrive_progress["bytes_total"] = file_size

        # Upload mit Resumable
        file_meta = {
            "name": f"{backup_id}.tar.gz",
            "parents": [folder_id],
            "description": f"CARLA Backup {backup_id}",
        }
        media = MediaFileUpload(
            archive_path,
            mimetype="application/gzip",
            resumable=True,
            chunksize=10 * 1024 * 1024,  # 10 MB Chunks
        )
        req = service.files().create(body=file_meta, media_body=media, fields="id,name,size")

        response = None
        while response is None:
            status, response = req.next_chunk()
            if status:
                _gdrive_progress["bytes_done"] = int(status.resumable_progress)
                _gdrive_progress["percent"] = int(status.progress() * 100)

        _gdrive_progress["bytes_done"] = file_size
        _gdrive_progress["percent"] = 100

        return {
            "ok": True,
            "file_id": response["id"],
            "name": response["name"],
            "size": file_size,
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _reset_progress()
        # Temp-Dateien aufraeumen
        if tmp_file and os.path.exists(tmp_file):
            try:
                shutil.rmtree(os.path.dirname(tmp_file))
            except Exception:
                pass


# ---------------------------------------------------------------
# List
# ---------------------------------------------------------------

def list_backups(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Listet alle Backups im CARLA-Backups Ordner auf."""
    try:
        service = build_drive_service(client_id, client_secret, refresh_token)
        folder_id = _ensure_folder(service)

        q = f"'{folder_id}' in parents and trashed=false"
        results = service.files().list(
            q=q,
            fields="files(id,name,size,createdTime)",
            orderBy="createdTime desc",
            pageSize=100,
        ).execute()

        backups = []
        for f in results.get("files", []):
            backups.append({
                "id": f["id"],
                "name": f["name"],
                "size": int(f.get("size", 0)),
                "created": f.get("createdTime", ""),
            })

        return {"ok": True, "backups": backups}
    except Exception as e:
        return {"ok": False, "error": str(e), "backups": []}


# ---------------------------------------------------------------
# Download
# ---------------------------------------------------------------

def download_backup(client_id: str, client_secret: str, refresh_token: str,
                    file_id: str, dest_dir: str) -> dict:
    """
    Laedt ein Backup von Google Drive herunter und entpackt es.
    dest_dir: Zielverzeichnis (z.B. /backup/carla)
    Gibt backup_id zurueck (extrahiert aus Dateiname).
    """
    _gdrive_progress.update({
        "running": True,
        "operation": "download",
        "backup_id": "",
        "bytes_done": 0,
        "bytes_total": 0,
        "percent": 0,
    })

    tmp_file = None
    try:
        service = build_drive_service(client_id, client_secret, refresh_token)

        # Dateiinfo holen
        file_info = service.files().get(fileId=file_id, fields="name,size").execute()
        file_name = file_info["name"]
        file_size = int(file_info.get("size", 0))
        backup_id = file_name.replace(".tar.gz", "")

        _gdrive_progress["backup_id"] = backup_id
        _gdrive_progress["bytes_total"] = file_size

        # Download
        os.makedirs(dest_dir, exist_ok=True)
        tmp_file = os.path.join(dest_dir, file_name)
        request = service.files().get_media(fileId=file_id)
        with open(tmp_file, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=10 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    _gdrive_progress["bytes_done"] = int(status.resumable_progress)
                    _gdrive_progress["percent"] = int(status.progress() * 100)

        _gdrive_progress["bytes_done"] = file_size
        _gdrive_progress["percent"] = 100

        # Entpacken
        shutil.unpack_archive(tmp_file, dest_dir)

        return {"ok": True, "backup_id": backup_id, "path": os.path.join(dest_dir, backup_id)}

    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _reset_progress()
        # Tar.gz nach Entpacken loeschen
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception:
                pass


# ---------------------------------------------------------------
# Delete
# ---------------------------------------------------------------

def delete_backup(client_id: str, client_secret: str, refresh_token: str,
                  file_id: str) -> dict:
    """Loescht ein Backup von Google Drive."""
    try:
        service = build_drive_service(client_id, client_secret, refresh_token)
        service.files().delete(fileId=file_id).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
