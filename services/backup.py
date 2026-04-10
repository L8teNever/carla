# ==============================================================
# CARLA – Backup Service
# Erstellt vollstaendige Backups aller Docker-Stacks
# (Compose-Dateien, Env-Dateien, benannte Volumes).
# ==============================================================

import os
import json
import time
import threading
from datetime import datetime
from . import system_executor

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CONFIG_FILE = os.path.join(DATA_DIR, "backup_config.json")
LOG_FILE = os.path.join(DATA_DIR, "backup_log.json")

DEFAULT_BACKUP_DIR = "/backup/carla"

_backup_running = False
_backup_progress = {"running": False, "current_stack": "", "done": 0, "total": 0, "log": []}


# ---------------------------------------------------------------
# Config
# ---------------------------------------------------------------

def load_config() -> dict:
    defaults = {
        "backup_dir": DEFAULT_BACKUP_DIR,
        "last_run": None,
        "schedule_enabled": False,
        "schedule_time": "03:00",
        "schedule_mode": "all",
        "schedule_stacks": [],
    }
    if not os.path.exists(CONFIG_FILE):
        return defaults
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
            # Fehlende Defaults einfuegen
            for k, v in defaults.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    except Exception:
        return defaults


def save_config(cfg: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------
# Log
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
    with open(LOG_FILE, "w") as f:
        json.dump(log[-30:], f, indent=2, ensure_ascii=False)


def get_log() -> list:
    return _load_log()


def get_progress() -> dict:
    return dict(_backup_progress)


# ---------------------------------------------------------------
# Backup-Logik
# ---------------------------------------------------------------

def _get_all_stacks() -> list:
    """Holt alle Compose-Stacks mit ihrem Working Directory."""
    out = system_executor.execute_command(
        "docker ps -a --format '{{.Label \"com.docker.compose.project\"}}\\t"
        "{{.Label \"com.docker.compose.project.working_dir\"}}' | sort -u"
    )
    if not out or "Error" in out:
        return []
    stacks = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            stacks[parts[0].strip()] = parts[1].strip()
    return [{"name": k, "workdir": v} for k, v in stacks.items()]


def _get_stack_volumes(stack_name: str) -> list:
    """Holt alle benannten Volumes eines Stacks."""
    out = system_executor.execute_command(
        f"docker volume ls --filter 'label=com.docker.compose.project={stack_name}' "
        f"--format '{{{{.Name}}}}'"
    )
    if not out or "Error" in out:
        return []
    return [v.strip() for v in out.splitlines() if v.strip()]


def _get_stack_containers(stack_name: str) -> list:
    """Holt Container-Namen eines Stacks."""
    out = system_executor.execute_command(
        f"docker ps -a --filter 'label=com.docker.compose.project={stack_name}' "
        f"--format '{{{{.Names}}}}'"
    )
    if not out or "Error" in out:
        return []
    return [c.strip() for c in out.splitlines() if c.strip()]


def backup_stack(stack_name: str, workdir: str, backup_dir: str) -> dict:
    """Erstellt ein vollstaendiges Backup eines einzelnen Stacks."""
    result = {
        "stack": stack_name,
        "status": "ok",
        "details": "",
        "volumes_backed_up": [],
        "files_backed_up": []
    }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stack_backup_dir = f"{backup_dir}/{timestamp}/{stack_name}"

    # Backup-Verzeichnis erstellen
    mk_out = system_executor.execute_command(f"mkdir -p {stack_backup_dir}")
    if mk_out and "Error" in mk_out:
        result["status"] = "error"
        result["details"] = f"Konnte Backup-Verzeichnis nicht erstellen: {mk_out}"
        return result

    # 1. Compose-Dateien sichern (docker-compose.yml, .env, etc.)
    compose_files = ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", ".env"]
    for f in compose_files:
        check = system_executor.execute_command(f"test -f {workdir}/{f} && echo EXISTS")
        if "EXISTS" in (check or ""):
            cp_out = system_executor.execute_command(f"cp {workdir}/{f} {stack_backup_dir}/{f}")
            if not cp_out or "Error" not in cp_out:
                result["files_backed_up"].append(f)

    # 2. Gesamtes Workdir als Archiv sichern (fuer zusaetzliche Configs)
    tar_out = system_executor.execute_command(
        f"tar czf {stack_backup_dir}/workdir.tar.gz -C {workdir} . 2>&1"
    )
    if not tar_out or "Error" not in tar_out:
        result["files_backed_up"].append("workdir.tar.gz")

    # 3. Container-Konfigurationen sichern (inspect)
    containers = _get_stack_containers(stack_name)
    for container in containers:
        inspect_out = system_executor.execute_command(
            f"docker inspect {container}"
        )
        if inspect_out and "Error" not in inspect_out:
            # Speichere inspect output als JSON
            system_executor.execute_command(
                f"docker inspect {container} > {stack_backup_dir}/{container}_inspect.json"
            )
            result["files_backed_up"].append(f"{container}_inspect.json")

    # 4. Benannte Volumes sichern
    volumes = _get_stack_volumes(stack_name)
    for vol in volumes:
        vol_file = f"{vol}.tar.gz"
        vol_out = system_executor.execute_command(
            f"docker run --rm -v {vol}:/volume_data -v {stack_backup_dir}:/backup "
            f"alpine tar czf /backup/{vol_file} -C /volume_data . 2>&1"
        )
        if not vol_out or "Error" not in (vol_out or "").lower():
            result["volumes_backed_up"].append(vol)
        else:
            result["details"] += f"Volume {vol}: {vol_out}; "

    if not result["details"]:
        result["details"] = (
            f"{len(result['files_backed_up'])} Dateien, "
            f"{len(result['volumes_backed_up'])} Volumes gesichert"
        )

    return result


def run_backup(stacks_filter: list = None) -> list:
    """Fuehrt Backups fuer alle (oder ausgewaehlte) Stacks durch."""
    global _backup_running, _backup_progress
    if _backup_running:
        return [{"stack": "-", "status": "skip", "details": "Backup laeuft bereits"}]

    _backup_running = True
    results = []

    try:
        cfg = load_config()
        backup_dir = cfg.get("backup_dir", DEFAULT_BACKUP_DIR)
        all_stacks = _get_all_stacks()

        if stacks_filter:
            all_stacks = [s for s in all_stacks if s["name"] in stacks_filter]

        _backup_progress = {
            "running": True,
            "current_stack": "",
            "done": 0,
            "total": len(all_stacks),
            "log": []
        }

        print(f"\n[BACKUP] Starte Backup fuer {len(all_stacks)} Stacks nach {backup_dir}")

        # Backup-Verzeichnis sicherstellen
        system_executor.execute_command(f"mkdir -p {backup_dir}")

        for i, stack_info in enumerate(all_stacks):
            name = stack_info["name"]
            workdir = stack_info["workdir"]

            _backup_progress["current_stack"] = name
            _backup_progress["done"] = i

            print(f"[BACKUP] ({i+1}/{len(all_stacks)}) Sichere Stack: {name}")
            result = backup_stack(name, workdir, backup_dir)
            results.append(result)
            _backup_progress["log"].append(f"{name}: {result['status']}")
            print(f"[BACKUP]   -> {result['status']}: {result['details'][:100]}")

        _backup_progress["done"] = len(all_stacks)
        _backup_progress["current_stack"] = ""

        # Log schreiben
        log = _load_log()
        log.append({
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "backup_dir": backup_dir,
            "stacks": results
        })
        _save_log(log)

        cfg["last_run"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        save_config(cfg)

    except Exception as e:
        print(f"[BACKUP] Fehler: {e}")
        results.append({"stack": "-", "status": "error", "details": str(e)})
    finally:
        _backup_running = False
        _backup_progress["running"] = False

    return results


# ---------------------------------------------------------------
# Restore-Logik
# ---------------------------------------------------------------

def list_backups() -> list:
    """Listet alle verfuegbaren Backups."""
    cfg = load_config()
    backup_dir = cfg.get("backup_dir", DEFAULT_BACKUP_DIR)

    out = system_executor.execute_command(
        f"ls -1d {backup_dir}/*/  2>/dev/null | sort -r"
    )
    if not out or "Error" in out:
        return []

    backups = []
    for line in out.splitlines():
        dirname = line.strip().rstrip("/").split("/")[-1]
        if not dirname:
            continue
        # Stacks in diesem Backup zaehlen
        stacks_out = system_executor.execute_command(
            f"ls -1d {backup_dir}/{dirname}/*/ 2>/dev/null"
        )
        stack_names = []
        if stacks_out and "Error" not in stacks_out:
            stack_names = [s.strip().rstrip("/").split("/")[-1] for s in stacks_out.splitlines() if s.strip()]

        # Groesse ermitteln
        size_out = system_executor.execute_command(
            f"du -sh {backup_dir}/{dirname} 2>/dev/null | cut -f1"
        )
        size = size_out.strip() if size_out and "Error" not in size_out else "?"

        backups.append({
            "id": dirname,
            "stacks": stack_names,
            "size": size
        })

    return backups


def restore_stack(backup_id: str, stack_name: str) -> dict:
    """Stellt einen einzelnen Stack aus einem Backup wieder her."""
    result = {"stack": stack_name, "status": "ok", "details": ""}
    cfg = load_config()
    backup_dir = cfg.get("backup_dir", DEFAULT_BACKUP_DIR)
    stack_backup_dir = f"{backup_dir}/{backup_id}/{stack_name}"

    # Pruefen ob Backup existiert
    check = system_executor.execute_command(f"test -d {stack_backup_dir} && echo EXISTS")
    if "EXISTS" not in (check or ""):
        result["status"] = "error"
        result["details"] = f"Backup nicht gefunden: {stack_backup_dir}"
        return result

    # 1. Working Dir aus Compose-File oder inspect ermitteln
    # Versuche zuerst das workdir.tar.gz zu finden
    workdir_check = system_executor.execute_command(
        f"test -f {stack_backup_dir}/workdir.tar.gz && echo EXISTS"
    )

    # Workdir aus einem Container-Inspect ermitteln
    workdir = system_executor.execute_command(
        f"cat {stack_backup_dir}/*_inspect.json 2>/dev/null | "
        f"grep -o '\"com.docker.compose.project.working_dir\":\"[^\"]*\"' | "
        f"head -1 | cut -d'\"' -f4"
    ).strip()

    if not workdir:
        result["status"] = "error"
        result["details"] = "Konnte Working Directory nicht ermitteln"
        return result

    # 2. Working Directory wiederherstellen
    system_executor.execute_command(f"mkdir -p {workdir}")
    if "EXISTS" in (workdir_check or ""):
        system_executor.execute_command(
            f"tar xzf {stack_backup_dir}/workdir.tar.gz -C {workdir} 2>&1"
        )
        result["details"] += "Workdir wiederhergestellt. "

    # 3. Volumes wiederherstellen
    vol_files = system_executor.execute_command(
        f"ls {stack_backup_dir}/*.tar.gz 2>/dev/null | grep -v workdir.tar.gz"
    )
    volumes_restored = 0
    if vol_files and "Error" not in vol_files:
        for vol_path in vol_files.splitlines():
            vol_path = vol_path.strip()
            if not vol_path:
                continue
            vol_name = vol_path.split("/")[-1].replace(".tar.gz", "")

            # Volume erstellen falls nicht vorhanden
            system_executor.execute_command(f"docker volume create {vol_name}")

            # Daten wiederherstellen
            restore_out = system_executor.execute_command(
                f"docker run --rm -v {vol_name}:/volume_data -v {stack_backup_dir}:/backup "
                f"alpine sh -c 'rm -rf /volume_data/* && tar xzf /backup/{vol_name}.tar.gz -C /volume_data' 2>&1"
            )
            if not restore_out or "Error" not in (restore_out or "").lower():
                volumes_restored += 1

    result["details"] += f"{volumes_restored} Volumes wiederhergestellt. "

    # 4. Stack starten
    start_out = system_executor.execute_command(
        f"cd {workdir} && docker compose up -d 2>&1"
    )
    if start_out and "Error" in start_out and "error" in start_out.lower():
        result["details"] += f"Start-Fehler: {start_out[:200]}"
        result["status"] = "warning"
    else:
        result["details"] += "Stack gestartet."

    return result


def run_restore(backup_id: str, stacks: list = None) -> list:
    """Stellt ein komplettes Backup wieder her."""
    global _backup_running, _backup_progress
    if _backup_running:
        return [{"stack": "-", "status": "skip", "details": "Operation laeuft bereits"}]

    _backup_running = True
    results = []

    try:
        cfg = load_config()
        backup_dir = cfg.get("backup_dir", DEFAULT_BACKUP_DIR)

        # Verfuegbare Stacks im Backup
        if stacks is None:
            stacks_out = system_executor.execute_command(
                f"ls -1d {backup_dir}/{backup_id}/*/ 2>/dev/null"
            )
            if stacks_out and "Error" not in stacks_out:
                stacks = [s.strip().rstrip("/").split("/")[-1] for s in stacks_out.splitlines() if s.strip()]
            else:
                stacks = []

        _backup_progress = {
            "running": True,
            "current_stack": "",
            "done": 0,
            "total": len(stacks),
            "log": []
        }

        print(f"\n[BACKUP] Starte Restore von Backup {backup_id} fuer {len(stacks)} Stacks")

        for i, stack_name in enumerate(stacks):
            _backup_progress["current_stack"] = stack_name
            _backup_progress["done"] = i

            print(f"[BACKUP] ({i+1}/{len(stacks)}) Stelle wieder her: {stack_name}")
            result = restore_stack(backup_id, stack_name)
            results.append(result)
            _backup_progress["log"].append(f"{stack_name}: {result['status']}")
            print(f"[BACKUP]   -> {result['status']}: {result['details'][:100]}")

        _backup_progress["done"] = len(stacks)
        _backup_progress["current_stack"] = ""

    except Exception as e:
        print(f"[BACKUP] Fehler: {e}")
        results.append({"stack": "-", "status": "error", "details": str(e)})
    finally:
        _backup_running = False
        _backup_progress["running"] = False

    return results


# ---------------------------------------------------------------
# Scheduler Daemon (Automatische Backups)
# ---------------------------------------------------------------

_scheduler_started = False


def _backup_scheduler_loop():
    """Prueft jede Minute ob ein geplantes Backup ansteht."""
    last_triggered_date = None

    while True:
        try:
            cfg = load_config()
            if cfg.get("schedule_enabled"):
                now = datetime.now()
                target_time = cfg.get("schedule_time", "03:00")
                parts = target_time.split(":")
                target_hour = int(parts[0])
                target_minute = int(parts[1]) if len(parts) > 1 else 0

                if now.hour == target_hour and now.minute == target_minute:
                    today = now.strftime("%Y-%m-%d")
                    if last_triggered_date != today:
                        last_triggered_date = today
                        # Welche Stacks sollen gesichert werden?
                        mode = cfg.get("schedule_mode", "all")
                        selected = cfg.get("schedule_stacks", [])
                        all_stacks = _get_all_stacks()
                        all_names = [s["name"] for s in all_stacks]

                        if mode == "all":
                            stacks_filter = None
                        elif mode == "only":
                            stacks_filter = [s for s in selected if s in all_names]
                        elif mode == "except":
                            stacks_filter = [s for s in all_names if s not in selected]
                        else:
                            stacks_filter = None

                        print(f"\n[BACKUP-SCHEDULER] Geplantes Backup gestartet um {now.strftime('%H:%M')}")
                        threading.Thread(target=run_backup, args=(stacks_filter,), daemon=True).start()
        except Exception as e:
            print(f"[BACKUP-SCHEDULER] Scheduler Error: {e}")

        time.sleep(30)


def start_scheduler():
    """Startet den Backup-Scheduler-Daemon."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    thread = threading.Thread(target=_backup_scheduler_loop, daemon=True)
    thread.start()
    print("[BACKUP-SCHEDULER] Scheduler-Daemon gestartet")
    return thread
