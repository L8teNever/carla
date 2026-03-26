# ==============================================================
# CARLA – SSH / Docker Service
# Holt Container-Daten vom Server via SSH.
# ==============================================================

import paramiko
import requests


def get_github_url(image_full_name: str, github_token: str) -> str | None:
    """Prüft, ob ein Docker-Image auf einem GitHub-Repo basiert."""
    repo_path = image_full_name.split(":")[0]
    if "/" in repo_path:
        try:
            api_url = f"https://api.github.com/repos/{repo_path}"
            headers = {
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "CARLA-Monitor",
            }
            response = requests.get(api_url, headers=headers, timeout=2, verify=False)
            if response.status_code == 200:
                return f"https://github.com/{repo_path}"
        except Exception:
            pass
    return None


def fetch_docker_data(ssh_host: str, ssh_user: str, ssh_pass: str, github_token: str) -> dict:
    """
    Verbindet sich per SSH mit dem Server und liest alle laufenden Docker-Container aus.
    Gibt ein Dict zurück: { stacks: {...}, os: str, error: str|None }
    """
    result = {"stacks": {}, "os": "Unbekannt", "error": None}
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(ssh_host, username=ssh_user, password=ssh_pass, timeout=8)

        # OS-Info
        _, stdout, _ = ssh.exec_command(
            "grep '^PRETTY_NAME=' /etc/os-release | cut -d'=' -f2 | tr -d '\"'"
        )
        result["os"] = stdout.read().decode("utf-8").strip()

        # Container-Info
        cmd = "docker ps --format '{{.Label \"com.docker.compose.project\"}}\t{{.Names}}\t{{.Image}}\t{{.Ports}}'"
        _, stdout, _ = ssh.exec_command(cmd)
        lines = stdout.read().decode("utf-8").splitlines()

        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 3:
                stack = parts[0] or "Einzelne"
                name = parts[1]
                img = parts[2]
                ports = parts[3] if len(parts) > 3 else ""

                local_url = ""
                if "->" in ports:
                    host_side = ports.split(",")[0].split("->")[0].strip()
                    ip, port = (
                        host_side.rsplit(":", 1) if ":" in host_side else (ssh_host, host_side)
                    )
                    local_url = f"http://{ssh_host if ip in ['0.0.0.0', '::', ''] else ip}:{port}"

                if stack not in result["stacks"]:
                    result["stacks"][stack] = []

                result["stacks"][stack].append({
                    "name": name,
                    "image": img,
                    "local_url": local_url,
                    "ports_raw": ports,
                    "github": get_github_url(img, github_token),
                })

        ssh.close()
    except Exception as e:
        result["error"] = str(e)

    return result


def fetch_container_logs(ssh_host, ssh_user, ssh_pass, container_name, tail=100) -> str:
    """Holt die letzten X Zeilen der Docker-Logs via SSH."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ssh_host, username=ssh_user, password=ssh_pass, timeout=5)
        _, stdout, stderr = ssh.exec_command(f"docker logs --tail {tail} {container_name}")
        output = stdout.read().decode("utf-8", "ignore")
        errors = stderr.read().decode("utf-8", "ignore")
        ssh.close()
        return output if output else errors
    except Exception as e:
        return f"Fehler beim Laden der Logs: {e}"


def execute_container_command(ssh_host, ssh_user, ssh_pass, container_name, cmd) -> str:
    """Führt einen Befehl im Docker-Container aus."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ssh_host, username=ssh_user, password=ssh_pass, timeout=5)
        # Wir nutzen docker exec -t (tty-like)
        _, stdout, stderr = ssh.exec_command(f"docker exec {container_name} {cmd}")
        output = stdout.read().decode("utf-8", "ignore")
        errors = stderr.read().decode("utf-8", "ignore")
        ssh.close()
        return output if output else (errors if errors else "Befehl ausgeführt.")
    except Exception as e:
        return f"Exec-Fehler: {e}"
