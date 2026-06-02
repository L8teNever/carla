# ==============================================================
# CARLA – Static Server Service
# Verwaltet nginx-Container für statische Websites.
# Dateien liegen in /opt/stacks/carla-site-{name}/www/
# und sind über den File Manager bearbeitbar.
# ==============================================================

import json
import base64
import re
from urllib.parse import urlparse
from services import system_executor
from services.cloudflare import CloudflareClient
import config

BASE_DIR = "/opt/stacks"
SITE_PREFIX = "carla-site-"
PORT_RANGE_START = 10100
PORT_RANGE_END = 10999


def _find_free_port() -> int:
    """Findet einen freien Port im reservierten Bereich 10100-10999."""
    out = system_executor.execute_command(
        "ss -tlnp 2>/dev/null | awk 'NR>1 {print $4}' | grep -oE '[0-9]+$'"
    )
    used = set()
    for line in out.splitlines():
        try:
            used.add(int(line.strip()))
        except ValueError:
            pass
    for s in list_sites():
        if s.get("port"):
            used.add(s["port"])
    for port in range(PORT_RANGE_START, PORT_RANGE_END):
        if port not in used:
            return port
    raise RuntimeError("Keine freien Ports im Bereich 10100-10999 verfügbar.")


def _site_dir(name: str) -> str:
    return f"{BASE_DIR}/{SITE_PREFIX}{name}"


def _load_meta(name: str) -> dict:
    path = f"{_site_dir(name)}/metadata.json"
    content = system_executor.execute_command(f"cat {path} 2>/dev/null")
    if content and "Error" not in content and "No such file" not in content:
        try:
            return json.loads(content)
        except Exception:
            pass
    return {}


def _save_meta(name: str, meta: dict):
    data = json.dumps(meta, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(data.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {_site_dir(name)}/metadata.json")


def _get_cf_client():
    if config.CF_API_TOKEN and config.CF_ACCOUNT_ID:
        return CloudflareClient(config.CF_API_TOKEN, config.CF_ACCOUNT_ID)
    return None


def _get_host_ip_from_tunnel(tunnel_id: str, cf_client) -> str:
    rules = cf_client.get_tunnel_ingress(tunnel_id)
    for r in rules:
        svc = r.get("service", "")
        if svc.startswith("http://") and not r.get("is_catchall"):
            parsed = urlparse(svc)
            if parsed.hostname and parsed.hostname not in ("localhost", "127.0.0.1"):
                return parsed.hostname
    return "10.7.0.1"


def _generate_nginx_config(port: int, spa: bool = False) -> str:
    fallback = "try_files $uri $uri/ /index.html;" if spa else "try_files $uri $uri/ =404;"
    return f"""server {{
    listen {port};
    server_name localhost;
    root /var/www;
    index index.html index.htm;
    charset utf-8;

    location / {{
        {fallback}
    }}

    location ~* \\.(css|js|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {{
        expires 7d;
        add_header Cache-Control "public";
    }}
}}"""


def _generate_compose(name: str) -> str:
    www_dir = f"{_site_dir(name)}/www"
    nginx_conf = f"{_site_dir(name)}/nginx.conf"
    return f"""services:
  nginx:
    image: nginx:alpine
    container_name: {SITE_PREFIX}{name}
    restart: always
    network_mode: host
    volumes:
      - {nginx_conf}:/etc/nginx/conf.d/default.conf:ro
      - {www_dir}:/var/www:ro
"""


def _default_index_html(name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d0d0d;color:#ccc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{text-align:center;padding:2rem}}
h1{{font-size:2.5rem;color:#7c3aed;margin-bottom:1rem}}
p{{color:#888;font-size:1rem;line-height:1.6}}
code{{background:#1a1a1a;padding:.2rem .5rem;border-radius:4px;font-size:.9rem;color:#a78bfa}}
</style>
</head>
<body>
<div class="box">
<h1>{name}</h1>
<p>Deine Website ist bereit.<br>
Bearbeite die Dateien unter <code>/opt/stacks/carla-site-{name}/www/</code><br>
über den CARLA File Manager.</p>
</div>
</body>
</html>"""


def list_sites() -> list:
    ls = system_executor.execute_command(f"ls -1 {BASE_DIR} 2>/dev/null")
    if not ls or "Error" in ls:
        return []

    sites = []
    for entry in ls.splitlines():
        entry = entry.strip()
        if not entry.startswith(SITE_PREFIX):
            continue
        name = entry[len(SITE_PREFIX):]
        if not name:
            continue

        meta = _load_meta(name)
        container = f"{SITE_PREFIX}{name}"
        state = system_executor.execute_command(
            f"docker inspect --format '{{{{.State.Status}}}}' {container} 2>/dev/null"
        ).strip()

        sites.append({
            "name": name,
            "port": meta.get("port"),
            "domain": meta.get("domain"),
            "cloudflare": meta.get("cloudflare", {"enabled": False}),
            "spa": meta.get("spa", False),
            "state": state if state and "Error" not in state else "stopped",
            "www_path": f"{_site_dir(name)}/www",
        })

    sites.sort(key=lambda x: x["name"])
    return sites


def create_site(name: str, port: int = None, spa: bool = False, cloudflare_data: dict = None) -> dict:
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        return {"ok": False, "error": "Name darf nur Buchstaben, Zahlen, - und _ enthalten."}

    site_dir = _site_dir(name)
    www_dir = f"{site_dir}/www"

    # Prüfen ob bereits existiert
    check = system_executor.execute_command(f"test -d {site_dir} && echo 'EXISTS' || echo 'NEW'")
    if "EXISTS" in check:
        return {"ok": False, "error": f"Site '{name}' existiert bereits."}

    # Port automatisch vergeben wenn nicht angegeben
    if port is None:
        try:
            port = _find_free_port()
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}

    cloudflare_data = cloudflare_data or {"enabled": False}

    # Cloudflare einrichten
    hostname = None
    tunnel_id = None
    if cloudflare_data.get("enabled"):
        cf = _get_cf_client()
        if not cf:
            return {"ok": False, "error": "Cloudflare nicht konfiguriert."}

        tunnel_id = cloudflare_data.get("tunnel_id")
        hostname = cloudflare_data.get("hostname", "").strip()
        if not tunnel_id or not hostname:
            return {"ok": False, "error": "Tunnel-ID und Hostname erforderlich."}

        zone_id = cf.find_zone_id(hostname)
        if not zone_id:
            return {"ok": False, "error": f"Keine Cloudflare-Zone für '{hostname}' gefunden."}

        host_ip = _get_host_ip_from_tunnel(tunnel_id, cf)

        # Ingress-Regel hinzufügen
        rules = cf.get_tunnel_ingress(tunnel_id)
        non_catchall = [r for r in rules if not r.get("is_catchall") and r.get("hostname")]
        if not any(r["hostname"] == hostname for r in non_catchall):
            non_catchall.append({"hostname": hostname, "service": f"http://{host_ip}:{port}"})
        catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
        non_catchall.append(catchall)
        res = cf.update_tunnel_ingress(tunnel_id, non_catchall)
        if not res.get("success"):
            return {"ok": False, "error": f"Tunnel konnte nicht aktualisiert werden: {res.get('errors')}"}

        # CNAME anlegen
        cf.delete_cname_record(zone_id, hostname)
        dns_res = cf.create_cname_record(zone_id, hostname, f"{tunnel_id}.cfargotunnel.com")
        if not dns_res.get("success"):
            return {"ok": False, "error": f"DNS-Eintrag konnte nicht erstellt werden: {dns_res.get('errors')}"}

    # Verzeichnisse erstellen
    system_executor.execute_command(f"mkdir -p {www_dir}")

    # Standard index.html anlegen
    html = _default_index_html(name)
    encoded = base64.b64encode(html.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {www_dir}/index.html")

    # nginx.conf schreiben
    nginx_conf = _generate_nginx_config(port, spa)
    encoded = base64.b64encode(nginx_conf.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {site_dir}/nginx.conf")

    # docker-compose.yml schreiben
    compose = _generate_compose(name)
    encoded = base64.b64encode(compose.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {site_dir}/docker-compose.yml")

    # Metadata speichern
    meta = {
        "name": name,
        "port": port,
        "spa": spa,
        "domain": hostname,
        "cloudflare": cloudflare_data,
    }
    _save_meta(name, meta)

    # Container starten
    result = system_executor.execute_command(
        f"cd {site_dir} && docker compose up -d 2>&1", timeout=120
    )
    has_error = result and ("failed" in result.lower() or "fatal" in result.lower())
    if has_error:
        return {"ok": False, "error": f"Container-Start fehlgeschlagen: {result}"}

    return {"ok": True, "name": name, "port": port, "domain": hostname}


def delete_site(name: str) -> dict:
    site_dir = _site_dir(name)
    check = system_executor.execute_command(f"test -d {site_dir} && echo 'OK' || echo 'NO'")
    if "NO" in check:
        return {"ok": True, "message": "Site existierte nicht."}

    # Cloudflare aufräumen
    meta = _load_meta(name)
    cf_data = meta.get("cloudflare", {})
    if cf_data.get("enabled") and cf_data.get("tunnel_id") and meta.get("domain"):
        cf = _get_cf_client()
        if cf:
            tunnel_id = cf_data["tunnel_id"]
            hostname = meta["domain"]
            rules = cf.get_tunnel_ingress(tunnel_id)
            new_rules = [r for r in rules if not r.get("is_catchall") and r.get("hostname") != hostname]
            catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
            new_rules.append(catchall)
            cf.update_tunnel_ingress(tunnel_id, new_rules)
            zone_id = cf.find_zone_id(hostname)
            if zone_id:
                cf.delete_cname_record(zone_id, hostname)

    system_executor.execute_command(f"cd {site_dir} && docker compose down 2>&1")
    system_executor.execute_command(f"rm -rf {site_dir}")
    return {"ok": True}


def execute_action(name: str, action: str) -> dict:
    allowed = ("start", "stop", "restart")
    if action not in allowed:
        return {"ok": False, "error": f"Aktion nicht erlaubt: {action}"}

    site_dir = _site_dir(name)
    check = system_executor.execute_command(f"test -d {site_dir} && echo 'OK' || echo 'NO'")
    if "NO" in check:
        return {"ok": False, "error": f"Site '{name}' nicht gefunden."}

    cmd_map = {"start": "docker compose up -d", "stop": "docker compose stop", "restart": "docker compose restart"}
    result = system_executor.execute_command(f"cd {site_dir} && {cmd_map[action]} 2>&1", timeout=60)
    has_error = result and ("failed" in result.lower() or "fatal" in result.lower() or "error" in result.lower())
    return {"ok": not has_error, "output": result}


def update_config(name: str, spa: bool) -> dict:
    """Aktualisiert nginx-Konfiguration (z.B. SPA-Modus umschalten)."""
    meta = _load_meta(name)
    if not meta:
        return {"ok": False, "error": f"Site '{name}' nicht gefunden."}

    port = meta.get("port", 80)
    site_dir = _site_dir(name)

    nginx_conf = _generate_nginx_config(port, spa)
    encoded = base64.b64encode(nginx_conf.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {site_dir}/nginx.conf")

    meta["spa"] = spa
    _save_meta(name, meta)

    # nginx reload
    container = f"{SITE_PREFIX}{name}"
    system_executor.execute_command(f"docker exec {container} nginx -s reload 2>/dev/null", timeout=10)
    return {"ok": True}
