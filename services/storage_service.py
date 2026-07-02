# ==============================================================
# CARLA – Storage Service
# Ermittelt den Speicherplatz der Host-Maschine, Docker-Images,
# Container, Volumes und Stack-Ordner. Bietet Cleanup-Aktionen.
# ==============================================================

import os
import sys
import shutil
import re
from . import system_executor

def format_bytes(b: float) -> str:
    """Formatiert Byte-Zahlen in lesbare Einheiten."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024.0:
            return f"{b:.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"

def parse_size_to_bytes(size_str: str) -> float:
    """Konvertiert Groessen-Strings wie '1.2GB', '45.2 MB' oder '800B' in Bytes."""
    size_str = size_str.strip().upper()
    if not size_str or size_str == "0B" or size_str == "0 B" or size_str == "0":
        return 0.0
    
    # Extrahiere Zahl
    val_match = re.search(r'([0-9.]+)', size_str)
    if not val_match:
        return 0.0
    val = float(val_match.group(1))
    
    # Multiplikator
    if "G" in size_str:
        return val * 1024 * 1024 * 1024
    if "M" in size_str:
        return val * 1024 * 1024
    if "K" in size_str:
        return val * 1024
    return val

def get_dir_size(path: str) -> int:
    """Berechnet die Gesamtgroesse eines Verzeichnisses rekursiv in Bytes."""
    total_size = 0
    try:
        if os.path.exists(path):
            for dirpath, dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        total_size += os.path.getsize(fp)
    except Exception:
        pass
    return total_size

def get_storage_data() -> dict:
    """Sammelt alle Host- und Docker-Speicherdaten."""
    # 1. Host Speicherplatz (funktioniert auf Windows & Linux)
    try:
        total, used, free = shutil.disk_usage(os.path.abspath(os.sep))
        host_pct = round((used / total) * 100, 1) if total > 0 else 0.0
        host_data = {
            "total": total,
            "used": used,
            "free": free,
            "percent": host_pct,
            "formatted_total": format_bytes(total),
            "formatted_used": format_bytes(used),
            "formatted_free": format_bytes(free)
        }
    except Exception as e:
        host_data = {
            "total": 0, "used": 0, "free": 0, "percent": 0.0,
            "formatted_total": "0 B", "formatted_used": "0 B", "formatted_free": "0 B",
            "error": str(e)
        }

    # Wenn Windows: Lade Mock-Daten zur Entwicklung
    if sys.platform == "win32":
        return _get_mock_storage_data(host_data)

    # 2. Docker System-DF Zusammenfassung (Images, Containers, Volumes, Build Cache)
    docker_df = {
        "images_size": 0,
        "containers_size": 0,
        "volumes_size": 0,
        "build_cache_size": 0,
        "total_docker_size": 0
    }
    
    df_raw = system_executor.execute_command("docker system df 2>/dev/null")
    if df_raw and "Error" not in df_raw and "not found" not in df_raw.lower():
        for line in df_raw.splitlines():
            # Zeile sieht z.B. so aus:
            # Images              5                   2                   1.2GB               800MB (66%)
            parts = line.split()
            if len(parts) >= 4:
                type_name = parts[0].lower()
                size_str = parts[3]
                if "images" in type_name:
                    docker_df["images_size"] = parse_size_to_bytes(size_str)
                elif "containers" in type_name:
                    docker_df["containers_size"] = parse_size_to_bytes(size_str)
                elif "volumes" in type_name:
                    docker_df["volumes_size"] = parse_size_to_bytes(size_str)
                elif "cache" in type_name or "build" in type_name:
                    docker_df["build_cache_size"] = parse_size_to_bytes(parts[3] if len(parts) == 5 else parts[2])
        
        docker_df["total_docker_size"] = (
            docker_df["images_size"] + 
            docker_df["containers_size"] + 
            docker_df["volumes_size"] + 
            docker_df["build_cache_size"]
        )

    # 3. Docker-Images Liste
    images_list = []
    img_raw = system_executor.execute_command("docker image ls --format '{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}' 2>/dev/null")
    if img_raw and "Error" not in img_raw:
        for line in img_raw.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                repo, tag, img_id, size_str = parts
                size_bytes = parse_size_to_bytes(size_str)
                images_list.append({
                    "repository": repo,
                    "tag": tag,
                    "id": img_id,
                    "size_bytes": size_bytes,
                    "formatted_size": size_str
                })
        # Sortiere nach Groesse absteigend
        images_list.sort(key=lambda x: x["size_bytes"], reverse=True)

    # 4. Docker-Container Liste (mit writable-layer und virtual-size)
    containers_list = []
    # format liefert z.B. "name\tsize\tstate"
    # size ist meist "0B (virtual 23.5MB)" oder "15.2MB (virtual 45.1MB)"
    c_raw = system_executor.execute_command("docker ps -as --format '{{.Names}}\t{{.Size}}\t{{.State}}' 2>/dev/null")
    if c_raw and "Error" not in c_raw:
        for line in c_raw.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                name, size_str, state = parts
                
                # Parsen von z.B. "15.2MB (virtual 45.1MB)"
                writable_bytes = 0.0
                virtual_bytes = 0.0
                
                # Writable size
                w_match = re.search(r'^([0-9.A-Z\s]+)', size_str)
                if w_match:
                    writable_bytes = parse_size_to_bytes(w_match.group(1))
                    
                # Virtual size
                v_match = re.search(r'virtual\s+([0-9.A-Z\s]+)', size_str)
                if v_match:
                    virtual_bytes = parse_size_to_bytes(v_match.group(1))
                else:
                    virtual_bytes = writable_bytes
                    
                containers_list.append({
                    "name": name,
                    "writable_bytes": writable_bytes,
                    "formatted_writable": format_bytes(writable_bytes),
                    "virtual_bytes": virtual_bytes,
                    "formatted_virtual": format_bytes(virtual_bytes),
                    "state": state
                })
        containers_list.sort(key=lambda x: x["writable_bytes"], reverse=True)

    # 5. Docker-Volumes Liste
    volumes_list = []
    # Da volume inspect die Groesse nicht direkt liefert, parsen wir 'docker system df -v'
    vol_raw = system_executor.execute_command("docker system df -v 2>/dev/null")
    if vol_raw and "Error" not in vol_raw:
        # Finde den Abschnitt fuer Local Volumes
        in_volumes_section = False
        for line in vol_raw.splitlines():
            if "VOLUME NAME" in line:
                in_volumes_section = True
                continue
            if in_volumes_section:
                if not line.strip():
                    in_volumes_section = False
                    continue
                # Zeile parsen, z.B. "carla_db_data           1                   450MB"
                parts = line.split()
                if len(parts) >= 3:
                    vol_name = parts[0]
                    links = parts[1]
                    size_str = parts[2]
                    size_bytes = parse_size_to_bytes(size_str)
                    volumes_list.append({
                        "name": vol_name,
                        "links": links,
                        "size_bytes": size_bytes,
                        "formatted_size": size_str
                    })
        volumes_list.sort(key=lambda x: x["size_bytes"], reverse=True)

    # 6. Stack-Verzeichnisse (/opt/stacks/*)
    folders_list = []
    stacks_dir = "/opt/stacks"
    if os.path.exists(stacks_dir):
        try:
            for item in os.listdir(stacks_dir):
                item_path = os.path.join(stacks_dir, item)
                if os.path.isdir(item_path):
                    size = get_dir_size(item_path)
                    folders_list.append({
                        "name": item,
                        "path": item_path,
                        "size_bytes": size,
                        "formatted_size": format_bytes(size)
                    })
            folders_list.sort(key=lambda x: x["size_bytes"], reverse=True)
        except Exception:
            pass

    return {
        "host": host_data,
        "docker_df": {
            "images_size": docker_df["images_size"],
            "formatted_images": format_bytes(docker_df["images_size"]),
            "containers_size": docker_df["containers_size"],
            "formatted_containers": format_bytes(docker_df["containers_size"]),
            "volumes_size": docker_df["volumes_size"],
            "formatted_volumes": format_bytes(docker_df["volumes_size"]),
            "build_cache_size": docker_df["build_cache_size"],
            "formatted_build_cache": format_bytes(docker_df["build_cache_size"]),
            "total_docker_size": docker_df["total_docker_size"],
            "formatted_total_docker": format_bytes(docker_df["total_docker_size"])
        },
        "images": images_list,
        "containers": containers_list,
        "volumes": volumes_list,
        "folders": folders_list
    }

def _get_mock_storage_data(host_data: dict) -> dict:
    """Gibt Mock-Daten fuer die Entwicklung unter Windows zurueck."""
    # Simuliere Host-Daten falls die Windows-Festplatte riesig ist
    mock_host = host_data.copy()
    mock_host["total"] = 512 * 1024 * 1024 * 1024 # 512 GB
    mock_host["used"] = 142.5 * 1024 * 1024 * 1024 # 142.5 GB
    mock_host["free"] = mock_host["total"] - mock_host["used"]
    mock_host["percent"] = 27.8
    mock_host["formatted_total"] = format_bytes(mock_host["total"])
    mock_host["formatted_used"] = format_bytes(mock_host["used"])
    mock_host["formatted_free"] = format_bytes(mock_host["free"])

    docker_sizes = {
        "images_size": 42.4 * 1024 * 1024 * 1024, # 42.4 GB
        "containers_size": 1.25 * 1024 * 1024 * 1024, # 1.25 GB
        "volumes_size": 8.92 * 1024 * 1024 * 1024, # 8.92 GB
        "build_cache_size": 4.11 * 1024 * 1024 * 1024 # 4.11 GB
    }
    total_docker = sum(docker_sizes.values())

    images = [
        {"repository": "postgres", "tag": "15", "id": "ef827392a832", "size_bytes": 379 * 1024 * 1024, "formatted_size": "379 MB"},
        {"repository": "node", "tag": "18-alpine", "id": "bc381a9218d3", "size_bytes": 174 * 1024 * 1024, "formatted_size": "174 MB"},
        {"repository": "nginx", "tag": "alpine", "id": "ac182c82b1c2", "size_bytes": 23.5 * 1024 * 1024, "formatted_size": "23.5 MB"},
        {"repository": "redis", "tag": "alpine", "id": "fd918ba72c91", "size_bytes": 32.4 * 1024 * 1024, "formatted_size": "32.4 MB"},
        {"repository": "carla-app", "tag": "latest", "id": "771b9e28c8d1", "size_bytes": 142 * 1024 * 1024, "formatted_size": "142 MB"}
    ]
    images.sort(key=lambda x: x["size_bytes"], reverse=True)

    containers = [
        {"name": "carla-web", "writable_bytes": 12.4 * 1024 * 1024, "formatted_writable": "12.40 MB", "virtual_bytes": 35.9 * 1024 * 1024, "formatted_virtual": "35.90 MB", "state": "running"},
        {"name": "carla-db", "writable_bytes": 45.1 * 1024 * 1024, "formatted_writable": "45.10 MB", "virtual_bytes": 424.3 * 1024 * 1024, "formatted_virtual": "424.30 MB", "state": "running"},
        {"name": "nextcloud-app", "writable_bytes": 104.5 * 1024 * 1024, "formatted_writable": "104.50 MB", "virtual_bytes": 890.2 * 1024 * 1024, "formatted_virtual": "890.20 MB", "state": "running"},
        {"name": "wordpress-db", "writable_bytes": 18.2 * 1024 * 1024, "formatted_writable": "18.20 MB", "virtual_bytes": 397.4 * 1024 * 1024, "formatted_virtual": "397.40 MB", "state": "exited"},
    ]
    containers.sort(key=lambda x: x["writable_bytes"], reverse=True)

    volumes = [
        {"name": "nextcloud_data", "links": "1", "size_bytes": 8.4 * 1024 * 1024 * 1024, "formatted_size": "8.40 GB"},
        {"name": "carla_db_data", "links": "1", "size_bytes": 245.8 * 1024 * 1024, "formatted_size": "245.80 MB"},
        {"name": "wordpress_db_data", "links": "0", "size_bytes": 156.2 * 1024 * 1024, "formatted_size": "156.20 MB"},
    ]
    volumes.sort(key=lambda x: x["size_bytes"], reverse=True)

    folders = [
        {"name": "nextcloud", "path": "/opt/stacks/nextcloud", "size_bytes": 12.4 * 1024 * 1024 * 1024, "formatted_size": "12.40 GB"},
        {"name": "carla", "path": "/opt/stacks/carla", "size_bytes": 450 * 1024 * 1024, "formatted_size": "450.00 MB"},
        {"name": "monitoring", "path": "/opt/stacks/monitoring", "size_bytes": 1.2 * 1024 * 1024 * 1024, "formatted_size": "1.20 GB"},
        {"name": "wordpress", "path": "/opt/stacks/wordpress", "size_bytes": 189.5 * 1024 * 1024, "formatted_size": "189.50 MB"}
    ]
    folders.sort(key=lambda x: x["size_bytes"], reverse=True)

    return {
        "host": mock_host,
        "docker_df": {
            "images_size": docker_sizes["images_size"],
            "formatted_images": format_bytes(docker_sizes["images_size"]),
            "containers_size": docker_sizes["containers_size"],
            "formatted_containers": format_bytes(docker_sizes["containers_size"]),
            "volumes_size": docker_sizes["volumes_size"],
            "formatted_volumes": format_bytes(docker_sizes["volumes_size"]),
            "build_cache_size": docker_sizes["build_cache_size"],
            "formatted_build_cache": format_bytes(docker_sizes["build_cache_size"]),
            "total_docker_size": total_docker,
            "formatted_total_docker": format_bytes(total_docker)
        },
        "images": images,
        "containers": containers,
        "volumes": volumes,
        "folders": folders
    }

def execute_cleanup(options: dict) -> dict:
    """Fuehrt ausgewaehlte Bereinigungsaktionen aus."""
    if sys.platform == "win32":
        # Simuliere Bereinigung unter Windows
        import time
        time.sleep(1.5)
        freed = 0.0
        details = []
        if options.get("unused_containers"):
            freed += 0.12 * 1024 * 1024 * 1024 # 120 MB
            details.append("2 gestoppte Container gelöscht.")
        if options.get("unused_volumes"):
            freed += 0.156 * 1024 * 1024 * 1024 # 156 MB
            details.append("1 ungenutztes Volume (wordpress_db_data) gelöscht.")
        if options.get("unused_images"):
            freed += 1.84 * 1024 * 1024 * 1024 # 1.84 GB
            details.append("5 ungenutzte Images gelöscht.")
        if options.get("build_cache"):
            freed += 2.1 * 1024 * 1024 * 1024 # 2.1 GB
            details.append("Docker Build Cache geleert.")
            
        return {
            "success": True,
            "freed_bytes": freed,
            "formatted_freed": format_bytes(freed),
            "output": "MOCK CLEANUP SUCCESS:\n" + "\n".join(details)
        }

    output = []
    freed_bytes = 0.0
    
    # 1. Unused Containers (docker system prune)
    if options.get("unused_containers"):
        cmd = "docker container prune -f 2>&1"
        res = system_executor.execute_command(cmd)
        output.append(f"--- Containers Clean ---\n{res}")
        # Versuche freigegebenen Speicherplatz zu extrahieren
        match = re.search(r'Total reclaimed space:\s+([0-9.A-Z\s]+)', res, re.IGNORECASE)
        if match:
            freed_bytes += parse_size_to_bytes(match.group(1))

    # 2. Unused Volumes (docker volume prune)
    if options.get("unused_volumes"):
        cmd = "docker volume prune -f 2>&1"
        res = system_executor.execute_command(cmd)
        output.append(f"--- Volumes Clean ---\n{res}")
        match = re.search(r'Total reclaimed space:\s+([0-9.A-Z\s]+)', res, re.IGNORECASE)
        if match:
            freed_bytes += parse_size_to_bytes(match.group(1))

    # 3. Unused Images (docker image prune)
    if options.get("unused_images"):
        # -a entfernt alle ungenutzten Images, nicht nur dangling ones
        cmd = "docker image prune -a -f 2>&1"
        res = system_executor.execute_command(cmd)
        output.append(f"--- Images Clean ---\n{res}")
        match = re.search(r'Total reclaimed space:\s+([0-9.A-Z\s]+)', res, re.IGNORECASE)
        if match:
            freed_bytes += parse_size_to_bytes(match.group(1))

    # 4. Build Cache (docker builder prune)
    if options.get("build_cache"):
        cmd = "docker builder prune -f 2>&1"
        res = system_executor.execute_command(cmd)
        output.append(f"--- Build Cache Clean ---\n{res}")
        match = re.search(r'Total reclaimed space:\s+([0-9.A-Z\s]+)', res, re.IGNORECASE)
        if match:
            freed_bytes += parse_size_to_bytes(match.group(1))

    return {
        "success": True,
        "freed_bytes": freed_bytes,
        "formatted_freed": format_bytes(freed_bytes),
        "output": "\n\n".join(output)
    }
