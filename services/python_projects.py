# ==============================================================
# CARLA – Python Projects Service
# Jedes Python-Projekt laeuft in einem eigenen Docker-Container
# (python:<version>-slim, restart: unless-stopped) mit eigener
# venv, Dateistruktur, Paketverwaltung, Script-Runner und Konsole.
#
# Layout je Projekt (auf dem Host und in CARLA identisch):
#   /opt/stacks/carla-py-<name>/
#       src/                <- Projektdateien (im Container /project/src)
#       venv/               <- venv, im Container erstellt
#       logs/               <- <key>.log / .pid / .exit je Script oder pip
#       docker-compose.yml, entrypoint.sh, runner.sh, bashrc
#       autostart.txt       <- Scripts, die beim Container-Start laufen
#       meta.json           <- Einstellungen (Port, Version, Domain, ...)
#
# Der Container nutzt network_mode: host. Scripts bekommen den
# Port per Umgebungsvariable PORT (fuer Flask/FastAPI & Co.).
# ==============================================================

import json
import os
import re
import shlex
import shutil
import subprocess
import time

from services import static_server, system_executor

BASE_DIR = "/opt/stacks"
PREFIX = "carla-py-"
PORT_RANGE_START = 11000
PORT_RANGE_END = 11099
PY_VERSIONS = ["3.9", "3.10", "3.11", "3.12", "3.13"]
DEFAULT_VERSION = "3.12"
MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_LOG_BYTES = 200 * 1024
HIDDEN_DIRS = {"__pycache__", ".git"}


# ---------------------------------------------------------------
# Pfade & Hilfsfunktionen
# ---------------------------------------------------------------

def normalize_name(name: str) -> str:
    return static_server.normalize_name(name or "")


def _pdir(name: str) -> str:
    return os.path.join(BASE_DIR, PREFIX + name)


def _cname(name: str) -> str:
    return PREFIX + name


def _src(name: str) -> str:
    return os.path.join(_pdir(name), "src")


def _logs(name: str) -> str:
    return os.path.join(_pdir(name), "logs")


def _exists(name: str) -> bool:
    return bool(name) and os.path.isdir(_src(name))


def _safe(name: str, rel: str):
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


def _run(args: list, timeout: int = 60, cwd: str = None):
    """Fuehrt einen Befehl ohne Shell aus. Gibt (returncode, ausgabe) zurueck."""
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return res.returncode, (res.stdout + res.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _write(path: str, content: str, mode: int = None):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    if mode:
        os.chmod(path, mode)


def _container_states() -> dict:
    rc, out = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"], timeout=20)
    states = {}
    if rc == 0:
        for line in out.splitlines():
            if "\t" in line:
                n, s = line.split("\t", 1)
                states[n.strip()] = s.strip()
    return states


def _state(name: str) -> str:
    return _container_states().get(_cname(name), "stopped")


def _job_key(script: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", "run-" + script)


# ---------------------------------------------------------------
# Container-Dateien (Compose, Entrypoint, Runner, bashrc)
# ---------------------------------------------------------------

_RUNNER = r'''#!/bin/sh
# usage: runner.sh <key> <python-args...>   (z.B. runner.sh run-main.py main.py --flag)
key="$1"; shift
mkdir -p /project/logs
LOG="/project/logs/$key.log"
echo $$ > "/project/logs/$key.pid"
rm -f "/project/logs/$key.exit"
echo "\$ python $*" > "$LOG"
cd /project/src || exit 1
[ -x /project/venv/bin/python ] || python -m venv /project/venv >> "$LOG" 2>&1
. /project/venv/bin/activate
python -u "$@" >> "$LOG" 2>&1
code=$?
printf '\n[CARLA] Prozess beendet (Exit-Code %s)\n' "$code" >> "$LOG"
echo "$code" > "/project/logs/$key.exit"
rm -f "/project/logs/$key.pid"
'''

_ENTRYPOINT = r'''#!/bin/sh
cd /project
mkdir -p logs src
rm -f logs/*.pid
[ -x venv/bin/python ] || python -m venv venv
if [ -f autostart.txt ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    eval "setsid sh /project/runner.sh $line >/dev/null 2>&1 &"
  done < autostart.txt
fi
exec sleep infinity
'''


def _bashrc(name: str) -> str:
    return ('[ -f ~/.bashrc ] && . ~/.bashrc\n'
            '[ -f /project/venv/bin/activate ] && . /project/venv/bin/activate\n'
            'cd /project/src\n'
            f'PS1="({name}) \\w \\$ "\n')


def _compose(name: str, version: str, port: int) -> str:
    return f"""services:
  app:
    image: python:{version}-slim
    container_name: {_cname(name)}
    restart: unless-stopped
    init: true
    network_mode: host
    working_dir: /project/src
    environment:
      - PORT={port}
      - PYTHONUNBUFFERED=1
    volumes:
      - {_pdir(name)}:/project
    command: ["sh", "/project/entrypoint.sh"]
"""


def _write_support_files(name: str, meta: dict):
    d = _pdir(name)
    _write(os.path.join(d, "runner.sh"), _RUNNER, 0o755)
    _write(os.path.join(d, "entrypoint.sh"), _ENTRYPOINT, 0o755)
    _write(os.path.join(d, "bashrc"), _bashrc(name))
    _write(os.path.join(d, "docker-compose.yml"),
           _compose(name, meta.get("python", DEFAULT_VERSION), meta["port"]))
    lines = []
    for a in meta.get("autostart", []):
        key = _job_key(a["script"])
        lines.append(f"{key} {shlex.quote(a['script'])} {a.get('args', '')}".rstrip())
    _write(os.path.join(d, "autostart.txt"), "\n".join(lines) + ("\n" if lines else ""))


# ---------------------------------------------------------------
# Ports & Cloudflare
# ---------------------------------------------------------------

def _find_free_port() -> int:
    out = system_executor.execute_command(
        "ss -tlnp 2>/dev/null | awk 'NR>1 {print $4}' | grep -oE '[0-9]+$'")
    used = set()
    for line in out.splitlines():
        try:
            used.add(int(line.strip()))
        except ValueError:
            pass
    if os.path.isdir(BASE_DIR):
        for entry in os.listdir(BASE_DIR):
            if entry.startswith(PREFIX):
                p = _load_meta(entry[len(PREFIX):]).get("port")
                if p:
                    used.add(p)
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port not in used:
            return port
    raise RuntimeError(f"Keine freien Ports im Bereich {PORT_RANGE_START}-{PORT_RANGE_END}.")


def _cf_setup(cloudflare_data: dict, port: int):
    """Ingress + CNAME anlegen. Gibt (hostname, fehlertext) zurueck."""
    cf = static_server._get_cf_client()
    if not cf:
        return None, "Cloudflare nicht konfiguriert."
    tunnel_id = cloudflare_data.get("tunnel_id")
    hostname = (cloudflare_data.get("hostname") or "").strip()
    if not tunnel_id or not hostname:
        return None, "Tunnel-ID und Hostname erforderlich."
    zone_id = cf.find_zone_id(hostname)
    if not zone_id:
        return None, f"Keine Cloudflare-Zone für '{hostname}' gefunden."
    host_ip = static_server._get_host_ip_from_tunnel(tunnel_id, cf)
    rules = cf.get_tunnel_ingress(tunnel_id)
    non_catchall = [r for r in rules if not r.get("is_catchall") and r.get("hostname")]
    if not any(r["hostname"] == hostname for r in non_catchall):
        non_catchall.append({"hostname": hostname, "service": f"http://{host_ip}:{port}"})
    catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
    res = cf.update_tunnel_ingress(tunnel_id, non_catchall + [catchall])
    if not res.get("success"):
        return None, f"Tunnel konnte nicht aktualisiert werden: {res.get('errors')}"
    cf.delete_cname_record(zone_id, hostname)
    dns = cf.create_cname_record(zone_id, hostname, f"{tunnel_id}.cfargotunnel.com")
    if not dns.get("success"):
        return None, f"DNS-Eintrag konnte nicht erstellt werden: {dns.get('errors')}"
    return hostname, None


def _cf_cleanup(meta: dict):
    cf_data = meta.get("cloudflare") or {}
    hostname = meta.get("domain")
    if not (cf_data.get("enabled") and cf_data.get("tunnel_id") and hostname):
        return
    cf = static_server._get_cf_client()
    if not cf:
        return
    tunnel_id = cf_data["tunnel_id"]
    rules = cf.get_tunnel_ingress(tunnel_id)
    keep = [r for r in rules if not r.get("is_catchall") and r.get("hostname") != hostname]
    catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
    cf.update_tunnel_ingress(tunnel_id, keep + [catchall])
    zone_id = cf.find_zone_id(hostname)
    if zone_id:
        cf.delete_cname_record(zone_id, hostname)


# ---------------------------------------------------------------
# Projekte
# ---------------------------------------------------------------

_MAIN_TEMPLATE = '''import os


def main():
    print("Hallo aus {name}!")
    print("PORT =", os.environ.get("PORT"))


if __name__ == "__main__":
    main()
'''


def list_projects() -> list:
    if not os.path.isdir(BASE_DIR):
        return []
    states = _container_states()
    result = []
    for entry in sorted(os.listdir(BASE_DIR)):
        if not entry.startswith(PREFIX):
            continue
        name = entry[len(PREFIX):]
        if not _exists(name):
            continue
        meta = _load_meta(name)
        state = states.get(_cname(name), "stopped")
        result.append({
            "name": name,
            "state": state,
            "python": meta.get("python", DEFAULT_VERSION),
            "port": meta.get("port"),
            "domain": meta.get("domain"),
            "entry": meta.get("entry", "main.py"),
            "autostart": meta.get("autostart", []),
            "running": len([j for j in list_jobs(name, state) if j["running"] and j["key"].startswith("run-")]),
            "venv_ok": os.path.isfile(os.path.join(_pdir(name), "venv", "bin", "python")),
        })
    return result


def create_project(name: str, python_version: str = DEFAULT_VERSION, cloudflare_data: dict = None) -> dict:
    name = normalize_name(name)
    if not name:
        return {"ok": False, "error": "Ungültiger Name."}
    if _exists(name):
        return {"ok": False, "error": f"Projekt '{name}' existiert bereits."}
    if python_version not in PY_VERSIONS:
        return {"ok": False, "error": f"Python-Version nicht unterstützt: {python_version}"}
    try:
        port = _find_free_port()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    cloudflare_data = cloudflare_data or {"enabled": False}
    hostname = None
    if cloudflare_data.get("enabled"):
        hostname, err = _cf_setup(cloudflare_data, port)
        if err:
            return {"ok": False, "error": err}

    try:
        os.makedirs(_src(name), exist_ok=True)
        os.makedirs(_logs(name), exist_ok=True)
        _write(os.path.join(_src(name), "main.py"), _MAIN_TEMPLATE.format(name=name))
        _write(os.path.join(_src(name), "requirements.txt"), "")
        meta = {"name": name, "python": python_version, "port": port, "entry": "main.py",
                "autostart": [], "domain": hostname, "cloudflare": cloudflare_data,
                "created": int(time.time())}
        _save_meta(name, meta)
        _write_support_files(name, meta)
    except Exception as e:
        shutil.rmtree(_pdir(name), ignore_errors=True)
        return {"ok": False, "error": str(e)}

    rc, out = _run(["docker", "compose", "up", "-d"], timeout=300, cwd=_pdir(name))
    if rc != 0:
        return {"ok": False, "error": f"Container-Start fehlgeschlagen: {out[-400:]}"}
    return {"ok": True, "name": name, "port": port, "domain": hostname}


def delete_project(name: str) -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    try:
        _cf_cleanup(_load_meta(name))
    except Exception as e:
        print(f"[CARLA-PY] Cloudflare-Cleanup fehlgeschlagen: {e}")
    _run(["docker", "compose", "down"], timeout=60, cwd=_pdir(name))
    shutil.rmtree(_pdir(name), ignore_errors=True)
    return {"ok": True}


def execute_action(name: str, action: str) -> dict:
    cmds = {"start": ["up", "-d"], "stop": ["stop"], "restart": ["restart"]}
    if action not in cmds:
        return {"ok": False, "error": f"Aktion nicht erlaubt: {action}"}
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    _write_support_files(name, _load_meta(name))   # Runner/Entrypoint aktuell halten
    rc, out = _run(["docker", "compose"] + cmds[action], timeout=120, cwd=_pdir(name))
    return {"ok": rc == 0, "output": out, "error": out if rc != 0 else None}


def update_config(name: str, entry: str = None, autostart: list = None) -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    meta = _load_meta(name)
    if entry is not None:
        meta["entry"] = entry
    if autostart is not None:
        clean = []
        for a in autostart:
            target = _safe(name, a["script"]) if isinstance(a, dict) and isinstance(a.get("script"), str) else None
            if target:
                args = a.get("args", "") or ""
                try:
                    shlex.split(args)
                except ValueError:
                    return {"ok": False, "error": f"Argumente ungültig: {args}"}
                clean.append({"script": os.path.relpath(target, os.path.realpath(_src(name))).replace(os.sep, "/"),
                              "args": " ".join(shlex.quote(x) for x in shlex.split(args))})
        meta["autostart"] = clean
    _save_meta(name, meta)
    _write_support_files(name, meta)
    return {"ok": True}


# ---------------------------------------------------------------
# Jobs (Scripts & pip) – laufen per docker exec ueber runner.sh
# ---------------------------------------------------------------

def _alive_keys(name: str) -> set:
    """Keys aller Jobs, deren pid-Datei auf einen lebenden Prozess zeigt."""
    script = ('for f in /project/logs/*.pid; do [ -f "$f" ] || continue; '
              'p=$(cat "$f"); kill -0 "$p" 2>/dev/null && basename "$f" .pid; done')
    rc, out = _run(["docker", "exec", _cname(name), "sh", "-c", script], timeout=15)
    return set(out.split()) if rc == 0 else set()


def list_jobs(name: str, state: str = None) -> list:
    logs = _logs(name)
    if not os.path.isdir(logs):
        return []
    state = state or _state(name)
    alive = _alive_keys(name) if state == "running" else set()
    jobs = []
    for f in sorted(os.listdir(logs)):
        if not f.endswith(".log"):
            continue
        key = f[:-4]
        path = os.path.join(logs, f)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline().strip()
        except OSError:
            first = ""
        exit_code = None
        try:
            with open(os.path.join(logs, key + ".exit")) as fh:
                exit_code = int(fh.read().strip())
        except (OSError, ValueError):
            pass
        running = key in alive
        jobs.append({"key": key, "cmd": first.lstrip("$ ").strip() or key, "running": running,
                     "exit": None if running else exit_code, "mtime": int(os.path.getmtime(path))})
    jobs.sort(key=lambda j: -j["mtime"])
    return jobs


def read_log(name: str, key: str) -> dict:
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", key or "")
    path = os.path.join(_logs(name), key + ".log")
    if not key or not os.path.isfile(path):
        return {"ok": True, "log": "", "running": False}
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > MAX_LOG_BYTES:
            f.seek(size - MAX_LOG_BYTES)
        text = f.read().decode("utf-8", errors="replace")
    running = key in _alive_keys(name) if _state(name) == "running" else False
    return {"ok": True, "log": text, "running": running}


def _start_job(name: str, key: str, py_args: list) -> dict:
    if _state(name) != "running":
        return {"ok": False, "error": "Container läuft nicht – erst starten."}
    if key in _alive_keys(name):
        return {"ok": False, "error": "Läuft bereits."}
    rc, out = _run(["docker", "exec", "-d", _cname(name), "setsid", "sh", "/project/runner.sh", key] + py_args, timeout=20)
    return {"ok": rc == 0, "error": None if rc == 0 else out}


def run_script(name: str, script: str, args: str = "") -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    target = _safe(name, script)
    if not target or not os.path.isfile(target):
        return {"ok": False, "error": f"Script nicht gefunden: {script}"}
    try:
        extra = shlex.split(args or "")
    except ValueError as e:
        return {"ok": False, "error": f"Argumente ungültig: {e}"}
    rel = os.path.relpath(target, os.path.realpath(_src(name))).replace(os.sep, "/")
    res = _start_job(name, _job_key(rel), [rel] + extra)
    res["key"] = _job_key(rel)
    return res


def stop_script(name: str, key: str) -> dict:
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", key or "")
    pid_file = os.path.join(_logs(name), key + ".pid")
    if not key or not os.path.isfile(pid_file) or key not in _alive_keys(name):
        return {"ok": True, "message": "Nicht aktiv."}
    pid = open(pid_file).read().strip()
    if not pid.isdigit():
        return {"ok": False, "error": "Ungültige PID."}
    cn = _cname(name)
    _run(["docker", "exec", cn, "kill", "-TERM", "--", f"-{pid}"], timeout=10)
    for _ in range(8):
        time.sleep(0.5)
        if key not in _alive_keys(name):
            break
    else:
        _run(["docker", "exec", cn, "kill", "-KILL", "--", f"-{pid}"], timeout=10)
    try:
        with open(os.path.join(_logs(name), key + ".log"), "a", encoding="utf-8") as f:
            f.write("\n[CARLA] Gestoppt\n")
        os.remove(pid_file)
    except OSError:
        pass
    return {"ok": True}


# ---------------------------------------------------------------
# Pakete (pip in der Projekt-venv, im Container)
# ---------------------------------------------------------------

_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-\[\],=<>!~;@:/+*]*$")
_PIP = "/project/venv/bin/python"


def list_packages(name: str) -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    if _state(name) != "running":
        return {"ok": False, "error": "Container läuft nicht."}
    rc, out = _run(["docker", "exec", _cname(name), _PIP, "-m", "pip", "list", "--format=json",
                    "--disable-pip-version-check"], timeout=60)
    if rc != 0:
        return {"ok": False, "error": out[-300:] or "venv noch nicht bereit."}
    try:
        return {"ok": True, "packages": json.loads(out)}
    except ValueError:
        return {"ok": False, "error": "pip-Ausgabe nicht lesbar."}


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
    base = ["-m", "pip", "install", "--disable-pip-version-check"]
    if requirements:
        if not os.path.isfile(os.path.join(_src(name), "requirements.txt")):
            return {"ok": False, "error": "requirements.txt fehlt."}
        return _start_job(name, "pip", base + ["-r", "requirements.txt"])
    parts, err = _parse_packages(packages)
    if err:
        return {"ok": False, "error": err}
    return _start_job(name, "pip", base + parts)


def pip_uninstall(name: str, packages: str) -> dict:
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    parts, err = _parse_packages(packages)
    if err:
        return {"ok": False, "error": err}
    return _start_job(name, "pip", ["-m", "pip", "uninstall", "-y"] + parts)


def pip_freeze(name: str) -> dict:
    """Schreibt die installierten Pakete in requirements.txt."""
    if not _exists(name):
        return {"ok": False, "error": "Projekt nicht gefunden."}
    rc, out = _run(["docker", "exec", _cname(name), _PIP, "-m", "pip", "freeze",
                    "--disable-pip-version-check"], timeout=60)
    if rc != 0:
        return {"ok": False, "error": out[-300:]}
    _write(os.path.join(_src(name), "requirements.txt"), out + ("\n" if out else ""))
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
    cur = os.path.relpath(target, os.path.realpath(_src(name)))
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
        _write(target, content)
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
    """(argv, cwd, env) fuer die interaktive Konsole im Projekt-Container, sonst None."""
    if not _exists(name) or _state(name) != "running":
        return None
    argv = ["docker", "exec", "-it", "-w", "/project/src", _cname(name),
            "sh", "-c", "[ -x /usr/bin/bash ] && exec bash --rcfile /project/bashrc -i || exec sh"]
    return argv, None, {**os.environ, "TERM": "xterm-256color"}
