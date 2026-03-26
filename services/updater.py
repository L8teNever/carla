# ==============================================================
# CARLA – Auto-Updater Service
# Prueft Docker-Images auf Updates und aktualisiert Stacks
# zu einer konfigurierten Uhrzeit.
# ==============================================================

import os
import json
import time
import threading
from datetime import datetime, timedelta
from . import system_executor

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CONFIG_FILE = os.path.join(DATA_DIR, "update_config.json")
LOG_FILE = os.path.join(DATA_DIR, "update_log.json")

_daemon_started = False
_update_running = False


# ---------------------------------------------------------------
# Config lesen/schreiben
# ---------------------------------------------------------------

def load_config() -> dict:
    """Laedt die Update-Konfiguration."""
    if not os.path.exists(CONFIG_FILE):
        return {"enabled": False, "time": "04:00", "mode": "all", "stacks": [], "last_run": None}
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"enabled": False, "time": "04:00", "mode": "all", "stacks": [], "last_run": None}


def save_config(cfg: dict) -> None:
    """Speichert die Update-Konfiguration."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------
# Update Log
# ---------------------------------------------------------------

def _load_log() -> list:
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_log(log: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    # Maximal 50 Eintraege behalten
    with open(LOG_FILE, "w") as f:
        json.dump(log[-50:], f, indent=2, ensure_ascii=False)


def get_log() -> list:
    return _load_log()


# ---------------------------------------------------------------
# Stack-Liste vom Server holen
# ---------------------------------------------------------------

def get_available_stacks() -> list:
    """Holt alle laufenden Compose-Stacks."""
    out = system_executor.execute_command(
        "docker ps --format '{{.Label \"com.docker.compose.project\"}}' | sort -u"
    )
    if not out or "Error" in out:
        return []
    return [s.strip() for s in out.splitlines() if s.strip()]


# ---------------------------------------------------------------
# Update-Logik
# ---------------------------------------------------------------

def check_image_update(image: str) -> bool:
    """Prueft ob ein neueres Image verfuegbar ist (vergleicht Digests)."""
    # Lokalen Digest holen
    local = system_executor.execute_command(
        f"docker inspect --format='{{{{.Id}}}}' {image} 2>/dev/null"
    )
    # Pull und neuen Digest vergleichen
    pull_out = system_executor.execute_command(f"docker pull {image} 2>&1")
    if "Error" in pull_out or "error" in pull_out.lower():
        return False
    new = system_executor.execute_command(
        f"docker inspect --format='{{{{.Id}}}}' {image} 2>/dev/null"
    )
    return local != new and bool(new)


def update_stack(stack_name: str) -> dict:
    """Aktualisiert einen einzelnen Compose-Stack."""
    result = {"stack": stack_name, "status": "ok", "details": "", "updated_containers": []}

    # Working Dir des Stacks finden
    workdir = system_executor.execute_command(
        f"docker ps --filter 'label=com.docker.compose.project={stack_name}' "
        f"--format '{{{{.Label \"com.docker.compose.project.working_dir\"}}}}' | head -1"
    )
    if not workdir or "Error" in workdir:
        result["status"] = "error"
        result["details"] = f"Working Dir nicht gefunden fuer Stack '{stack_name}'"
        return result

    # Images des Stacks pruefen
    images_out = system_executor.execute_command(
        f"docker ps --filter 'label=com.docker.compose.project={stack_name}' "
        f"--format '{{{{.Image}}}}|{{{{.Names}}}}'"
    )
    updated = []
    for line in images_out.splitlines():
        parts = line.split("|")
        if len(parts) >= 2:
            img, name = parts[0], parts[1]
            if check_image_update(img):
                updated.append(name)

    if not updated:
        result["details"] = "Keine Updates verfuegbar"
        return result

    # Stack neu starten mit neuen Images
    compose_cmd = f"cd {workdir} && docker compose up -d --remove-orphans 2>&1"
    output = system_executor.execute_command(compose_cmd)
    result["updated_containers"] = updated
    result["details"] = output
    return result


def run_update(stacks_to_update: list = None) -> list:
    """Fuehrt Updates fuer die angegebenen Stacks durch."""
    global _update_running
    if _update_running:
        return [{"stack": "-", "status": "skip", "details": "Update laeuft bereits"}]

    _update_running = True
    results = []

    try:
        cfg = load_config()
        all_stacks = get_available_stacks()

        if stacks_to_update is None:
            mode = cfg.get("mode", "all")
            selected = cfg.get("stacks", [])

            if mode == "all":
                stacks_to_update = all_stacks
            elif mode == "only":
                stacks_to_update = [s for s in selected if s in all_stacks]
            elif mode == "except":
                stacks_to_update = [s for s in all_stacks if s not in selected]
            else:
                stacks_to_update = all_stacks

        print(f"\n[UPDATER] Starte Update fuer {len(stacks_to_update)} Stacks: {stacks_to_update}")

        for stack in stacks_to_update:
            print(f"[UPDATER] Aktualisiere Stack: {stack}")
            result = update_stack(stack)
            results.append(result)
            print(f"[UPDATER]   -> {result['status']}: {result['details'][:100]}")

        # Log schreiben
        log = _load_log()
        log.append({
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "stacks": results,
            "trigger": "manual" if stacks_to_update else "scheduled"
        })
        _save_log(log)

        # last_run aktualisieren
        cfg["last_run"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        save_config(cfg)

    except Exception as e:
        print(f"[UPDATER] Fehler: {e}")
        results.append({"stack": "-", "status": "error", "details": str(e)})
    finally:
        _update_running = False

    return results


# ---------------------------------------------------------------
# Scheduler Daemon
# ---------------------------------------------------------------

def _scheduler_loop():
    """Prueft jede Minute ob ein geplantes Update ansteht."""
    last_triggered_date = None

    while True:
        try:
            cfg = load_config()
            if cfg.get("enabled"):
                now = datetime.now()
                target_time = cfg.get("time", "04:00")
                parts = target_time.split(":")
                target_hour = int(parts[0])
                target_minute = int(parts[1]) if len(parts) > 1 else 0

                if now.hour == target_hour and now.minute == target_minute:
                    today = now.strftime("%Y-%m-%d")
                    if last_triggered_date != today:
                        last_triggered_date = today
                        print(f"\n[UPDATER] Geplantes Update gestartet um {now.strftime('%H:%M')}")
                        threading.Thread(target=run_update, daemon=True).start()
        except Exception as e:
            print(f"[UPDATER] Scheduler Error: {e}")

        time.sleep(30)


def start_daemon():
    """Startet den Scheduler-Daemon."""
    global _daemon_started
    if _daemon_started:
        return
    _daemon_started = True
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()
    print("[UPDATER] Scheduler-Daemon gestartet")
    return thread
