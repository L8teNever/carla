# ==============================================================
# CARLA – Redirect Service
# Verwaltet kleine Webserver für URL-Weiterleitungen auf Ports.
# ==============================================================

import re
import json
import config
from urllib.parse import urlparse
from services import system_executor, error_server
from services.cloudflare import CloudflareClient

BASE_DIR = "/opt/stacks"

def _get_base_domain(hostname: str) -> str:
    parts = hostname.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname

def _ensure_error_server(cf_client, tunnel_id: str, host_ip: str, hostname: str) -> str | None:
    """Stellt den Error-Server sicher und richtet Cloudflare ein. Gibt die Error-Domain zurück."""
    base_domain = _get_base_domain(hostname)
    error_domain = f"error.{base_domain}"

    # Error-Server starten (error_server.py übernimmt Duplikat-Prüfung)
    error_server.ensure_server()

    # Cloudflare-Eintrag prüfen und ggf. anlegen
    if cf_client and tunnel_id:
        rules = cf_client.get_tunnel_ingress(tunnel_id)
        has_error_entry = any(r.get("hostname") == error_domain for r in rules)
        if not has_error_entry:
            non_catchall = [r for r in rules if not r.get("is_catchall") and r.get("hostname")]
            non_catchall.append({"hostname": error_domain, "service": f"http://{host_ip}:{error_server.ERROR_SERVER_PORT}"})
            non_catchall.append({"service": "http_status:404"})
            cf_client.update_tunnel_ingress(tunnel_id, non_catchall)

        zone_id = cf_client.find_zone_id(error_domain)
        if zone_id:
            dns_target = f"{tunnel_id}.cfargotunnel.com"
            cf_client.delete_cname_record(zone_id, error_domain)
            cf_client.create_cname_record(zone_id, error_domain, dns_target)

    return error_domain

def _get_cf_client():
    if config.CF_API_TOKEN and config.CF_ACCOUNT_ID:
        return CloudflareClient(config.CF_API_TOKEN, config.CF_ACCOUNT_ID)
    return None

def _cleanup_cloudflare(tunnel_id: str, hostname: str):
    cf_client = _get_cf_client()
    if not cf_client or not tunnel_id or not hostname:
        return
        
    # 1. DNS CNAME löschen
    zone_id = cf_client.find_zone_id(hostname)
    if zone_id:
        cf_client.delete_cname_record(zone_id, hostname)
    cf_client.delete_access_app_by_domain(hostname)
        
    # 2. Ingress-Regel aus dem Tunnel löschen
    existing_rules = cf_client.get_tunnel_ingress(tunnel_id)
    new_ingress = []
    catchall_service = "http_status:404"
    for r in existing_rules:
        if r.get("is_catchall") or not r.get("hostname"):
            catchall_service = r.get("service", "http_status:404")
            continue
        if r.get("hostname") != hostname:
            new_ingress.append({
                "hostname": r["hostname"],
                "service": r["service"]
            })
            
    # Füge Catch-All am Ende wieder hinzu
    new_ingress.append({
        "is_catchall": True,
        "service": catchall_service
    })
    
    # Ingress aktualisieren
    cf_client.update_tunnel_ingress(tunnel_id, new_ingress)

def parse_nginx_config(content: str) -> list:
    """Parst die Weiterleitungsregeln aus einer nginx.conf."""
    pattern = r"location\s+([^\s{]+)\s*\{\s*return\s+(?:301|302)\s+([^;]+);\s*\}"
    rules = []
    for match in re.finditer(pattern, content):
        path = match.group(1).strip()
        url = match.group(2).strip()
        rules.append({"path": path, "url": url})
    return rules

def generate_nginx_config(rules: list, port: int = 80, error_domain: str = None) -> str:
    """Generiert den Inhalt der nginx.conf aus den Regeln."""
    config_lines = [
        "server {",
        f"    listen {port};",
        "    server_name localhost;",
        ""
    ]
    sorted_rules = sorted(rules, key=lambda x: len(x.get('path', '')), reverse=True)

    for rule in sorted_rules:
        path = rule.get('path', '').strip()
        url = rule.get('url', '').strip()
        if not path or not url:
            continue
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url
        config_lines.append(f"    location {path} {{")
        config_lines.append(f"        return 301 {url};")
        config_lines.append("    }")
        config_lines.append("")

    if error_domain:
        config_lines.append("    location / {")
        config_lines.append(f"        return 302 https://{error_domain}/404;")
        config_lines.append("    }")
        config_lines.append("")

    config_lines.append("}")
    return "\n".join(config_lines)

def generate_compose_config(port: int) -> str:
    """Generiert die docker-compose.yml für den Weiterleitungsserver."""
    return f"""services:
  nginx:
    image: nginx:alpine
    container_name: redirect-{port}
    restart: always
    network_mode: host
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
                
            # Metadata lesen
            metadata_path = f"/opt/stacks/{name}/metadata.json"
            meta_content = system_executor.execute_command(f"cat {metadata_path} 2>/dev/null")
            
            rules = []
            cloudflare_meta = {"enabled": False}
            
            if meta_content and "Error" not in meta_content and "No such file" not in meta_content:
                try:
                    meta = json.loads(meta_content)
                    rules = meta.get("rules", [])
                    cloudflare_meta = meta.get("cloudflare", {"enabled": False})
                except Exception:
                    pass
            
            # Fallback wenn metadata.json fehlt/ungültig
            if not rules:
                nginx_conf_path = f"/opt/stacks/{name}/nginx.conf"
                content = system_executor.execute_command(f"cat {nginx_conf_path} 2>/dev/null")
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
                "cloudflare": cloudflare_meta,
                "state": state
            })
            
    # Sortieren nach Port
    redirects.sort(key=lambda x: x["port"])
    return redirects

def create_redirect(port: int, rules: list, cloudflare_data: dict = None) -> dict:
    """Erstellt oder aktualisiert einen Weiterleitungsserver."""
    workdir = f"/opt/stacks/redirect-{port}"
    cloudflare_data = cloudflare_data or {"enabled": False}
    
    # 1. Altes Metadata-Backup & Cleanup falls vorhanden
    old_metadata = {}
    metadata_path = f"{workdir}/metadata.json"
    meta_content = system_executor.execute_command(f"cat {metadata_path} 2>/dev/null")
    if meta_content and "Error" not in meta_content and "No such file" not in meta_content:
        try:
            old_metadata = json.loads(meta_content)
        except Exception:
            pass
            
    old_cf = old_metadata.get("cloudflare", {})
    if old_cf.get("enabled"):
        try:
            _cleanup_cloudflare(old_cf.get("tunnel_id"), old_cf.get("hostname"))
        except Exception as e:
            # Fehler protokollieren aber fortfahren, um Deadlocks zu vermeiden
            print(f"[CARLA] Cloudflare Cleanup Fehler: {e}")

    # 2. Cloudflare einrichten falls aktiviert
    if cloudflare_data.get("enabled"):
        cf_client = _get_cf_client()
        if not cf_client:
            return {"ok": False, "error": "Cloudflare API-Token oder Account-ID nicht konfiguriert."}
            
        tunnel_id = cloudflare_data.get("tunnel_id")
        hostname = cloudflare_data.get("hostname")
        if not tunnel_id or not hostname:
            return {"ok": False, "error": "Tunnel-ID und Hostname sind erforderlich."}
            
        # Zone finden
        zone_id = cf_client.find_zone_id(hostname)
        if not zone_id:
            return {"ok": False, "error": f"Keine passende Cloudflare DNS-Zone für den Hostname '{hostname}' gefunden."}
            
        # Host-IP aus bestehenden Ingress-Regeln ableiten
        existing_rules = cf_client.get_tunnel_ingress(tunnel_id)
        host_ip = "localhost"
        for r in existing_rules:
            svc = r.get("service", "")
            if svc.startswith("http://") and not r.get("is_catchall"):
                parsed = urlparse(svc)
                if parsed.hostname and parsed.hostname not in ("localhost", "127.0.0.1"):
                    host_ip = parsed.hostname
                    break

        new_ingress = []
        found_existing = False

        for r in existing_rules:
            if r.get("is_catchall") or not r.get("hostname"):
                continue
            if r.get("hostname") == hostname:
                new_ingress.append({
                    "hostname": hostname,
                    "service": f"http://{host_ip}:{port}"
                })
                found_existing = True
            else:
                new_ingress.append({
                    "hostname": r["hostname"],
                    "service": r["service"]
                })

        if not found_existing:
            new_ingress.append({
                "hostname": hostname,
                "service": f"http://{host_ip}:{port}"
            })
            
        # Finde alte Catch-All-Regel
        catchall_service = "http_status:404"
        for r in existing_rules:
            if r.get("is_catchall"):
                catchall_service = r.get("service", "http_status:404")
                break
                
        new_ingress.append({
            "is_catchall": True,
            "service": catchall_service
        })
        
        up_res = cf_client.update_tunnel_ingress(tunnel_id, new_ingress)
        if not up_res.get("success"):
            return {"ok": False, "error": f"Cloudflare-Tunnel konnte nicht aktualisiert werden: {up_res.get('errors')}"}

        # CNAME erstellen
        dns_target = f"{tunnel_id}.cfargotunnel.com"
        cf_client.delete_cname_record(zone_id, hostname)
        dns_res = cf_client.create_cname_record(zone_id, hostname, dns_target)
        if not dns_res.get("success"):
            return {"ok": False, "error": f"Cloudflare DNS-Eintrag konnte nicht erstellt werden: {dns_res.get('errors')}"}

        # Error-Server sicherstellen (einmalig, kein Duplikat)
        error_domain = _ensure_error_server(cf_client, tunnel_id, host_ip, hostname)
    else:
        error_domain = None

    # 3. Verzeichnis auf dem Host anlegen
    mkdir_res = system_executor.execute_command(f"mkdir -p {workdir}")
    if mkdir_res and "Error" in mkdir_res:
        return {"ok": False, "error": f"Verzeichnis konnte nicht erstellt werden: {mkdir_res}"}
        
    # Konfigurationen erzeugen
    nginx_content = generate_nginx_config(rules, port, error_domain)
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
        
    # metadata.json schreiben
    metadata = {
        "port": port,
        "rules": rules,
        "cloudflare": cloudflare_data
    }
    metadata_str = json.dumps(metadata, indent=2)
    metadata_cmd = f"cat > {workdir}/metadata.json << 'CARLA_EOF'\n{metadata_str}\nCARLA_EOF"
    system_executor.execute_command(metadata_cmd)
        
    # Container neu starten (down & up), um Konfiguration anzuwenden
    # Timeout 120s wegen möglichem Image-Pull bei erstem Start
    up_res = system_executor.execute_command(f"cd {workdir} && docker compose down && docker compose up -d 2>&1", timeout=120)
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
        
    # 1. Metadata auslesen für Cloudflare Cleanup
    metadata_path = f"{workdir}/metadata.json"
    meta_content = system_executor.execute_command(f"cat {metadata_path} 2>/dev/null")
    if meta_content and "Error" not in meta_content and "No such file" not in meta_content:
        try:
            meta = json.loads(meta_content)
            cf = meta.get("cloudflare", {})
            if cf.get("enabled"):
                _cleanup_cloudflare(cf.get("tunnel_id"), cf.get("hostname"))
        except Exception as e:
            print(f"[CARLA] Cloudflare Cleanup bei Löschung fehlgeschlagen: {e}")
            
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
