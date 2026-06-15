# ==============================================================
# CARLA – Docker Service
# Steuert und überwacht Docker-Container und Compose-Stacks lokal.
# ==============================================================

from . import system_executor
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed


def get_github_url(image_full_name: str, github_token: str) -> str | None:
    repo_path = image_full_name.split(":")[0]
    if "/" in repo_path:
        try:
            api_url = f"https://api.github.com/repos/{repo_path}"
            headers = {
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json"
            }
            # verify=False ist beibehalten wegen urllib3 Deaktivierung in main
            response = requests.get(api_url, headers=headers, timeout=2, verify=False)
            if response.status_code == 200:
                return f"https://github.com/{repo_path}"
        except Exception:
            pass
    return None


def fetch_docker_data(github_token: str) -> dict:
    """Holt lokale Container-Daten."""
    result = {"stacks": {}, "os": "Unbekannt", "error": None}

    try:
        # OS-Info
        os_out = system_executor.execute_command(
            "grep '^PRETTY_NAME=' /etc/os-release | cut -d'=' -f2 | tr -d '\"'"
        )
        if os_out and "Error" not in os_out:
            result["os"] = os_out.strip()

        # Bekannte Stack-Namen aus /opt/stacks ermitteln
        known_stacks = []
        try:
            ls_out = system_executor.execute_command("ls -1 /opt/stacks 2>/dev/null")
            if ls_out and "Error" not in ls_out and "not found" not in ls_out.lower():
                known_stacks = [d.strip() for d in ls_out.splitlines() if d.strip()]
        except Exception:
            pass

        # Container-Info (ALLE Container inkl. gestoppte)
        # Tab-sep: Project | Name | Image | Ports | Status | RunningState
        cmd = "docker ps -a --format '{{.Label \"com.docker.compose.project\"}}\t{{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}\t{{.State}}'"
        out = system_executor.execute_command(cmd)

        # Container network IPs holen
        ip_map = {}
        try:
            ids_out = system_executor.execute_command("docker ps -aq")
            ids = [i.strip() for i in ids_out.splitlines() if i.strip()]
            if ids:
                inspect_cmd = f"docker inspect --format '{{{{.Name}}}}\\t{{{{range .NetworkSettings.Networks}}}}{{{{.IPAddress}}}} {{{{end}}}}' {' '.join(ids)}"
                inspect_out = system_executor.execute_command(inspect_cmd)
                for line in inspect_out.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        c_name = parts[0].strip().lstrip("/")
                        ips = [ip.strip() for ip in parts[1].split() if ip.strip()]
                        ip_map[c_name] = ips
        except Exception as e:
            print(f"[Docker Service] Fehler beim Holen der IPs: {e}")

        containers = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 6:
                stack = parts[0].strip()
                name = parts[1].strip()
                img = parts[2].strip()
                ports = parts[3].strip()
                status_text = parts[4].strip()
                state = parts[5].strip()

                # Robustes matching fuer Stacks falls Label leer oder "Einzelne"
                if not stack or stack.lower() == "einzelne":
                    matched_stack = None
                    for ks in known_stacks:
                        if name == ks:
                            matched_stack = ks
                            break
                    if not matched_stack:
                        for ks in known_stacks:
                            if name.startswith(f"{ks}_") or name.startswith(f"{ks}-"):
                                matched_stack = ks
                                break
                    stack = matched_stack or "Einzelne"
                else:
                    # Case-insensitive Matching mit bekannten Stacks zur Casing-Normalisierung
                    for ks in known_stacks:
                        if stack.lower() == ks.lower():
                            stack = ks
                            break

                # Alle Port-Bindings als exakte IP:PORT Paare speichern
                port_bindings = []  # [{"host_ip": "10.7.0.1", "host_port": 1014}, ...]
                local_url = ""
                if "->" in ports:
                    seen_bindings = set()
                    for mapping in ports.split(","):
                        mapping = mapping.strip()
                        if "->" not in mapping:
                            continue
                        try:
                            host_side, _ = mapping.split("->")
                            host_side = host_side.strip()
                            if ":" in host_side:
                                host_ip, hp_str = host_side.rsplit(":", 1)
                            else:
                                host_ip, hp_str = "", host_side
                            hp = int(hp_str)
                            key = (host_ip, hp)
                            if key not in seen_bindings:
                                seen_bindings.add(key)
                                port_bindings.append({"host_ip": host_ip, "host_port": hp})
                        except Exception:
                            pass
                    if port_bindings:
                        b = port_bindings[0]
                        local_url = f"http://{b['host_ip']}:{b['host_port']}" if b['host_ip'] else f"http://localhost:{b['host_port']}"

                containers.append({
                    "stack": stack,
                    "name": name,
                    "image": img,
                    "local_url": local_url,
                    "port_bindings": port_bindings,
                    "ports_raw": ports,
                    "status_text": status_text,
                    "state": state,
                    "internal_ips": ip_map.get(name, [])
                })

        # GitHub-URLs parallel abrufen
        unique_images = list({c["image"] for c in containers})
        github_map = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(get_github_url, img, github_token): img for img in unique_images}
            for future in as_completed(futures):
                img = futures[future]
                github_map[img] = future.result()

        for c in containers:
            stack = c.pop("stack")
            c["github"] = github_map.get(c["image"])
            if stack not in result["stacks"]:
                result["stacks"][stack] = []
            result["stacks"][stack].append(c)

    except Exception as e:
        result["error"] = str(e)

    return result


def fetch_container_logs(container_name: str, tail=100) -> str:
    """Holt die letzten X Zeilen der Docker-Logs."""
    return system_executor.execute_command(f"docker logs --tail {tail} {container_name}")


def execute_container_command(container_name: str, cmd: str) -> str:
    """Führt einen Befehl im Docker-Container aus."""
    return system_executor.execute_command(f"docker exec {container_name} {cmd}")


def container_action(container_name: str, action: str) -> str:
    """Fuehrt eine Docker-Aktion auf einem Container aus (start/stop/restart/pause/unpause)."""
    allowed = ("start", "stop", "restart", "pause", "unpause")
    if action not in allowed:
        return f"Unerlaubte Aktion: {action}"
    return system_executor.execute_command(f"docker {action} {container_name}")


def resolve_stack_workdir(stack_name: str) -> str:
    """Findet das Arbeitsverzeichnis eines Stacks auf dem Docker-Host mit Fallbacks."""
    # 1. Label abfragen
    workdir = system_executor.execute_command(
        f"docker ps -a --filter 'label=com.docker.compose.project={stack_name}' "
        f"--format '{{{{.Label \"com.docker.compose.project.working_dir\"}}}}' | head -1"
    ).strip()

    possible_dirs = []
    if workdir and "Error" not in workdir:
        possible_dirs.append(workdir)

    # Standard-Pfade unter /opt/stacks probieren
    possible_dirs.append(f"/opt/stacks/{stack_name}")
    possible_dirs.append(f"/opt/stacks/{stack_name.replace('_', '-')}")
    possible_dirs.append(f"/opt/stacks/{stack_name.replace('-', '_')}")

    # Den ersten Pfad nehmen, der tatsächlich ein existierendes Verzeichnis ist
    for d in possible_dirs:
        check = system_executor.execute_command(f'test -d "{d}" && echo "OK" || echo "NO"')
        if "OK" in check:
            return d

    # Fallback auf ersten ermittelten Pfad
    return possible_dirs[0] if possible_dirs else f"/opt/stacks/{stack_name}"


def stack_action(stack_name: str, action: str) -> str:
    """Fuehrt eine Aktion auf einem gesamten Compose-Stack aus."""
    allowed = ("start", "stop", "restart", "down", "update")
    if action not in allowed:
        return f"Unerlaubte Aktion: {action}"
    
    # Pfad zum Compose-File finden (mit Fallbacks)
    workdir = resolve_stack_workdir(stack_name)

    if not workdir:
        return f"Fehler: Working Directory fuer Stack '{stack_name}' nicht gefunden."

    if action == "restart":
        cmd = f"cd {workdir} && docker compose restart 2>&1"
    elif action == "stop":
        cmd = f"cd {workdir} && docker compose stop 2>&1"
    elif action == "start":
        cmd = f"cd {workdir} && docker compose up -d 2>&1"
    elif action == "down":
        cmd = f"cd {workdir} && docker compose down 2>&1"
    elif action == "update":
        cmd = f"cd {workdir} && docker compose pull && docker compose up -d 2>&1"
    else:
        return "Unbekannte Aktion"

    cmd_timeout = 120 if action == "update" else 60
    return system_executor.execute_command(cmd, timeout=cmd_timeout)


def deploy_stack(stack_name: str, compose_content: str, env_content: str = "") -> dict:
    """Erstellt einen neuen Stack aus einer docker-compose.yml."""
    base_dir = "/opt/stacks"
    workdir = f"{base_dir}/{stack_name}"

    # Verzeichnis erstellen
    result = system_executor.execute_command(f"mkdir -p {workdir}")
    if result and "Error" in result:
        return {"ok": False, "error": f"Verzeichnis konnte nicht erstellt werden: {result}"}

    # Compose-Datei schreiben (via heredoc)
    write_cmd = f"cat > {workdir}/docker-compose.yml << 'CARLA_EOF'\n{compose_content}\nCARLA_EOF"
    result = system_executor.execute_command(write_cmd)
    if result and "Error" in result:
        return {"ok": False, "error": f"Compose-Datei konnte nicht geschrieben werden: {result}"}

    # Optional .env schreiben
    if env_content and env_content.strip():
        env_cmd = f"cat > {workdir}/.env << 'CARLA_EOF'\n{env_content}\nCARLA_EOF"
        result = system_executor.execute_command(env_cmd)
        if result and "Error" in result:
            return {"ok": False, "error": f".env konnte nicht geschrieben werden: {result}"}

    # Stack starten
    up_result = system_executor.execute_command(f"cd {workdir} && docker compose up -d 2>&1")

    return {
        "ok": True,
        "output": up_result,
        "workdir": workdir,
        "stack_name": stack_name,
    }


def fetch_container_logs_since_last_start(container_name: str) -> str:
    """Holt alle Logs seit dem letzten Start des Containers."""
    started_at = system_executor.execute_command(
        f"docker inspect --format '{{{{.State.StartedAt}}}}' {container_name}"
    ).strip()
    
    if not started_at or "Error" in started_at:
        return "Konnte Startzeitpunkt nicht ermitteln."

    return system_executor.execute_command(f"docker logs --since {started_at} {container_name}")


def get_docker_networks() -> list:
    """Holt alle Docker Netzwerke mit Subnetzen und verbundenen Containern."""
    import json
    from . import system_executor

    # Holen aller Netzwerk-IDs
    ids_out = system_executor.execute_command("docker network ls -q")
    if not ids_out or "Error" in ids_out or "falsch geschrieben" in ids_out or "not found" in ids_out.lower():
        # Fallback fuer Mock/Windows
        return [
            {
                "id": "bridge-id-123",
                "name": "bridge",
                "driver": "bridge",
                "scope": "local",
                "internal": False,
                "attachable": True,
                "subnets": ["172.17.0.0/16"],
                "gateways": ["172.17.0.1"],
                "containers": [
                    {"name": "carla-web", "ip": "172.17.0.2", "mac": "02:42:ac:11:00:02"}
                ]
            },
            {
                "id": "host-id-456",
                "name": "host",
                "driver": "host",
                "scope": "local",
                "internal": False,
                "attachable": False,
                "subnets": [],
                "gateways": [],
                "containers": []
            },
            {
                "id": "overlay-id-789",
                "name": "my-overlay-net",
                "driver": "overlay",
                "scope": "swarm",
                "internal": True,
                "attachable": True,
                "subnets": ["10.0.1.0/24"],
                "gateways": ["10.0.1.1"],
                "containers": [
                    {"name": "nginx-ingress", "ip": "10.0.1.5", "mac": "02:42:0a:00:01:05"},
                    {"name": "app-backend", "ip": "10.0.1.12", "mac": "02:42:0a:00:01:0c"}
                ]
            }
        ]

    ids = [i.strip() for i in ids_out.splitlines() if i.strip()]
    if not ids:
        return []

    inspect_cmd = f"docker network inspect {' '.join(ids)}"
    inspect_out = system_executor.execute_command(inspect_cmd)
    try:
        data = json.loads(inspect_out)
    except Exception as e:
        print(f"[Docker Service] Fehler beim Parsen von network inspect: {e}")
        return []

    networks = []
    for net in data:
        subnets = []
        gateways = []
        ipam_config = net.get("IPAM", {}).get("Config") or []
        for cfg in ipam_config:
            if "Subnet" in cfg:
                subnets.append(cfg["Subnet"])
            if "Gateway" in cfg:
                gateways.append(cfg["Gateway"])

        containers = []
        containers_dict = net.get("Containers") or {}
        for c_id, c_info in containers_dict.items():
            c_name = c_info.get("Name", "")
            c_name = c_name.lstrip("/")
            c_ip = c_info.get("IPv4Address", "")
            if "/" in c_ip:
                c_ip = c_ip.split("/")[0]
            if not c_ip:
                c_ip = c_info.get("IPv6Address", "")
                if "/" in c_ip:
                    c_ip = c_ip.split("/")[0]

            containers.append({
                "name": c_name,
                "ip": c_ip or "None",
                "mac": c_info.get("MacAddress", "None")
            })

        containers.sort(key=lambda x: x["name"].lower())

        networks.append({
            "id": net.get("Id", "")[:12],
            "full_id": net.get("Id", ""),
            "name": net.get("Name", ""),
            "driver": net.get("Driver", ""),
            "scope": net.get("Scope", ""),
            "internal": net.get("Internal", False),
            "attachable": net.get("Attachable", False),
            "subnets": subnets,
            "gateways": gateways,
            "containers": containers
        })

    networks.sort(key=lambda x: x["name"].lower())
    return networks
