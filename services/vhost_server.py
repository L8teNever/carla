# ==============================================================
# CARLA – Virtual Host Server
# Ein einzelner nginx-Container hostet beliebig viele kleine
# Sites/Dateien. Jede Site hat ihren eigenen Cloudflare-Hostname.
# Neue Sites werden per nginx-Reload hinzugefügt — kein neuer Container.
# ==============================================================

import json
import base64
from urllib.parse import urlparse
from services import system_executor
from services.cloudflare import CloudflareClient
import config

VHOST_DIR = "/opt/stacks/carla-vhost"
VHOST_NAME = "carla-vhost"
VHOST_PORT = 10050
SITES_DIR = f"{VHOST_DIR}/sites"
META_FILE = f"{VHOST_DIR}/sites.json"


def _get_cf_client():
    if config.CF_API_TOKEN and config.CF_ACCOUNT_ID:
        return CloudflareClient(config.CF_API_TOKEN, config.CF_ACCOUNT_ID)
    return None


def _get_host_ip(cf_client, tunnel_id: str) -> str:
    rules = cf_client.get_tunnel_ingress(tunnel_id)
    for r in rules:
        svc = r.get("service", "")
        if svc.startswith("http://") and not r.get("is_catchall"):
            parsed = urlparse(svc)
            if parsed.hostname and parsed.hostname not in ("localhost", "127.0.0.1"):
                return parsed.hostname
    return "10.7.0.1"


def _load_meta() -> list:
    content = system_executor.execute_command(f"cat {META_FILE} 2>/dev/null")
    if content and "Error" not in content and "No such file" not in content:
        try:
            return json.loads(content)
        except Exception:
            pass
    return []


def _save_meta(sites: list):
    data = json.dumps(sites, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(data.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {META_FILE}")


def _generate_nginx_conf(sites: list) -> str:
    lines = [
        "# Default: unbekannte Hosts → 404",
        "server {",
        f"    listen {VHOST_PORT} default_server;",
        "    server_name _;",
        "    return 404;",
        "}",
        ""
    ]
    for site in sites:
        hostname = site.get("hostname", "")
        name = site.get("name", "")
        spa = site.get("spa", False)
        root = f"{SITES_DIR}/{name}"
        fallback = "try_files $uri $uri/ /index.html;" if spa else "try_files $uri $uri/ =404;"
        lines += [
            f"server {{",
            f"    listen {VHOST_PORT};",
            f"    server_name {hostname};",
            f"    root {root};",
            "    index index.html index.htm;",
            "    charset utf-8;",
            "",
            "    location / {",
            f"        {fallback}",
            "    }",
            "",
            "    location ~* \\.(css|js|png|jpg|jpeg|gif|ico|svg|woff|woff2)$ {",
            "        expires 7d;",
            "        add_header Cache-Control \"public\";",
            "    }",
            "}",
            ""
        ]
    return "\n".join(lines)


def _write_config(sites: list):
    conf = _generate_nginx_conf(sites)
    encoded = base64.b64encode(conf.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {VHOST_DIR}/nginx.conf")


def _reload():
    system_executor.execute_command(
        f"docker exec {VHOST_NAME} nginx -s reload 2>/dev/null", timeout=10
    )


def is_running() -> bool:
    state = system_executor.execute_command(
        f"docker inspect --format '{{{{.State.Status}}}}' {VHOST_NAME} 2>/dev/null"
    ).strip()
    return state == "running"


def _default_index(name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d0d0d;color:#ccc;font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{text-align:center;padding:2rem}}
h1{{font-size:2rem;color:#7c3aed;margin-bottom:.8rem}}
p{{color:#666;font-size:.95rem}}
code{{background:#1a1a1a;padding:.2rem .5rem;border-radius:4px;color:#a78bfa;font-size:.85rem}}
</style>
</head>
<body>
<div class="box">
<h1>{name}</h1>
<p>Dateien unter<br><code>{SITES_DIR}/{name}/</code><br>im File Manager bearbeiten.</p>
</div>
</body>
</html>"""


def ensure_server(sites: list = None):
    """Startet den geteilten vhost-Container, falls er nicht läuft."""
    system_executor.execute_command(f"mkdir -p {SITES_DIR}")

    if sites is None:
        sites = _load_meta()

    _write_config(sites)

    compose = f"""services:
  nginx:
    image: nginx:alpine
    container_name: {VHOST_NAME}
    restart: always
    network_mode: host
    volumes:
      - {VHOST_DIR}/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - {SITES_DIR}:{SITES_DIR}:ro
"""
    encoded = base64.b64encode(compose.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {VHOST_DIR}/docker-compose.yml")

    if not is_running():
        system_executor.execute_command(
            f"cd {VHOST_DIR} && docker compose up -d 2>&1", timeout=120
        )
    else:
        _reload()


def list_sites() -> list:
    return _load_meta()


def add_site(name: str, hostname: str, tunnel_id: str, spa: bool = False) -> dict:
    if not name or not hostname or not tunnel_id:
        return {"ok": False, "error": "Name, Hostname und Tunnel sind erforderlich."}

    sites = _load_meta()
    if any(s.get("name") == name for s in sites):
        return {"ok": False, "error": f"Site '{name}' existiert bereits."}
    if any(s.get("hostname") == hostname for s in sites):
        return {"ok": False, "error": f"Hostname '{hostname}' wird bereits verwendet."}

    cf = _get_cf_client()
    if not cf:
        return {"ok": False, "error": "Cloudflare nicht konfiguriert."}

    host_ip = _get_host_ip(cf, tunnel_id)

    # Cloudflare Tunnel-Eintrag
    rules = cf.get_tunnel_ingress(tunnel_id)
    non_catchall = [r for r in rules if not r.get("is_catchall") and r.get("hostname")]
    if not any(r["hostname"] == hostname for r in non_catchall):
        non_catchall.append({"hostname": hostname, "service": f"http://{host_ip}:{VHOST_PORT}"})
    catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
    non_catchall.append(catchall)
    res = cf.update_tunnel_ingress(tunnel_id, non_catchall)
    if not res.get("success"):
        return {"ok": False, "error": f"Tunnel konnte nicht aktualisiert werden: {res.get('errors')}"}

    # CNAME
    zone_id = cf.find_zone_id(hostname)
    if not zone_id:
        return {"ok": False, "error": f"Keine Cloudflare-Zone für '{hostname}' gefunden."}
    cf.delete_cname_record(zone_id, hostname)
    dns_res = cf.create_cname_record(zone_id, hostname, f"{tunnel_id}.cfargotunnel.com")
    if not dns_res.get("success"):
        return {"ok": False, "error": f"DNS-Eintrag konnte nicht erstellt werden: {dns_res.get('errors')}"}

    # Site-Verzeichnis + Standard-index.html
    site_dir = f"{SITES_DIR}/{name}"
    system_executor.execute_command(f"mkdir -p {site_dir}")
    html = _default_index(name)
    encoded = base64.b64encode(html.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {site_dir}/index.html")

    # Metadata speichern
    site_entry = {"name": name, "hostname": hostname, "tunnel_id": tunnel_id, "spa": spa}
    sites.append(site_entry)
    _save_meta(sites)

    # nginx neu laden (kein neuer Container)
    ensure_server(sites)

    return {"ok": True, "name": name, "hostname": hostname, "port": VHOST_PORT, "www_path": site_dir}


def remove_site(name: str) -> dict:
    sites = _load_meta()
    site = next((s for s in sites if s.get("name") == name), None)
    if not site:
        return {"ok": False, "error": f"Site '{name}' nicht gefunden."}

    # Cloudflare aufräumen
    cf = _get_cf_client()
    if cf:
        tunnel_id = site.get("tunnel_id")
        hostname = site.get("hostname")
        if tunnel_id and hostname:
            rules = cf.get_tunnel_ingress(tunnel_id)
            new_rules = [r for r in rules if not r.get("is_catchall") and r.get("hostname") != hostname]
            catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
            new_rules.append(catchall)
            cf.update_tunnel_ingress(tunnel_id, new_rules)
            zone_id = cf.find_zone_id(hostname)
            if zone_id:
                cf.delete_cname_record(zone_id, hostname)

    # Dateien und Metadata
    system_executor.execute_command(f"rm -rf {SITES_DIR}/{name}")
    new_sites = [s for s in sites if s.get("name") != name]
    _save_meta(new_sites)
    ensure_server(new_sites)

    return {"ok": True}
