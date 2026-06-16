# ==============================================================
# CARLA – System Executor
# Führt Shell-Befehle lokal aus.
# ==============================================================
import subprocess
import os
import sys

def execute_command(cmd: str, timeout: int = 15) -> str:
    """Führt einen Shell-Befehl lokal aus (mit Windows-Kompatibilitätsschicht für Entwickler)."""
    # Windows Kompatibilitätsschicht
    if sys.platform == "win32":
        cmd_clean = cmd.strip()
        
        # Mock docker commands if docker is not installed or running
        if cmd_clean.startswith("docker ps -a") or cmd_clean.startswith("docker ps --format"):
            # Mock container data
            return "carla\tcarla-app\tnginx:alpine\t0.0.0.0:80->80/tcp\tUp 2 hours\trunning\n" \
                   "carla\tcarla-db\tpostgres:15\t5432/tcp\tUp 2 hours\trunning\n"
        
        if cmd_clean.startswith("docker ps -aq"):
            return "carla-app-id\ncarla-db-id"
            
        if cmd_clean.startswith("docker inspect"):
            # If checking for container update
            if "carla-app" in cmd_clean:
                return '[{"Config": {"Image": "nginx:alpine", "Labels": {"com.docker.compose.project": "carla", "com.docker.compose.service": "app", "com.docker.compose.project.working_dir": "/opt/stacks/carla"}}}]'
            if "carla-db" in cmd_clean:
                return '[{"Config": {"Image": "postgres:15", "Labels": {"com.docker.compose.project": "carla", "com.docker.compose.service": "db", "com.docker.compose.project.working_dir": "/opt/stacks/carla"}}}]'
            return "[]"
            
        if cmd_clean.startswith("docker volume ls"):
            return ""

        if cmd_clean.startswith("ls -1 /opt/stacks"):
            return "carla\n"

        if "test -d" in cmd_clean:
            # e.g., test -d "/opt/stacks/carla" && echo "OK" || echo "NO"
            path = ""
            if '"' in cmd_clean:
                parts = cmd_clean.split('"')
                if len(parts) > 1:
                    path = parts[1]
            elif "'" in cmd_clean:
                parts = cmd_clean.split("'")
                if len(parts) > 1:
                    path = parts[1]
            if not path:
                for p in cmd_clean.split():
                    if p.startswith("/opt/"):
                        path = p
                        break
            if path:
                win_path = path.replace("/opt/", "C:/opt/")
                if os.path.isdir(win_path) or path == "/opt/stacks/carla":
                    return "OK"
            return "NO"

        if "test -f" in cmd_clean:
            # e.g., test -f "/opt/stacks/carla/docker-compose.yml" && echo "OK" || echo "NO"
            path = ""
            if '"' in cmd_clean:
                parts = cmd_clean.split('"')
                if len(parts) > 1:
                    path = parts[1]
            elif "'" in cmd_clean:
                parts = cmd_clean.split("'")
                if len(parts) > 1:
                    path = parts[1]
            if not path:
                for p in cmd_clean.split():
                    if p.startswith("/opt/"):
                        path = p
                        break
            if path:
                win_path = path.replace("/opt/", "C:/opt/")
                if os.path.isfile(win_path) or "docker-compose" in path:
                    return "OK"
            return "NO"

        if cmd_clean.startswith("cat "):
            # e.g. cat "/opt/stacks/carla/docker-compose.yml"
            path = cmd_clean.split("cat ")[1].strip().strip('"').strip("'")
            win_path = path.replace("/opt/", "C:/opt/")
            if os.path.isfile(win_path):
                try:
                    with open(win_path, "r", encoding="utf-8") as f:
                        return f.read()
                except Exception:
                    pass
            # Default mock compose if not found
            if "docker-compose" in path:
                return """version: '3.8'
services:
  app:
    image: nginx:alpine
    ports:
      - "80:80"
  db:
    image: postgres:15
"""
            if ".env" in path:
                return "DB_PASSWORD=secret"
            return "File not found"

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return result.stderr.strip()
    except Exception as e:
        return f"Error: {e}"

def get_host_ip():
    """Gibt die IP der Zielmaschine zurück (lokal immer localhost)."""
    return "localhost"
