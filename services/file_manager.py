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
    """Schreibt Inhalt in eine Datei (direkt via Python, kein Shell-Limit)."""
    path = _sanitize_path(path)

    parent = _parent(path)
    check = system_executor.execute_command(f'test -d {_quote(parent)} && echo "OK" || echo "NODIR"')
    if "NODIR" in check:
        return {"ok": False, "error": f"Verzeichnis existiert nicht: {parent}"}

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": f"Fehler beim Schreiben: {e}"}


def create_directory(path: str) -> dict:
    """Erstellt ein neues Verzeichnis."""
    path = _sanitize_path(path)
    import os
    try:
        os.makedirs(path, exist_ok=True)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": f"Fehler: {e}"}


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


def upload_file(dest_dir: str, filename: str, file_bytes: bytes) -> dict:
    """Speichert eine hochgeladene Datei in ein Verzeichnis (inklusive Unterordnern)."""
    import os
    dest_dir = _sanitize_path(dest_dir)
    
    # Pfadbereinigung für relative Pfade (Unterordner)
    filename = filename.replace("\\", "/").replace("\0", "")
    combined = f"{dest_dir.rstrip('/')}/{filename.lstrip('/')}"
    target_path = _sanitize_path(combined)
    
    # Path Traversal Schutz via os.path.commonpath
    try:
        norm_dest = os.path.normpath(dest_dir)
        norm_target = os.path.normpath(target_path)
        common = os.path.commonpath([norm_dest, norm_target])
        is_safe = (os.path.normcase(common) == os.path.normcase(norm_dest))
    except (ValueError, Exception):
        is_safe = False

    if not is_safe:
        return {"ok": False, "error": "Ungültiger Pfad (Path Traversal erkannt)."}
        
    # Elternverzeichnis erstellen falls nötig
    parent_dir = _parent(target_path)
    if parent_dir != dest_dir:
        create_dir_res = create_directory(parent_dir)
        if not create_dir_res.get("ok"):
            return {"ok": False, "error": f"Konnte Unterverzeichnis nicht erstellen: {create_dir_res.get('error')}"}
            
    try:
        with open(target_path, "wb") as f:
            f.write(file_bytes)
        return {"ok": True, "path": target_path}
    except Exception as e:
        return {"ok": False, "error": f"Fehler beim Schreiben: {e}"}


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
    from services.docker_service import resolve_stack_workdir
    # Working Directory mit Fallbacks
    workdir = resolve_stack_workdir(stack_name)

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
        "workdir": workdir,
        "volumes": volumes,
    }


def get_stack_compose(stack_name: str) -> dict:
    """Liest die Compose-Datei eines Stacks und parst Volumes/Bind-Mounts."""
    import yaml
    from services.docker_service import resolve_stack_workdir

    # Working Directory mit Fallbacks
    workdir = resolve_stack_workdir(stack_name)

    if not workdir:
        return {"ok": False, "error": "Arbeitsverzeichnis nicht gefunden."}

    # Compose-Datei finden (docker-compose.yml oder compose.yml)
    compose_file = None
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        check = system_executor.execute_command(
            f'test -f {_quote(workdir + "/" + name)} && echo "OK" || echo "NO"'
        )
        if "OK" in check:
            compose_file = f"{workdir}/{name}"
            break

    if not compose_file:
        return {"ok": False, "error": "Keine Compose-Datei gefunden.", "workdir": workdir}

    # Compose-Datei lesen
    content = system_executor.execute_command(f"cat {_quote(compose_file)}")
    if not content or "Error" in content:
        return {"ok": False, "error": f"Fehler beim Lesen: {content}", "workdir": workdir}

    # .env lesen (falls vorhanden)
    env_content = ""
    env_check = system_executor.execute_command(
        f'test -f {_quote(workdir + "/.env")} && echo "OK" || echo "NO"'
    )
    if "OK" in env_check:
        env_content = system_executor.execute_command(f"cat {_quote(workdir + '/.env')}") or ""

    # YAML parsen um Volumes/Binds zu extrahieren
    mounts = []
    named_volumes = []
    try:
        parsed = yaml.safe_load(content)
        if parsed and isinstance(parsed, dict):
            services = parsed.get("services", {}) or {}
            for svc_name, svc in services.items():
                if not isinstance(svc, dict):
                    continue
                vols = svc.get("volumes", []) or []
                for v in vols:
                    if isinstance(v, str):
                        # Kurzform: host:container oder volume:container
                        parts = v.split(":")
                        if len(parts) >= 2:
                            source = parts[0].strip()
                            target = parts[1].strip()
                            mode = parts[2].strip() if len(parts) > 2 else "rw"
                            if source.startswith("/") or source.startswith("./") or source.startswith("../"):
                                # Bind-Mount — relativen Pfad auflösen
                                if source.startswith("./") or source.startswith("../"):
                                    import os.path
                                    source = os.path.normpath(f"{workdir}/{source}")
                                mounts.append({
                                    "service": svc_name, "type": "bind",
                                    "source": source, "target": target, "mode": mode
                                })
                            else:
                                # Named Volume
                                named_volumes.append({
                                    "service": svc_name, "type": "volume",
                                    "source": source, "target": target, "mode": mode
                                })
                        elif len(parts) == 1:
                            # Nur Container-Pfad (anonym)
                            mounts.append({
                                "service": svc_name, "type": "anonymous",
                                "source": "", "target": parts[0].strip(), "mode": "rw"
                            })
                    elif isinstance(v, dict):
                        # Langform: type, source, target
                        v_type = v.get("type", "volume")
                        source = v.get("source", "")
                        target = v.get("target", "")
                        mode = "ro" if v.get("read_only") else "rw"
                        if v_type == "bind" and source:
                            if source.startswith("./") or source.startswith("../"):
                                import os.path
                                source = os.path.normpath(f"{workdir}/{source}")
                            mounts.append({
                                "service": svc_name, "type": "bind",
                                "source": source, "target": target, "mode": mode
                            })
                        elif source:
                            named_volumes.append({
                                "service": svc_name, "type": "volume",
                                "source": source, "target": target, "mode": mode
                            })

            # Top-level volumes Sektion
            top_volumes = parsed.get("volumes", {}) or {}
            for vol_name, vol_cfg in top_volumes.items():
                # Pruefen ob externer oder driver-spezifischer Pfad
                if isinstance(vol_cfg, dict) and vol_cfg.get("driver_opts", {}).get("device"):
                    device = vol_cfg["driver_opts"]["device"]
                    mounts.append({
                        "service": "(top-level)", "type": "bind",
                        "source": device, "target": vol_name, "mode": "rw"
                    })
    except Exception as e:
        # YAML-Parse-Fehler ignorieren — Compose-Inhalt wird trotzdem angezeigt
        pass

    # Named Volumes: Mountpoints vom Docker-Daemon holen
    for nv in named_volumes:
        full_name = f"{stack_name}_{nv['source']}"
        mp = system_executor.execute_command(
            f"docker volume inspect {full_name} --format '{{{{.Mountpoint}}}}' 2>/dev/null"
        ).strip()
        if mp and "Error" not in mp:
            nv["mountpoint"] = mp
        else:
            # Versuch ohne Stack-Prefix
            mp2 = system_executor.execute_command(
                f"docker volume inspect {nv['source']} --format '{{{{.Mountpoint}}}}' 2>/dev/null"
            ).strip()
            nv["mountpoint"] = mp2 if mp2 and "Error" not in mp2 else ""

    return {
        "ok": True,
        "stack_name": stack_name,
        "workdir": workdir,
        "compose_file": compose_file,
        "compose_content": content,
        "env_content": env_content,
        "env_file": f"{workdir}/.env" if env_content else "",
        "bind_mounts": mounts,
        "named_volumes": named_volumes,
    }


# ---------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------

def _sanitize_path(path: str) -> str:
    """Bereinigt einen Pfad: kein Traversal, muss absolut sein."""
    import os.path
    import os
    # Doppelpunkte und Traversal entfernen
    path = path.replace("\0", "")
    # Normalisieren (loest .. auf)
    path = os.path.normpath(path)
    # Separator vereinheitlichen (wichtig für Windows-Kompatibilität bei Tests)
    path = path.replace(os.sep, "/")
    # Muss absolut sein
    if not path.startswith("/"):
        path = "/" + path
    # Unter Windows den führenden Slash bei Laufwerksbuchstaben entfernen (z.B. /c:/... -> c:/...)
    if os.name == 'nt' and path.startswith("/") and len(path) > 2 and path[1].isalpha() and path[2] == ':':
        path = path[1:]
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
