# ==============================================================
# CARLA – Token Gate Service
# Erstellt limitierte Einmallinks für vhost-Sites in zwei Modi:
#
#   Pfad-Modus:      anna.domain.de/TOKEN  (nginx proxied)
#   Subdomain-Modus: TOKEN.domain.de       (direkt via Tunnel)
#
# hostname/base_domain können None sein → zufälliger vorhandener Host.
# ==============================================================

import json
import base64
import secrets
import string
import random
from datetime import datetime
from services import system_executor

TOKEN_GATE_PORT = 10052
VHOST_DIR       = "/opt/stacks/carla-vhost"
SITES_DIR       = f"{VHOST_DIR}/sites"
TOKEN_FILE      = f"{VHOST_DIR}/tokens.json"
TOKEN_GATE_NAME = "carla-token-gate"

# URL-path safe (Pfad-Modus)
TOKEN_CHARS_PATH      = string.ascii_letters + string.digits + "-_"
# DNS-safe / lowercase (Subdomain-Modus; kein _)
TOKEN_CHARS_SUBDOMAIN = string.ascii_lowercase + string.digits + "-"
TOKEN_LENGTH_MIN =  16
TOKEN_LENGTH_MAX = 256
TOKEN_LENGTH_DEFAULT = 16

# ── Eingebettetes Python-Server-Skript ─────────────────────────
_SERVER_SCRIPT = r'''#!/usr/bin/env python3
import json, mimetypes, urllib.parse, urllib.request, os, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

TOKEN_FILE    = "/opt/stacks/carla-vhost/tokens.json"
SITES_DIR     = "/opt/stacks/carla-vhost/sites"
CF_API_TOKEN  = os.environ.get("CF_API_TOKEN", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
ASSET_EXTS = {
    ".css",".js",".png",".jpg",".jpeg",".gif",".ico",
    ".svg",".woff",".woff2",".ttf",".eot",".webp",".map",".json",
}

def load_tokens():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_tokens(t):
    with open(TOKEN_FILE, "w") as f:
        json.dump(t, f, indent=2)

# ── Cloudflare-Cleanup (läuft im Hintergrund-Thread) ───────────

def _cf_request(method, path, body=None):
    url = f"https://api.cloudflare.com/client/v4{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
          headers={"Authorization": f"Bearer {CF_API_TOKEN}",
                   "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _cf_cleanup_subdomain(token):
    """Entfernt Tunnel-Ingress + DNS-CNAME für Subdomain-Tokens."""
    if not CF_API_TOKEN or not CF_ACCOUNT_ID:
        return
    tunnel_id = token.get("tunnel_id")
    hostname  = token.get("hostname")
    if not tunnel_id or not hostname:
        return
    try:
        # 1. Tunnel-Ingress aktualisieren
        path   = f"/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{tunnel_id}/configurations"
        result = _cf_request("GET", path)
        rules  = result.get("result", {}).get("config", {}).get("ingress", [])
        new_rules = [r for r in rules if r.get("hostname") != hostname]
        if not any(not r.get("hostname") for r in new_rules):
            new_rules.append({"service": "http_status:404"})
        _cf_request("PUT", path, {"config": {"ingress": new_rules}})

        # 2. DNS-CNAME löschen
        root = ".".join(hostname.split(".")[-2:])
        zones = _cf_request("GET", f"/zones?name={root}").get("result", [])
        if not zones:
            return
        zone_id = zones[0]["id"]
        records = _cf_request("GET",
            f"/zones/{zone_id}/dns_records?type=CNAME&name={hostname}"
        ).get("result", [])
        for rec in records:
            _cf_request("DELETE", f"/zones/{zone_id}/dns_records/{rec['id']}")
    except Exception as e:
        print(f"[token_gate] CF-Cleanup Fehler: {e}", flush=True)

def _auto_cleanup(token_code, token, tokens):
    """Löscht das Token und räumt Cloudflare auf (im Hintergrund-Thread)."""
    tokens.pop(token_code, None)
    save_tokens(tokens)
    if token.get("is_subdomain"):
        _cf_cleanup_subdomain(token)

# ── HTTP-Handler ───────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def reply_html(self, code, msg):
        body = f"<!DOCTYPE html><html><body><h1>{code}</h1><p>{msg}</p></body></html>".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def resolve_token(self, tokens):
        """
        Pfad-Modus  – nginx setzt X-Forwarded-Host + X-Forwarded-Uri.
        Subdomain   – kein X-Forwarded-Host; Token ist erster Host-Label.
        Gibt (token_code, sub_path) oder (None, None) zurück.
        """
        fwd_host = self.headers.get("X-Forwarded-Host", "")
        if fwd_host:
            uri   = self.headers.get("X-Forwarded-Uri", self.path)
            parts = urllib.parse.urlparse(uri).path.strip("/").split("/", 1)
            cand  = urllib.parse.unquote(parts[0]) if parts[0] else ""
            if cand in tokens:
                return cand, (parts[1] if len(parts) > 1 else "")
        else:
            host = self.headers.get("Host", "").split(":")[0]
            cand = host.split(".")[0] if host else ""
            if cand in tokens:
                return cand, urllib.parse.urlparse(self.path).path.lstrip("/")
        return None, None

    def do_GET(self):
        tokens     = load_tokens()
        token_code, sub_path = self.resolve_token(tokens)

        if not token_code:
            self.reply_html(404, "Dieser Link existiert nicht.")
            return

        token    = tokens[token_code]
        uses     = token.get("uses", 0)
        max_uses = token.get("max_uses", 1)
        is_asset = Path(sub_path).suffix.lower() in ASSET_EXTS if sub_path else False

        # max_uses == 0 bedeutet unbegrenzt
        if not is_asset and max_uses != 0 and uses >= max_uses:
            self.reply_html(410, "Dieser Link ist abgelaufen.")
            return

        site_dir  = Path(SITES_DIR) / token["site_name"]
        file_path = site_dir / sub_path if sub_path else site_dir / "index.html"

        try:
            file_path.resolve().relative_to(site_dir.resolve())
        except ValueError:
            self.reply_html(403, "Verboten.")
            return

        if file_path.is_dir():
            file_path = file_path / "index.html"

        if not file_path.exists() or not file_path.is_file():
            self.reply_html(404, "Datei nicht gefunden.")
            return

        # Antwort senden bevor wir aufräumen
        content = file_path.read_bytes()
        mime, _ = mimetypes.guess_type(str(file_path))
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

        if not is_asset:
            new_uses = uses + 1
            if max_uses != 0 and new_uses >= max_uses:
                # Letzte Nutzung → Token löschen + CF aufräumen (im Hintergrund)
                threading.Thread(
                    target=_auto_cleanup,
                    args=(token_code, dict(token), tokens),
                    daemon=True,
                ).start()
            else:
                token["uses"] = new_uses
                save_tokens(tokens)

if __name__ == "__main__":
    print("Token Gate running on port 10052", flush=True)
    HTTPServer(("0.0.0.0", 10052), Handler).serve_forever()
'''


# ── Token-Datei ────────────────────────────────────────────────

def _load_tokens() -> dict:
    content = system_executor.execute_command(f"cat {TOKEN_FILE} 2>/dev/null")
    if content and "Error" not in content and "No such file" not in content:
        try:
            return json.loads(content)
        except Exception:
            pass
    return {}


def _save_tokens(tokens: dict):
    data    = json.dumps(tokens, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(data.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {TOKEN_FILE}")


# ── Docker-Container ───────────────────────────────────────────

def _write_server_script():
    encoded = base64.b64encode(_SERVER_SCRIPT.encode()).decode()
    system_executor.execute_command(
        f"echo '{encoded}' | base64 -d > {VHOST_DIR}/token_gate_server.py"
    )


def _is_running() -> bool:
    state = system_executor.execute_command(
        f"docker inspect --format '{{{{.State.Status}}}}' {TOKEN_GATE_NAME} 2>/dev/null"
    ).strip()
    return state == "running"


def ensure_token_gate_running():
    """Stellt sicher, dass der Token-Gate-Container läuft."""
    import config as _cfg
    _write_server_script()

    cf_token  = getattr(_cfg, "CF_API_TOKEN",  "") or ""
    cf_account = getattr(_cfg, "CF_ACCOUNT_ID", "") or ""

    compose = f"""services:
  token-gate:
    image: python:3-alpine
    container_name: {TOKEN_GATE_NAME}
    restart: always
    network_mode: host
    environment:
      CF_API_TOKEN: "{cf_token}"
      CF_ACCOUNT_ID: "{cf_account}"
    volumes:
      - {VHOST_DIR}:{VHOST_DIR}
    command: python3 {VHOST_DIR}/token_gate_server.py
"""
    encoded = base64.b64encode(compose.encode()).decode()
    system_executor.execute_command(
        f"echo '{encoded}' | base64 -d > {VHOST_DIR}/token-gate-compose.yml"
    )
    if not _is_running():
        system_executor.execute_command(
            f"cd {VHOST_DIR} && docker compose -f token-gate-compose.yml up -d 2>&1",
            timeout=120,
        )


# ── Cloudflare-Hilfsfunktionen ─────────────────────────────────

def _get_cf_client():
    import config
    from services.cloudflare import CloudflareClient
    if config.CF_API_TOKEN and config.CF_ACCOUNT_ID:
        return CloudflareClient(config.CF_API_TOKEN, config.CF_ACCOUNT_ID)
    return None


def _get_host_ip(cf, tunnel_id: str) -> str:
    from urllib.parse import urlparse
    rules = cf.get_tunnel_ingress(tunnel_id)
    for r in rules:
        svc = r.get("service", "")
        if svc.startswith("http://") and not r.get("is_catchall"):
            parsed = urlparse(svc)
            if parsed.hostname and parsed.hostname not in ("localhost", "127.0.0.1"):
                return parsed.hostname
    return "10.7.0.1"


def _cf_add_subdomain(cf, tunnel_id: str, hostname: str) -> dict:
    """Legt Tunnel-Ingress + CNAME für eine Subdomain an."""
    rules        = cf.get_tunnel_ingress(tunnel_id)
    non_catchall = [r for r in rules if not r.get("is_catchall") and r.get("hostname")]

    if any(r["hostname"] == hostname for r in non_catchall):
        return {"ok": False, "error": f"'{hostname}' ist bereits konfiguriert."}

    host_ip = _get_host_ip(cf, tunnel_id)
    non_catchall.append({"hostname": hostname, "service": f"http://{host_ip}:{TOKEN_GATE_PORT}"})
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


def _cf_remove_subdomain(cf, tunnel_id: str, hostname: str):
    """Entfernt Tunnel-Ingress + CNAME einer Subdomain."""
    try:
        rules     = cf.get_tunnel_ingress(tunnel_id)
        new_rules = [r for r in rules if not r.get("is_catchall") and r.get("hostname") != hostname]
        catchall  = next((r for r in rules if r.get("is_catchall")), {"service": "http_status:404"})
        new_rules.append(catchall)
        cf.update_tunnel_ingress(tunnel_id, new_rules)

        zone_id = cf.find_zone_id(hostname)
        if zone_id:
            cf.delete_cname_record(zone_id, hostname)
    except Exception as e:
        print(f"❌ [token_gate] CF-Cleanup Fehler: {e}")


# ── Hilfsfunktionen ────────────────────────────────────────────

def _extract_base_domain(hostname: str) -> str:
    """'sub.domain.de' → 'domain.de'"""
    parts = hostname.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def _pick_random_site() -> dict | None:
    """Wählt eine zufällige vorhandene vhost-Site."""
    from services import vhost_server
    sites = vhost_server.list_sites()
    return random.choice(sites) if sites else None


def _find_tunnel_for_domain(base_domain: str) -> str | None:
    from services import vhost_server
    sites = vhost_server.list_sites()
    match = next(
        (s for s in sites
         if s["hostname"] == base_domain or s["hostname"].endswith(f".{base_domain}")),
        None,
    )
    return match.get("tunnel_id") if match else None


def _gen_token(chars: str, existing: set, length: int = TOKEN_LENGTH_DEFAULT) -> str:
    length = max(TOKEN_LENGTH_MIN, min(TOKEN_LENGTH_MAX, length))
    for _ in range(200):
        code = "".join(secrets.choice(chars) for _ in range(length))
        if chars is TOKEN_CHARS_SUBDOMAIN:
            if code[0] == "-" or code[-1] == "-":
                continue
        if code not in existing:
            return code
    raise RuntimeError("Token-Generierung fehlgeschlagen")


# ── Öffentliche API ────────────────────────────────────────────

def create_link(
    site_name: str,
    hostname: str = None,
    max_uses: int = 1,
    use_subdomain: bool = False,
    base_domain: str = None,
    tunnel_id: str = None,
    token_length: int = TOKEN_LENGTH_DEFAULT,
) -> dict:
    """
    Erstellt einen limitierten Zugangslink für eine vhost-Site.

    Modi:
      use_subdomain=False  →  https://hostname/TOKEN         (nginx proxied)
      use_subdomain=True   →  https://TOKEN.base_domain      (eigene Subdomain via CF)

    Automatische Auflösung:
      hostname=None        →  zufälliger vorhandener vhost-Host
      base_domain=None     →  wird aus hostname/Sites extrahiert
      tunnel_id=None       →  wird aus vorhandenen Sites ermittelt
    """
    if not site_name:
        return {"ok": False, "error": "site_name ist erforderlich."}
    max_uses = max(0, int(max_uses))  # 0 = unbegrenzt
    tokens   = _load_tokens()

    # ── Subdomain-Modus ──────────────────────────────────────
    if use_subdomain:
        # base_domain ermitteln
        if not base_domain:
            if hostname:
                base_domain = _extract_base_domain(hostname)
            else:
                site = _pick_random_site()
                if not site:
                    return {"ok": False, "error": "Keine vhost-Sites vorhanden."}
                base_domain = _extract_base_domain(site["hostname"])
                if not tunnel_id:
                    tunnel_id = site.get("tunnel_id")

        # tunnel_id ermitteln
        if not tunnel_id:
            tunnel_id = _find_tunnel_for_domain(base_domain)
        if not tunnel_id:
            return {"ok": False, "error": f"Kein Tunnel für Domain '{base_domain}' gefunden."}

        cf = _get_cf_client()
        if not cf:
            return {"ok": False, "error": "Cloudflare nicht konfiguriert."}

        code              = _gen_token(TOKEN_CHARS_SUBDOMAIN, set(tokens.keys()), token_length)
        subdomain_hostname = f"{code}.{base_domain}"

        cf_result = _cf_add_subdomain(cf, tunnel_id, subdomain_hostname)
        if not cf_result.get("ok"):
            return cf_result

        tokens[code] = {
            "site_name":   site_name,
            "hostname":    subdomain_hostname,
            "base_domain": base_domain,
            "tunnel_id":   tunnel_id,
            "is_subdomain": True,
            "max_uses":    max_uses,
            "uses":        0,
            "created_at":  datetime.utcnow().isoformat(),
        }
        _save_tokens(tokens)
        ensure_token_gate_running()

        return {
            "ok":            True,
            "token":         code,
            "url":           f"https://{subdomain_hostname}",
            "max_uses":      max_uses,
            "uses_remaining": max_uses,
        }

    # ── Pfad-Modus ───────────────────────────────────────────
    if not hostname:
        site = _pick_random_site()
        if not site:
            return {"ok": False, "error": "Keine vhost-Sites vorhanden."}
        hostname = site["hostname"]

    code = _gen_token(TOKEN_CHARS_PATH, set(tokens.keys()), token_length)

    tokens[code] = {
        "site_name":   site_name,
        "hostname":    hostname,
        "is_subdomain": False,
        "max_uses":    max_uses,
        "uses":        0,
        "created_at":  datetime.utcnow().isoformat(),
    }
    _save_tokens(tokens)
    ensure_token_gate_running()

    return {
        "ok":            True,
        "token":         code,
        "url":           f"https://{hostname}/{code}",
        "max_uses":      max_uses,
        "uses_remaining": max_uses,
    }


def list_links(site_name: str = None) -> list:
    """Gibt alle Links zurück, optional gefiltert nach site_name."""
    tokens = _load_tokens()
    result = []
    for code, data in tokens.items():
        if site_name and data.get("site_name") != site_name:
            continue
        uses  = data.get("uses", 0)
        max_u = data.get("max_uses", 1)
        hn    = data.get("hostname", "")
        url   = f"https://{hn}" if data.get("is_subdomain") else f"https://{hn}/{code}"
        result.append({
            "token":         code,
            "site_name":     data.get("site_name"),
            "hostname":      hn,
            "url":           url,
            "is_subdomain":  data.get("is_subdomain", False),
            "max_uses":      max_u,
            "uses":          uses,
            "uses_remaining": max(0, max_u - uses),
            "active":        uses < max_u,
            "created_at":    data.get("created_at"),
        })
    return result


def delete_link(token_code: str) -> dict:
    """Löscht einen Link und räumt Cloudflare auf (bei Subdomain-Tokens)."""
    tokens = _load_tokens()
    if token_code not in tokens:
        return {"ok": False, "error": f"Token '{token_code}' nicht gefunden."}

    token = tokens[token_code]
    if token.get("is_subdomain"):
        cf = _get_cf_client()
        if cf and token.get("tunnel_id") and token.get("hostname"):
            _cf_remove_subdomain(cf, token["tunnel_id"], token["hostname"])

    del tokens[token_code]
    _save_tokens(tokens)
    return {"ok": True}


def reset_link(token_code: str) -> dict:
    """Setzt den Nutzungszähler zurück."""
    tokens = _load_tokens()
    if token_code not in tokens:
        return {"ok": False, "error": f"Token '{token_code}' nicht gefunden."}
    tokens[token_code]["uses"] = 0
    _save_tokens(tokens)
    return {"ok": True, "token": token_code, "uses_remaining": tokens[token_code]["max_uses"]}
