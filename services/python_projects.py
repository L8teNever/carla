# ==============================================================
# CARLA – Python Projects Service
# Eigenstaendige Python-Projekte mit eigener venv, Dateistruktur,
# Paketverwaltung, Script-Ausfuehrung und Konsole.
#
# Layout je Projekt:
#   /opt/stacks/carla-python/<name>/
#       src/        <- Projektdateien (hier arbeitet der User)
#       venv/       <- eigene virtuelle Umgebung
#       logs/       <- Ausgabe laufender Scripts / pip
#       meta.json   <- Einstellungen (Entry-Script, Autostart)
#       bashrc      <- Rcfile fuer die Konsole (aktiviert venv)
# ==============================================================

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from services import static_server

BASE_DIR = "/opt/stacks/carla-python"
IS_WIN = sys.platform == "win32"
MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_LOG_BYTES = 200 * 1024
HIDDEN_DIRS = {"__pycache__", ".git"}

_RUNS = {}          # (project, key) -> {"proc", "started", "cmd", "log", "exit"}
_RUNS_LOCK = threading.Lock()


# ---------------------------------------------------------------
# Pfade & Hilfsfunktionen
# ---------------------------------------------------------------

def normalize_name(name: str) -> str:
    return static_server.normalize_name(name or "")


def _pdir(name: str) -> str:
    return os.path.join(BASE_DIR, name)


def _src(name: str) -> str:
    return os.path.join(_pdir(name), "src")


def _venv(name: str) -> str:
    return os.path.join(_pdir(name), "venv")


def _venv_bin(name: str, exe: str) -> str:
    return os.path.join(_venv(name), "Scripts" if IS_WIN else "bin", exe + (".exe" if IS_WIN else ""))


def _logs(name: str) -> str:
    return os.path.join(_pdir(name), "logs")


def _exists(name: str) -> bool:
    return bool(name) and os.path.isdir(_src(name))


def _safe(name: str, rel: str) -> str | None:
    """Loest einen relativen Pfad innerhalb von src/ auf (None bei Ausbruch)."""
    root = os.path.realpath(_src(name))
    target = os.path.realpath(os.path.join(root, (rel or "").lstrip("/\\")))
    if target == root or target.startswith(root + os.sep):
        return target
    return None


def _load_meta(name: str) -> dict:
    try:
        with open(os.path.join(_pdir(name), "meta.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta(name: str, meta: dict):
    with open(os.path.join(_pdir(name), "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def _env(name: str) -> dict:
    env = dict(os.environ)
    venv = _venv(name)
    env["VIRTUAL_ENV"] = venv
    env["PATH"] = os.path.dirname(_venv_bin(name, "python")) + os.pathsep + env.get("PATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONHOME", None)
    return env


def _log_path(name: str, key: str) -> str:
    return os.path.join(_logs(name), re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".log")


# ---------------------------------------------------------------
# Prozess-/Job-Verwaltung (Scripts & pip)
# ---------------------------------------------------------------

def _is_running(run: dict) -> bool:
    return run is not None and run["proc"].poll() is None


def _start_job(name: str, key: str, cmd: list, label: str) -> dict:
    with _RUNS_LOCK:
        if _is_running(_RUNS.get((name, key))):
            return {"ok": False, "error": "Laeuft bereits."}
        os.makedirs(_logs(name), exist_ok=True)
        log = _log_path(name, key)
        logf = open(log, "wb")
        logf.write(f"$ {label}\n".encode())
        logf.flush()
        try:
            proc = subprocess.Popen(
                cmd, cwd=_src(name), env=_env(name), stdin=subprocess.DEVNULL,
                stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=not IS_WIN,
            )
        except Exception as e:
            logf.close()
            return {"ok": False, "error": f"Start fehlgeschlagen: {e}"}
        run = {"proc": proc, "started": time.time(), "cmd": label, "log": log}
        _RUNS[(name, key)] = run

    def _wait():
        code = proc.wait()
        run["exit"] = code
        try:
            logf.write(f"\n[CARLA] Prozess beendet (Exit-Code {code})\n".encode())
            logf.close()
        except Exception:
            pass

    threading.Thread(target=_wait, daemon=True).start()
    return {"ok": True, "pid": proc.pid}


def _stop_job(name: str, key: str) -> dict:
    run = _RUNS.get((name, key))
    if not _is_running(run):
        return {"ok": True, "message": "Nicht aktiv."}
    proc = run["proc"]
    try:
        if IS_WIN:
            proc.terminate()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            if IS_WIN:
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _stop_all(name: str):
    for (p, key) in list(_RUNS.keys()):
        if p == name:
            _stop_job(name, key)


def list_jobs(name: str) -> list:
    jobs = []
    for (p, key), run in list(_RUNS.items()):
        if p != name:
            continue
        running = _is_running(run)
        jobs.append({
            "key": key, "cmd": run["cmd"], "running": running,
            "started": int(run["started"]), "pid": run["proc"].pid,
            "exit": None if running else run.get("exit", run["proc"].poll()),
        })
    return jobs


def read_log(name: str, key: str) -> dict:
    run = _RUNS.get((name, key))
    path = _log_path(name, key)
    if not os.path.isfile(path):
        return {"ok": True, "log": "", "running": False}
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > MAX_LOG_BYTES:
            f.seek(size - MAX_LOG_BYTES)
        text = f.read().decode("utf-8", errors="replace")
    return {"ok": True, "log": text, "running": _is_running(run)}


# ---------------------------------------------------------------
# Projekte
# ---------------------------------------------------------------

_MAIN_TEMPLATE = '''def main():
    print("Hallo aus {name}!")


if __name__ == "__main__":
    main()
'''


def list_projects() -> list:
    if not os.path.isdir(BASE_DIR):
        return []
    result = []
    for name in sorted(os.listdir(BASE_DIR)):
        if not _exists(name):
            continue
        meta = _load_meta(name)
        jobs = [j for j in list_jobs(name) if j["running"] and j["key"].startswith("run:")]
        result.append({
            "name": name,
            "entry": meta.get("entry", "main.py"),
            "autostart": meta.get("autostart", []),
            "created": meta.get("created"),
            "running": len(jobs),
            "venv_ok": os.path.isfile(_venv_bin(name, "python")),
        })
    return result


def create_project(name: str) -> dict:
    name = normalize_name(name)
    if not name:
        return {"ok": False, "error": "Ungültiger Name."}
    if _exists(name):
        return {"ok": False, "error": f"Projekt '{name}' existiert bereits."}
    try:
        os.makedirs(_src(name), exist_ok=True)
        os.makedirs(_logs(name), exist_ok=True)
        with open(os.path.join(_src(name), "main.py"), "w", encoding="utf-8") as f:
            f.write(_MAIN_TEMPLATE.format(name=name))
        with open(os.path.join(_src(name), "requirements.txt"), "w", encoding="utf-8") as f:
            f.write("")
        res = subprocess.run([sys.executable, "-m", "venv", _venv(name)],
                             capture_output=True, text=True, timeout=180)
        if res.returncode != 0:
            shutil.rmtree(_pdir(name), ignore_errors=True)
            return {"ok": False, "error": f"venv konnte nicht erstellt werden: {res.stderr.strip()[-300:]}"}
        with open(os.path.join(_pdir(name), "bashrc"), "w", encoding="utf-8") as f:
            f.write('[ -f ~/.bashrc ] && . ~/.bashrc\n'
                    f'. "{_venv(name)}/bin/activate"\n'
                    f'cd "{_src(name)}"\n'
                    f'PS1="({name}) \\w \\$ "\n')
        _save_meta(name, {"entry": "main.py", "autostart": [], "created": int(time.time())})
    except Exception as e:
        shutil.rmtree(_pdir(name), ignore_errors=True)
        return {"ok": False, "error": str(e)}
    return {"ok": True, "name": name}


def delete_project(name: str) -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    _stop_all(name)
    for k in [k for k in _RUNS if k[0] == name]:
        _RUNS.pop(k, None)
    shutil.rmtree(_pdir(name), ignore_errors=True)
    return {"ok": True}


def update_config(name: str, entry: str = None, autostart: list = None) -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    meta = _load_meta(name)
    if entry is not None:
        meta["entry"] = entry
    if autostart is not None:
        meta["autostart"] = [a for a in autostart if isinstance(a, str)]
    _save_meta(name, meta)
    return {"ok": True}


def start_autostart():
    """Startet beim CARLA-Start alle Scripts mit Autostart-Flag."""
    for p in list_projects():
        for script in p["autostart"]:
            try:
                run_script(p["name"], script)
            except Exception as e:
                print(f"[CARLA-PY] Autostart {p['name']}/{script} fehlgeschlagen: {e}")


# ---------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------

def run_script(name: str, script: str, args: str = "") -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    target = _safe(name, script)
    if not target or not os.path.isfile(target):
        return {"ok": False, "error": f"Script nicht gefunden: {script}"}
    if not os.path.isfile(_venv_bin(name, "python")):
        return {"ok": False, "error": "venv fehlt."}
    import shlex
    try:
        extra = shlex.split(args or "")
    except ValueError as e:
        return {"ok": False, "error": f"Argumente ungültig: {e}"}
    rel = os.path.relpath(target, os.path.realpath(_src(name)))
    cmd = [_venv_bin(name, "python"), "-u", rel] + extra
    return _start_job(name, f"run:{rel}", cmd, f"python {rel} {args}".strip())


def stop_script(name: str, key: str) -> dict:
    return _stop_job(name, key)


# ---------------------------------------------------------------
# Pakete (pip in der Projekt-venv)
# ---------------------------------------------------------------

_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-\[\],=<>!~;@:/+*]*$")


def list_packages(name: str) -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    try:
        res = subprocess.run([_venv_bin(name, "python"), "-m", "pip", "list", "--format=json",
                              "--disable-pip-version-check"],
                             capture_output=True, text=True, timeout=60, env=_env(name))
        if res.returncode != 0:
            return {"ok": False, "error": res.stderr.strip()[-300:]}
        return {"ok": True, "packages": json.loads(res.stdout)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _parse_packages(raw: str):
    parts = (raw or "").replace(",", " ").split()
    if not parts:
        return None, "Kein Paket angegeben."
    for p in parts:
        if not _PKG_RE.match(p):
            return None, f"Ungültiger Paketname: {p}"
    return parts, None


def pip_install(name: str, packages: str = "", requirements: bool = False) -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    py = _venv_bin(name, "python")
    base = [py, "-m", "pip", "install", "--disable-pip-version-check"]
    if requirements:
        if not os.path.isfile(os.path.join(_src(name), "requirements.txt")):
            return {"ok": False, "error": "requirements.txt fehlt."}
        return _start_job(name, "pip", base + ["-r", "requirements.txt"], "pip install -r requirements.txt")
    parts, err = _parse_packages(packages)
    if err:
        return {"ok": False, "error": err}
    return _start_job(name, "pip", base + parts, "pip install " + " ".join(parts))


def pip_uninstall(name: str, packages: str) -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    parts, err = _parse_packages(packages)
    if err:
        return {"ok": False, "error": err}
    return _start_job(name, "pip", [_venv_bin(name, "python"), "-m", "pip", "uninstall", "-y"] + parts,
                      "pip uninstall " + " ".join(parts))


def pip_freeze(name: str) -> dict:
    """Schreibt die installierten Pakete in requirements.txt."""
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    res = subprocess.run([_venv_bin(name, "python"), "-m", "pip", "freeze", "--disable-pip-version-check"],
                         capture_output=True, text=True, timeout=60, env=_env(name))
    if res.returncode != 0:
        return {"ok": False, "error": res.stderr.strip()[-300:]}
    with open(os.path.join(_src(name), "requirements.txt"), "w", encoding="utf-8") as f:
        f.write(res.stdout)
    return {"ok": True}


# ---------------------------------------------------------------
# Dateien (nur innerhalb src/)
# ---------------------------------------------------------------

def list_dir(name: str, rel: str = "") -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    target = _safe(name, rel)
    if not target or not os.path.isdir(target):
        return {"ok": False, "error": "Ordner nicht gefunden."}
    items = []
    for entry in os.scandir(target):
        if entry.name in HIDDEN_DIRS:
            continue
        st = entry.stat()
        items.append({"name": entry.name, "type": "dir" if entry.is_dir() else "file",
                      "size": 0 if entry.is_dir() else st.st_size, "mtime": int(st.st_mtime)})
    items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    root = os.path.realpath(_src(name))
    cur = os.path.relpath(target, root)
    return {"ok": True, "path": "" if cur == "." else cur.replace(os.sep, "/"), "items": items}


def read_file(name: str, rel: str) -> dict:
    target = _safe(name, rel) if _exists(name) else None
    if not target or not os.path.isfile(target):
        return {"ok": False, "error": "Datei nicht gefunden."}
    size = os.path.getsize(target)
    if size > MAX_FILE_SIZE:
        return {"ok": False, "error": "Datei zu groß (max. 2 MB)."}
    try:
        with open(target, "r", encoding="utf-8") as f:
            return {"ok": True, "content": f.read(), "size": size}
    except UnicodeDecodeError:
        return {"ok": False, "error": "Binärdatei kann nicht bearbeitet werden."}


def write_file(name: str, rel: str, content: str) -> dict:
    target = _safe(name, rel) if _exists(name) else None
    if not target or target == os.path.realpath(_src(name)):
        return {"ok": False, "error": "Ungültiger Pfad."}
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def make_dir(name: str, rel: str) -> dict:
    target = _safe(name, rel) if _exists(name) else None
    if not target:
        return {"ok": False, "error": "Ungültiger Pfad."}
    os.makedirs(target, exist_ok=True)
    return {"ok": True}


def delete_item(name: str, rel: str) -> dict:
    target = _safe(name, rel) if _exists(name) else None
    if not target or target == os.path.realpath(_src(name)) or not os.path.exists(target):
        return {"ok": False, "error": "Ungültiger Pfad."}
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.remove(target)
    return {"ok": True}


def rename_item(name: str, rel: str, new_rel: str) -> dict:
    src = _safe(name, rel) if _exists(name) else None
    dst = _safe(name, new_rel) if _exists(name) else None
    if not src or not dst or not os.path.exists(src) or os.path.exists(dst):
        return {"ok": False, "error": "Ungültiger Pfad oder Ziel existiert bereits."}
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)
    return {"ok": True}


def save_upload(name: str, rel_dir: str, filename: str, data: bytes) -> dict:
    filename = os.path.basename(filename or "")
    if not filename:
        return {"ok": False, "error": "Dateiname fehlt."}
    target = _safe(name, os.path.join(rel_dir or "", filename)) if _exists(name) else None
    if not target:
        return {"ok": False, "error": "Ungültiger Pfad."}
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(data)
    return {"ok": True}


# ---------------------------------------------------------------
# Konsole
# ---------------------------------------------------------------

def shell_spec(name: str):
    """Gibt (argv, cwd, env) fuer die interaktive Projekt-Konsole zurueck oder None."""
    if not _exists(name):
        return None
    rc = os.path.join(_pdir(name), "bashrc")
    bash = shutil.which("bash")
    argv = [bash, "--rcfile", rc, "-i"] if bash and os.path.isfile(rc) else [shutil.which("sh") or "/bin/sh"]
    return argv, _src(name), {**_env(name), "TERM": "xterm-256color"}
