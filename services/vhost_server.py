# ==============================================================
# CARLA – Virtual Host Server
# Ein einzelner nginx-Container hostet beliebig viele kleine
# Sites/Dateien. Jede Site hat ihren eigenen Cloudflare-Hostname.
# Neue Sites werden per nginx-Reload hinzugefügt — kein neuer Container.
# ==============================================================

import json
import base64
import re
from urllib.parse import urlparse
from services import system_executor
from services.cloudflare import CloudflareClient
import config

VHOST_DIR = "/opt/stacks/carla-vhost"
VHOST_NAME = "carla-vhost"
VHOST_PORT = 10050
SITES_DIR = f"{VHOST_DIR}/sites"
META_FILE = f"{VHOST_DIR}/sites.json"


def normalize_name(name: str) -> str:
    """Normalisiert den Site-Namen: Umlauts zu ae/oe/ue, Leerzeichen zu Unterstrichen,
    Sonderzeichen entfernen, alles lowercase.
    """
    name = name.lower()
    replacements = {
        'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'ß': 'ss'
    }
    for char, rep in replacements.items():
        name = name.replace(char, rep)
    name = name.replace(' ', '_')
    name = re.sub(r'[^a-z0-9_-]', '_', name)
    name = re.sub(r'_+', '_', name)
    name = re.sub(r'-+', '-', name)
    return name.strip('_').strip('-')


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


def _parse_domain(domain_input: str) -> tuple[str, str]:
    """Trennt 'domain.de/pfad/xy' in ('domain.de', '/pfad/xy').
    Gibt ('domain.de', '/') zurück wenn kein Pfad angegeben."""
    domain_input = domain_input.strip().rstrip("/")
    if "/" in domain_input:
        idx = domain_input.index("/")
        return domain_input[:idx], domain_input[idx:] or "/"
    return domain_input, "/"


def _generate_nginx_conf(sites: list) -> str:
    from collections import defaultdict
    # Gruppiere Sites nach Hostname
    by_host: dict = defaultdict(list)
    for site in sites:
        by_host[site["hostname"]].append(site)

    lines = [
        "server {",
        f"    listen {VHOST_PORT} default_server;",
        "    server_name _;",
        "    return 404;",
        "}",
        ""
    ]

    for hostname, host_sites in by_host.items():
        # Spezifischere Pfade zuerst (längster Pfad hat Vorrang)
        host_sites_sorted = sorted(host_sites, key=lambda s: len(s.get("path", "/")), reverse=True)
        root_site = next((s for s in host_sites_sorted if s.get("path", "/") == "/"), None)

        # Sammle alle extra_hostnames der Sites unter diesem Host
        all_names = {hostname}
        for s in host_sites:
            for eh in s.get("extra_hostnames", []):
                all_names.add(eh)
        server_name_line = " ".join(sorted(all_names))
        lines += [
            "server {",
            f"    listen {VHOST_PORT};",
            f"    server_name {server_name_line};",
            "    charset utf-8;",
            "    port_in_redirect off;",
            "    absolute_redirect off;",
            ""
        ]

        # Zuerst alle Unterpfad-Sites (alias-basiert)
        for site in host_sites_sorted:
            path = site.get("path", "/")
            if path == "/":
                continue
            name = site["name"]
            spa = site.get("spa", False)
            site_dir = f"{SITES_DIR}/{name}"
            # SPA: unbekannte Unterrouten → index.html; sonst 404
            fallback = f"try_files $uri $uri/ @spa_{name};" if spa else "try_files $uri $uri/ =404;"
            path_slash = path.rstrip("/") + "/"
            path_no_slash = path.rstrip("/")
            lines += [
                f"    location = {path_no_slash} {{",
                f"        rewrite ^ {path_slash} permanent;",
                "    }",
                f"    location {path_slash} {{",
                f"        alias {site_dir}/;",
                "        index index.html index.htm;",
                f"        {fallback}",
                "    }",
            ]
            if spa:
                lines += [
                    f"    location @spa_{name} {{",
                    f"        rewrite ^ {path_slash}index.html break;",
                    "    }",
                ]
            lines.append("")

        # Root-Site am Ende (niedrigste Priorität)
        if root_site:
            name = root_site["name"]
            spa = root_site.get("spa", False)
            site_dir = f"{SITES_DIR}/{name}"
            fallback = "try_files $uri $uri/ /index.html;" if spa else "try_files $uri $uri/ =404;"
            lines += [
                f"    root {site_dir};",
                "    index index.html index.htm;",
                "",
                "    location / {",
                f"        {fallback}",
                "    }",
                "",
                "    location ~* \\.(css|js|png|jpg|jpeg|gif|ico|svg|woff|woff2)$ {",
                "        expires 7d;",
                '        add_header Cache-Control "public";',
                "    }",
                ""
            ]

        lines += ["}", ""]

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
    sites = _load_meta()
    for s in sites:
        s["www_path"] = f"{SITES_DIR}/{s['name']}"
    return sites


def _setup_cf_hostname(cf, tunnel_id: str, hostname: str, host_ip: str) -> dict | None:
    """Richtet Tunnel-Eintrag + CNAME für einen Hostname ein. None = schon vorhanden."""
    rules = cf.get_tunnel_ingress(tunnel_id)
    non_catchall = [r for r in rules if not r.get("is_catchall") and r.get("hostname")]
    existing = next((r for r in non_catchall if r["hostname"] == hostname), None)
    if existing and f":{VHOST_PORT}" in existing.get("service", ""):
        return None  # bereits korrekt eingerichtet
    if existing:
        return {"ok": False, "error": f"'{hostname}' zeigt bereits auf einen anderen Service ({existing['service']})."}
    non_catchall.append({"hostname": hostname, "service": f"http://{host_ip}:{VHOST_PORT}"})
    catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
    non_catchall.append(catchall)
    res = cf.update_tunnel_ingress(tunnel_id, non_catchall)
    if not res.get("success"):
        return {"ok": False, "error": f"Tunnel-Update fehlgeschlagen: {res.get('errors')}"}
    zone_id = cf.find_zone_id(hostname)
    if not zone_id:
        return {"ok": False, "error": f"Keine CF-Zone für '{hostname}'."}
    cf.delete_cname_record(zone_id, hostname)
    dns = cf.create_cname_record(zone_id, hostname, f"{tunnel_id}.cfargotunnel.com")
    if not dns.get("success"):
        return {"ok": False, "error": f"CNAME fehlgeschlagen: {dns.get('errors')}"}
    return {"ok": True}


def add_site(name: str, domain_input: str, tunnel_id: str, spa: bool = False,
             extra_hostnames: list = None) -> dict:
    """domain_input kann 'host.de' oder 'host.de/pfad/xy' sein."""
    name = normalize_name(name)
    if not name or not domain_input or not tunnel_id:
        return {"ok": False, "error": "Name, Domain und Tunnel sind erforderlich."}

    # Bereinige extra_hostnames vorab
    extra_hostnames_clean = [h.strip() for h in (extra_hostnames or []) if h.strip()]

    hostname, path = _parse_domain(domain_input)
    if not hostname and extra_hostnames_clean:
        hostname = extra_hostnames_clean[0]
        extra_hostnames = extra_hostnames_clean[1:]
    else:
        extra_hostnames = extra_hostnames_clean

    if not hostname:
        return {"ok": False, "error": "Ungültige Domain: Hostname darf nicht leer sein."}

    sites = _load_meta()
    if any(s.get("name") == name for s in sites):
        return {"ok": False, "error": f"Site '{name}' existiert bereits."}
    if any(s.get("hostname") == hostname and s.get("path", "/") == path for s in sites):
        return {"ok": False, "error": f"'{hostname}{path}' wird bereits verwendet."}

    cf = _get_cf_client()
    if not cf:
        return {"ok": False, "error": "Cloudflare nicht konfiguriert."}

    host_ip = _get_host_ip(cf, tunnel_id)
    rules = cf.get_tunnel_ingress(tunnel_id)
    non_catchall = [r for r in rules if not r.get("is_catchall") and r.get("hostname")]
    hostname_already_in_tunnel = any(
        r["hostname"] == hostname and f":{VHOST_PORT}" in r.get("service", "")
        for r in non_catchall
    )

    if not hostname_already_in_tunnel:
        # Tunnel-Eintrag nur anlegen wenn Hostname noch nicht auf unseren vhost zeigt
        # (bei Unterpfaden desselben Hosts ist der Eintrag bereits vorhanden)
        existing_entry = next((r for r in non_catchall if r["hostname"] == hostname), None)
        if existing_entry:
            # Hostname existiert schon, zeigt aber auf anderen Service → Fehler
            if f":{VHOST_PORT}" not in existing_entry.get("service", ""):
                return {"ok": False, "error": f"'{hostname}' ist bereits für einen anderen Service konfiguriert ({existing_entry['service']}). Wähle einen Unterpfad oder einen anderen Hostnamen."}
        else:
            non_catchall.append({"hostname": hostname, "service": f"http://{host_ip}:{VHOST_PORT}"})
            catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
            non_catchall.append(catchall)
            res = cf.update_tunnel_ingress(tunnel_id, non_catchall)
            if not res.get("success"):
                return {"ok": False, "error": f"Tunnel konnte nicht aktualisiert werden: {res.get('errors')}"}

            zone_id = cf.find_zone_id(hostname)
            if not zone_id:
                return {"ok": False, "error": f"Keine Cloudflare-Zone für '{hostname}' gefunden."}
            cf.delete_cname_record(zone_id, hostname)
            dns_res = cf.create_cname_record(zone_id, hostname, f"{tunnel_id}.cfargotunnel.com")
            if not dns_res.get("success"):
                return {"ok": False, "error": f"DNS-Eintrag konnte nicht erstellt werden: {dns_res.get('errors')}"}

    # Extra-Hostnames einrichten (zusätzliche Cloudflare-Domains)
    extra_hostnames = [h.strip() for h in (extra_hostnames or []) if h.strip()]
    for eh in extra_hostnames:
        result = _setup_cf_hostname(cf, tunnel_id, eh, host_ip)
        if result and not result.get("ok"):
            return result  # Abbruch bei Fehler

    # Site-Verzeichnis + Standard-index.html
    site_dir = f"{SITES_DIR}/{name}"
    system_executor.execute_command(f"mkdir -p {site_dir}")
    html = _default_index(name)
    encoded = base64.b64encode(html.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {site_dir}/index.html")

    # Metadata speichern
    site_entry = {
        "name": name, "hostname": hostname, "path": path,
        "tunnel_id": tunnel_id, "spa": spa,
        "extra_hostnames": extra_hostnames,
    }
    sites.append(site_entry)
    _save_meta(sites)

    ensure_server(sites)

    all_urls = [f"https://{hostname}{path}"] + [f"https://{eh}" for eh in extra_hostnames]
    return {"ok": True, "name": name, "hostname": hostname, "path": path,
            "urls": all_urls, "www_path": site_dir}


def _cleanup_cf_hostnames(cf, tunnel_id: str, hostnames: list, remaining_sites: list):
    """Entfernt Ingress-Regeln und DNS-Einträge für eine Liste von Hostnames,
    wenn sie von keinem der verbleibenden Sites mehr genutzt werden.
    """
    used_hostnames = set()
    for s in remaining_sites:
        used_hostnames.add(s.get("hostname"))
        for eh in s.get("extra_hostnames", []):
            used_hostnames.add(eh)

    to_remove = [h for h in hostnames if h and h not in used_hostnames]
    if not to_remove:
        return

    try:
        rules = cf.get_tunnel_ingress(tunnel_id)
        new_rules = [r for r in rules if not r.get("is_catchall") and r.get("hostname") not in to_remove]
        catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
        new_rules.append(catchall)
        cf.update_tunnel_ingress(tunnel_id, new_rules)

        for hostname in to_remove:
            zone_id = cf.find_zone_id(hostname)
            if zone_id:
                cf.delete_cname_record(zone_id, hostname)
    except Exception as e:
        print(f"❌ [vhost] Fehler beim Cloudflare-Cleanup: {e}")


def remove_site(name: str) -> dict:
    sites = _load_meta()
    site = next((s for s in sites if s.get("name") == name), None)
    if not site:
        return {"ok": False, "error": f"Site '{name}' nicht gefunden."}

    new_sites = [s for s in sites if s.get("name") != name]

    cf = _get_cf_client()
    if cf:
        tunnel_id = site.get("tunnel_id")
        if tunnel_id:
            old_domains = [site.get("hostname")] + site.get("extra_hostnames", [])
            _cleanup_cf_hostnames(cf, tunnel_id, old_domains, new_sites)

    system_executor.execute_command(f"rm -rf {SITES_DIR}/{name}")
    _save_meta(new_sites)
    ensure_server(new_sites)

    return {"ok": True}


def update_site(old_name: str, new_name: str, domain_input: str, tunnel_id: str, spa: bool = False,
                extra_hostnames: list = None) -> dict:
    """Aktualisiert eine bestehende Virtual Host Site."""
    if not old_name or not new_name or not domain_input or not tunnel_id:
        return {"ok": False, "error": "Name, Domain und Tunnel sind erforderlich."}

    new_name = normalize_name(new_name)
    if not new_name:
        return {"ok": False, "error": "Name ist ungültig."}

    sites = _load_meta()
    site_idx = next((i for i, s in enumerate(sites) if s.get("name") == old_name), -1)
    if site_idx == -1:
        return {"ok": False, "error": f"Site '{old_name}' nicht gefunden."}

    old_site = sites[site_idx]

    if old_name != new_name and any(s.get("name") == new_name for s in sites):
        return {"ok": False, "error": f"Eine Site mit dem Namen '{new_name}' existiert bereits."}

    extra_hostnames_clean = [h.strip() for h in (extra_hostnames or []) if h.strip()]
    hostname, path = _parse_domain(domain_input)
    if not hostname and extra_hostnames_clean:
        hostname = extra_hostnames_clean[0]
        extra_hostnames_clean = extra_hostnames_clean[1:]

    if not hostname:
        return {"ok": False, "error": "Ungültige Domain: Hostname darf nicht leer sein."}

    if any(s.get("name") != old_name and s.get("hostname") == hostname and s.get("path", "/") == path for s in sites):
        return {"ok": False, "error": f"'{hostname}{path}' wird bereits von einer anderen Site verwendet."}

    cf = _get_cf_client()
    if not cf:
        return {"ok": False, "error": "Cloudflare nicht konfiguriert."}

    host_ip = _get_host_ip(cf, tunnel_id)

    # 1. Cloudflare Cleanup für alte Domains, die nicht mehr verwendet werden
    old_domains = [old_site.get("hostname")] + old_site.get("extra_hostnames", [])
    other_sites = [s for s in sites if s.get("name") != old_name]
    _cleanup_cf_hostnames(cf, old_site.get("tunnel_id"), old_domains, other_sites + [{"hostname": hostname, "extra_hostnames": extra_hostnames_clean}])

    # 2. Setup für die neuen Domains
    rules = cf.get_tunnel_ingress(tunnel_id)
    non_catchall = [r for r in rules if not r.get("is_catchall") and r.get("hostname")]
    hostname_already_in_tunnel = any(
        r["hostname"] == hostname and f":{VHOST_PORT}" in r.get("service", "")
        for r in non_catchall
    )

    if not hostname_already_in_tunnel:
        existing_entry = next((r for r in non_catchall if r["hostname"] == hostname), None)
        if existing_entry and f":{VHOST_PORT}" not in existing_entry.get("service", ""):
            return {"ok": False, "error": f"'{hostname}' ist bereits für einen anderen Service konfiguriert ({existing_entry['service']})."}
        
        non_catchall.append({"hostname": hostname, "service": f"http://{host_ip}:{VHOST_PORT}"})
        catchall = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
        non_catchall.append(catchall)
        res = cf.update_tunnel_ingress(tunnel_id, non_catchall)
        if not res.get("success"):
            return {"ok": False, "error": f"Tunnel konnte nicht aktualisiert werden: {res.get('errors')}"}

        zone_id = cf.find_zone_id(hostname)
        if not zone_id:
            return {"ok": False, "error": f"Keine Cloudflare-Zone für '{hostname}' gefunden."}
        cf.delete_cname_record(zone_id, hostname)
        dns_res = cf.create_cname_record(zone_id, hostname, f"{tunnel_id}.cfargotunnel.com")
        if not dns_res.get("success"):
            return {"ok": False, "error": f"DNS-Eintrag konnte nicht erstellt werden: {dns_res.get('errors')}"}

    for eh in extra_hostnames_clean:
        result = _setup_cf_hostname(cf, tunnel_id, eh, host_ip)
        if result and not result.get("ok"):
            return result

    # 3. Rename Site-Verzeichnis falls Name geändert
    old_dir = f"{SITES_DIR}/{old_name}"
    new_dir = f"{SITES_DIR}/{new_name}"
    if old_name != new_name:
        system_executor.execute_command(f"mv {old_dir} {new_dir}")

    # 4. Metadata updaten
    updated_entry = {
        "name": new_name, "hostname": hostname, "path": path,
        "tunnel_id": tunnel_id, "spa": spa,
        "extra_hostnames": extra_hostnames_clean,
    }
    sites[site_idx] = updated_entry
    _save_meta(sites)

    ensure_server(sites)

    all_urls = [f"https://{hostname}{path}"] + [f"https://{eh}" for eh in extra_hostnames_clean]
    return {"ok": True, "name": new_name, "hostname": hostname, "path": path,
            "urls": all_urls, "www_path": new_dir}
