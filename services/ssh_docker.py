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
            response = requests.get(api_url, headers=headers, timeout=2, verify=False)
            if response.status_code == 200:
                return f"https://github.com/{repo_path}"
        except Exception:
            pass
    return None


def fetch_docker_data(unused_host, unused_user, unused_pass, github_token: str) -> dict:
    """Holt Container-Daten via System-Executor."""
    result = {"stacks": {}, "os": "Unbekannt", "error": None}
    target_ip = system_executor.get_host_ip()

    try:
        # OS-Info
        os_name = system_executor.execute_command(
            "grep '^PRETTY_NAME=' /etc/os-release | cut -d'=' -f2 | tr -d '\"'"
        )
        if "Error" not in os_name: result["os"] = os_name

        # Container-Info
        cmd = "docker ps --format '{{.Label \"com.docker.compose.project\"}}\t{{.Names}}\t{{.Image}}\t{{.Ports}}'"
        out = system_executor.execute_command(cmd)
        
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                stack = parts[0] or "Einzelne"
                name = parts[1]
                img = parts[2]
                ports = parts[3] if len(parts) > 3 else ""

                local_url = ""
                if "->" in ports:
                    host_side = ports.split(",")[0].split("->")[0].strip()
                    ip, port = host_side.rsplit(":", 1) if ":" in host_side else (target_ip, host_side)
                    local_url = f"http://{target_ip if ip in ['0.0.0.0', '::', ''] else ip}:{port}"

                if stack not in result["stacks"]:
                    result["stacks"][stack] = []

                result["stacks"][stack].append({
                    "name": name,
                    "image": img,
                    "local_url": local_url,
                    "ports_raw": ports,
                    "github": get_github_url(img, github_token),
                })
    except Exception as e:
        result["error"] = str(e)

    return result


def fetch_container_logs(unused_host, unused_user, unused_pass, container_name, tail=100) -> str:
    """Holt die letzten X Zeilen der Docker-Logs."""
    return system_executor.execute_command(f"docker logs --tail {tail} {container_name}")


def execute_container_command(unused_host, unused_user, unused_pass, container_name, cmd) -> str:
    """Führt einen Befehl im Docker-Container aus."""
    return system_executor.execute_command(f"docker exec {container_name} {cmd}")

