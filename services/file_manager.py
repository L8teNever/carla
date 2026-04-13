# ==============================================================
# CARLA – File Manager Service
# Durchsucht Verzeichnisse und Dateien auf dem Docker-Host,
# inklusive Stack-Arbeitsverzeichnisse und Docker-Volumes.
# ==============================================================

from . import system_executor


def list_directory(path: str) -> dict:
    """Listet Dateien und Ordner in einem Verzeichnis auf."""
    # Pfad normalisieren und absichern
    path = _sanitize_path(path)

    # Pruefen ob Pfad existiert
    check = system_executor.execute_command(f'test -d {_quote(path)} && echo "OK" || echo "NOTDIR"')
    if "NOTDIR" in check or "Error" in check:
        return {"ok": False, "error": f"Verzeichnis nicht gefunden: {path}"}

    # ls -la mit maschinenlesbarem Format
    cmd = (
        f"ls -la --time-style=long-iso {_quote(path)} 2>/dev/null | tail -n +2"
    )
    out = system_executor.execute_command(cmd)
    if not out or "Error" in out:
        return {"ok": True, "path": path, "items": [], "parent": _parent(path)}

    items = []
    for line in out.splitlines():
        parsed = _parse_ls_line(line, path)
        if parsed:
            items.append(parsed)

    # Ordner zuerst, dann Dateien, jeweils alphabetisch
    items.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))

    return {"ok": True, "path": path, "items": items, "parent": _parent(path)}


def read_file(path: str) -> dict:
    """Liest den Inhalt einer Datei (max 2MB, nur Text)."""
    path = _sanitize_path(path)

    # Datei pruefen
    check = system_executor.execute_command(f'test -f {_quote(path)} && echo "OK" || echo "NOFILE"')
    if "NOFILE" in check or "Error" in check:
        return {"ok": False, "error": f"Datei nicht gefunden: {path}"}

    # Groesse pruefen (max 2MB)
    size_str = system_executor.execute_command(f"stat -c%s {_quote(path)} 2>/dev/null")
    try:
        size = int(size_str.strip())
    except (ValueError, TypeError):
        size = 0

    if size > 2 * 1024 * 1024:
        return {"ok": False, "error": f"Datei zu gross ({_fmt_size(size)}). Maximum: 2 MB."}

    # Binaer-Check
    is_binary = system_executor.execute_command(
        f"file --mime-encoding {_quote(path)} 2>/dev/null"
    )
    if is_binary and "binary" in is_binary.lower():
        return {"ok": False, "error": "Binaerdatei kann nicht bearbeitet werden.", "binary": True, "size": size}

    content = system_executor.execute_command(f"cat {_quote(path)}")
    return {"ok": True, "path": path, "content": content, "size": size}


def write_file(path: str, content: str) -> dict:
    """Schreibt Inhalt in eine Datei."""
    path = _sanitize_path(path)

    # Verzeichnis muss existieren
    parent = _parent(path)
    check = system_executor.execute_command(f'test -d {_quote(parent)} && echo "OK" || echo "NODIR"')
    if "NODIR" in check:
        return {"ok": False, "error": f"Verzeichnis existiert nicht: {parent}"}

    # Datei schreiben via heredoc
    cmd = f"cat > {_quote(path)} << 'CARLA_FILE_EOF'\n{content}\nCARLA_FILE_EOF"
    result = system_executor.execute_command(cmd)
    if result and "Error" in result:
        return {"ok": False, "error": f"Fehler beim Schreiben: {result}"}

    return {"ok": True, "path": path}


def create_directory(path: str) -> dict:
    """Erstellt ein neues Verzeichnis."""
    path = _sanitize_path(path)
    result = system_executor.execute_command(f"mkdir -p {_quote(path)}")
    if result and "Error" in result:
        return {"ok": False, "error": f"Fehler: {result}"}
    return {"ok": True, "path": path}


def delete_item(path: str) -> dict:
    """Loescht eine Datei oder ein Verzeichnis."""
    path = _sanitize_path(path)

    # Sicherheitspruefung: kritische Pfade schuetzen
    critical = ["/", "/bin", "/sbin", "/usr", "/etc", "/lib", "/lib64",
                "/boot", "/dev", "/proc", "/sys", "/var", "/root", "/home"]
    if path.rstrip("/") in critical:
        return {"ok": False, "error": "Dieses Verzeichnis darf nicht geloescht werden."}

    check = system_executor.execute_command(
        f'test -e {_quote(path)} && echo "EXISTS" || echo "NOEXIST"'
    )
    if "NOEXIST" in check:
        return {"ok": False, "error": f"Nicht gefunden: {path}"}

    result = system_executor.execute_command(f"rm -rf {_quote(path)}")
    if result and "Error" in result:
        return {"ok": False, "error": f"Fehler: {result}"}
    return {"ok": True}


def rename_item(old_path: str, new_name: str) -> dict:
    """Benennt eine Datei oder Ordner um."""
    old_path = _sanitize_path(old_path)
    # new_name ist nur der Name, kein Pfad
    if "/" in new_name or "\\" in new_name:
        return {"ok": False, "error": "Neuer Name darf keinen Pfad enthalten."}

    parent = _parent(old_path)
    new_path = f"{parent}/{new_name}"

    result = system_executor.execute_command(f"mv {_quote(old_path)} {_quote(new_path)}")
    if result and "Error" in result:
        return {"ok": False, "error": f"Fehler: {result}"}
    return {"ok": True, "new_path": new_path}


def get_stack_paths(stack_name: str) -> dict:
    """Gibt Arbeitsverzeichnis und Volumes eines Stacks zurueck."""
    # Working Directory
    workdir = system_executor.execute_command(
        f"docker ps -a --filter 'label=com.docker.compose.project={stack_name}' "
        f"--format '{{{{.Label \"com.docker.compose.project.working_dir\"}}}}' | head -1"
    ).strip()

    # Volumes
    vol_out = system_executor.execute_command(
        f"docker volume ls --filter 'label=com.docker.compose.project={stack_name}' "
        f"--format '{{{{.Name}}}}'"
    )
    volumes = []
    if vol_out and "Error" not in vol_out:
        for v in vol_out.splitlines():
            v = v.strip()
            if not v:
                continue
            # Mountpoint ermitteln
            mp = system_executor.execute_command(
                f"docker volume inspect {v} --format '{{{{.Mountpoint}}}}'"
            ).strip()
            volumes.append({"name": v, "mountpoint": mp})

    return {
        "ok": True,
        "stack_name": stack_name,
        "workdir": workdir if workdir and "Error" not in workdir else "",
        "volumes": volumes,
    }


# ---------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------

def _sanitize_path(path: str) -> str:
    """Bereinigt einen Pfad: kein Traversal, muss absolut sein."""
    import os.path
    # Doppelpunkte und Traversal entfernen
    path = path.replace("\0", "")
    # Normalisieren (loest .. auf)
    path = os.path.normpath(path)
    # Muss absolut sein
    if not path.startswith("/"):
        path = "/" + path
    return path


def _quote(path: str) -> str:
    """Quotiert einen Pfad fuer Shell-Befehle."""
    return "'" + path.replace("'", "'\\''") + "'"


def _parent(path: str) -> str:
    """Gibt das Elternverzeichnis zurueck."""
    if path == "/":
        return "/"
    return "/".join(path.rstrip("/").split("/")[:-1]) or "/"


def _fmt_size(size: int) -> str:
    """Formatiert Bytes in menschenlesbare Groesse."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _parse_ls_line(line: str, base_path: str) -> dict | None:
    """Parst eine Zeile von ls -la --time-style=long-iso."""
    # Format: drwxr-xr-x 2 root root 4096 2024-01-01 03:00 dirname
    parts = line.split(None, 7)
    if len(parts) < 8:
        return None

    perms = parts[0]
    owner = parts[2]
    group = parts[3]
    size_str = parts[4]
    date_str = parts[5]
    time_str = parts[6]
    name = parts[7]

    # . und .. ueberspringen
    if name in (".", ".."):
        return None

    # Symlink-Ziel abschneiden
    if " -> " in name:
        name = name.split(" -> ")[0]

    item_type = "dir" if perms.startswith("d") else ("link" if perms.startswith("l") else "file")

    try:
        size = int(size_str)
    except ValueError:
        size = 0

    return {
        "name": name,
        "type": item_type,
        "size": size,
        "size_human": _fmt_size(size),
        "permissions": perms,
        "owner": f"{owner}:{group}",
        "modified": f"{date_str} {time_str}",
        "path": f"{base_path.rstrip('/')}/{name}",
    }
