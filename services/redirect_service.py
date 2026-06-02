# ==============================================================
# CARLA – Redirect Service
# Verwaltet kleine Webserver für URL-Weiterleitungen auf Ports.
# ==============================================================

import os
import re
from services import system_executor

BASE_DIR = "/opt/stacks"

def parse_nginx_config(content: str) -> list:
    """Parst die Weiterleitungsregeln aus einer nginx.conf."""
    pattern = r"location\s+([^\s{]+)\s*\{\s*return\s+(?:301|302)\s+([^;]+);\s*\}"
    rules = []
    for match in re.finditer(pattern, content):
        path = match.group(1).strip()
        url = match.group(2).strip()
        rules.append({"path": path, "url": url})
    return rules

def generate_nginx_config(rules: list) -> str:
    """Generiert den Inhalt der nginx.conf aus den Regeln."""
    config_lines = [
        "server {",
        "    listen 80;",
        "    server_name localhost;",
        ""
    ]
    # Regeln nach Pfadlänge absteigend sortieren, damit spezifischere Pfade zuerst deklariert werden
    sorted_rules = sorted(rules, key=lambda x: len(x.get('path', '')), reverse=True)
    
    for rule in sorted_rules:
        path = rule.get('path', '').strip()
        url = rule.get('url', '').strip()
        if not path or not url:
            continue
        # Protokoll erzwingen
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url
            
        config_lines.append(f"    location {path} {{")
        config_lines.append(f"        return 301 {url};")
        config_lines.append("    }")
        config_lines.append("")
        
    config_lines.append("}")
    return "\n".join(config_lines)

def generate_compose_config(port: int) -> str:
    """Generiert die docker-compose.yml für den Weiterleitungsserver."""
    return f"""version: '3.8'
services:
  nginx:
    image: nginx:alpine
    container_name: redirect-{port}
    restart: always
    ports:
      - "{port}:80"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
"""

def list_redirects() -> list:
    """Gibt eine Liste aller konfigurierten Weiterleitungsserver zurück."""
    redirects = []
    
    # Prüfen ob /opt/stacks existiert, wenn nicht, erstellen (oder über ls prüfen)
    ls_out = system_executor.execute_command("ls -1 /opt/stacks 2>/dev/null")
    if not ls_out or "Error" in ls_out:
        return redirects
        
    for name in ls_out.splitlines():
        name = name.strip()
        if name.startswith("redirect-"):
            port_str = name.split("-")[1]
            try:
                port = int(port_str)
            except ValueError:
                continue
                
            nginx_conf_path = f"/opt/stacks/{name}/nginx.conf"
            content = system_executor.execute_command(f"cat {nginx_conf_path} 2>/dev/null")
            
            rules = []
            if content and "Error" not in content and "No such file" not in content:
                rules = parse_nginx_config(content)
                
            # Status via Docker abfragen
            container_name = f"redirect-{port}"
            state_out = system_executor.execute_command(
                f"docker inspect --format '{{{{.State.Status}}}}' {container_name} 2>/dev/null"
            ).strip()
            
            state = "stopped"
            if state_out and "Error" not in state_out:
                state = state_out
                
            redirects.append({
                "port": port,
                "stack_name": name,
                "rules": rules,
                "state": state
            })
            
    # Sortieren nach Port
    redirects.sort(key=lambda x: x["port"])
    return redirects

def create_redirect(port: int, rules: list) -> dict:
    """Erstellt oder aktualisiert einen Weiterleitungsserver."""
    workdir = f"/opt/stacks/redirect-{port}"
    
    # Verzeichnis auf dem Host anlegen
    mkdir_res = system_executor.execute_command(f"mkdir -p {workdir}")
    if mkdir_res and "Error" in mkdir_res:
        return {"ok": False, "error": f"Verzeichnis konnte nicht erstellt werden: {mkdir_res}"}
        
    # Konfigurationen erzeugen
    nginx_content = generate_nginx_config(rules)
    compose_content = generate_compose_config(port)
    
    # Dateien via heredoc schreiben
    nginx_cmd = f"cat > {workdir}/nginx.conf << 'CARLA_EOF'\n{nginx_content}\nCARLA_EOF"
    compose_cmd = f"cat > {workdir}/docker-compose.yml << 'CARLA_EOF'\n{compose_content}\nCARLA_EOF"
    
    n_res = system_executor.execute_command(nginx_cmd)
    if n_res and "Error" in n_res:
        return {"ok": False, "error": f"Nginx-Config konnte nicht geschrieben werden: {n_res}"}
        
    c_res = system_executor.execute_command(compose_cmd)
    if c_res and "Error" in c_res:
        return {"ok": False, "error": f"docker-compose.yml konnte nicht geschrieben werden: {c_res}"}
        
    # Container neu starten (down & up), um Konfiguration anzuwenden
    up_res = system_executor.execute_command(f"cd {workdir} && docker compose down && docker compose up -d 2>&1")
    if up_res and "Error" in up_res and "unsupported" not in up_res.lower():
        if "fatal" in up_res.lower() or "failed" in up_res.lower():
            return {"ok": False, "error": f"Container-Start fehlgeschlagen: {up_res}"}
            
    return {"ok": True, "port": port}

def delete_redirect(port: int) -> dict:
    """Entfernt einen Weiterleitungsserver und stoppt die Container."""
    workdir = f"/opt/stacks/redirect-{port}"
    
    # Prüfen ob Verzeichnis existiert
    check = system_executor.execute_command(f"test -d {workdir} && echo 'OK' || echo 'NO'")
    if "NO" in check:
        return {"ok": True, "message": "Weiterleitungsserver existierte nicht."}
        
    # Container stoppen & löschen
    system_executor.execute_command(f"cd {workdir} && docker compose down 2>&1")
    rm_res = system_executor.execute_command(f"rm -rf {workdir}")
    
    if rm_res and "Error" in rm_res:
        return {"ok": False, "error": f"Ordner konnte nicht gelöscht werden: {rm_res}"}
        
    return {"ok": True}

def execute_action(port: int, action: str) -> dict:
    """Führt Aktionen (start/stop/restart) auf dem Redirect-Stack aus."""
    allowed = ("start", "stop", "restart")
    if action not in allowed:
        return {"ok": False, "error": f"Aktion nicht erlaubt: {action}"}
        
    workdir = f"/opt/stacks/redirect-{port}"
    check = system_executor.execute_command(f"test -d {workdir} && echo 'OK' || echo 'NO'")
    if "NO" in check:
        return {"ok": False, "error": "Weiterleitungsserver existiert nicht."}
        
    if action == "start":
        cmd = "docker compose up -d"
    elif action == "stop":
        cmd = "docker compose stop"
    elif action == "restart":
        cmd = "docker compose restart"
        
    res = system_executor.execute_command(f"cd {workdir} && {cmd} 2>&1")
    has_error = res and ("failed" in res.lower() or "fatal" in res.lower() or "error" in res.lower())
    
    return {"ok": not has_error, "output": res}
