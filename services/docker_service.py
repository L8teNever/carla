# ==============================================================
# CARLA – Docker Service
# Steuert und überwacht Docker-Container und Compose-Stacks lokal.
# ==============================================================

from . import system_executor
import requests


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
    target_ip = system_executor.get_host_ip()

    try:
        # OS-Info
        os_out = system_executor.execute_command(
            "grep '^PRETTY_NAME=' /etc/os-release | cut -d'=' -f2 | tr -d '\"'"
        )
        if os_out and "Error" not in os_out:
            result["os"] = os_out.strip()

        # Container-Info (ALLE Container inkl. gestoppte)
        # Tab-sep: Project | Name | Image | Ports | Status | RunningState
        cmd = "docker ps -a --format '{{.Label \"com.docker.compose.project\"}}\t{{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}\t{{.State}}'"
        out = system_executor.execute_command(cmd)
        
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 6:
                stack = parts[0].strip() or "Einzelne"
                name = parts[1].strip()
                img = parts[2].strip()
                ports = parts[3].strip()
                status_text = parts[4].strip()
                state = parts[5].strip() # running, exited, paused, etc.

                local_url = ""
                if "->" in ports:
                    try:
                        host_side = ports.split(",")[0].split("->")[0].strip()
                        ip, port = host_side.rsplit(":", 1) if ":" in host_side else (target_ip, host_side)
                        local_url = f"http://{target_ip if ip in ['0.0.0.0', '::', ''] else ip}:{port}"
                    except Exception:
                        pass

                if stack not in result["stacks"]:
                    result["stacks"][stack] = []

                result["stacks"][stack].append({
                    "name": name,
                    "image": img,
                    "local_url": local_url,
                    "ports_raw": ports,
                    "status_text": status_text,
                    "state": state,
                    "github": get_github_url(img, github_token),
                })
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


def stack_action(stack_name: str, action: str) -> str:
    """Fuehrt eine Aktion auf einem gesamten Compose-Stack aus."""
    allowed = ("start", "stop", "restart", "down", "update")
    if action not in allowed:
        return f"Unerlaubte Aktion: {action}"
    
    # Pfad zum Compose-File finden (Working Dir Label)
    workdir = system_executor.execute_command(
        f"docker ps -a --filter 'label=com.docker.compose.project={stack_name}' "
        f"--format '{{{{.Label \"com.docker.compose.project.working_dir\"}}}}' | head -1"
    ).strip()

    if not workdir or "Error" in workdir:
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
