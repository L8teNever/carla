# ==============================================================
# CARLA – Flask Routes
# Definiert alle URL-Endpunkte der Webanwendung.
# ==============================================================

from flask import Blueprint, render_template, jsonify, request, redirect, url_for
import threading
from urllib.parse import urlparse
from services import cloudflare, ssh_docker, cache, metrics_db, setup, updater, backup, ports, discovery, google_drive, file_manager
import config

bp = Blueprint("main", __name__)
CACHE_KEY = "full_infrastructure"


# ---------------------------------------------------------------
# Setup-Middleware: Redirect auf Setup wenn nicht eingerichtet
# ---------------------------------------------------------------

@bp.before_request
def check_setup():
    if not setup.is_setup_done():
        # Erlaube nur Setup-Routes ohne Redirect
        allowed = ('/setup', '/api/setup')
        if not request.path.startswith(allowed):
            return redirect('/setup')

# Threading-Logik für Hintergrund-Abfragen
fetch_lock = threading.Lock()
is_fetching = False

def _build_cf_graph_data(client: cloudflare.CloudflareClient) -> dict:
    nodes, edges = [], []
    nodes.append({"id": "root", "label": "Account", "group": "root", "level": 0})
    try:
        tunnels = client.fetch("cfd_tunnel")
        for tun in tunnels:
            tun_id = tun['id']
            nodes.append({"id": tun_id, "label": tun['name'], "group": "tunnel", "level": 1})
            edges.append({"from": "root", "to": tun_id})

            config_data = client.get_tunnel_config(tun_id)
            for i, entry in enumerate(config_data.get('config', {}).get('ingress', [])):
                if 'hostname' in entry:
                    host_name = entry['hostname']
                    host_id = f"host_{tun_id}_{i}"
                    nodes.append({"id": host_id, "label": host_name, "group": "hostname", "level": 2})
                    edges.append({"from": tun_id, "to": host_id})

                    svc_url = entry['service']
                    # Die Gesundheitsprüfung (DNS Ping) von der lokalen Maschine aus
                    # überschreitet das Timeout und wurde für Performance-Zwecke entfernt.
                    status_color = "#AABB00"

                    svc_id = f"svc_{tun_id}_{i}"
                    nodes.append({"id": svc_id, "label": f"→ {svc_url}", "group": "service", "level": 3, "font": {"color": status_color}})
                    edges.append({"from": host_id, "to": svc_id})

        apps = client.fetch("access/apps")
        for app_data in apps:
            app_id = app_data['id']
            app_uid = f"access_{app_id}"
            domain = app_data.get('domain', '')

            parent_id = "root"
            for node in nodes:
                if node.get('label') == domain:
                    parent_id = node['id']
                    break

            nodes.append({"id": app_uid, "label": app_data['name'], "group": "access", "level": 4})
            edges.append({"from": parent_id, "to": app_uid})

            policies = client.fetch(f"access/apps/{app_id}/policies")
            for p_idx, policy in enumerate(policies):
                for m_idx, member in enumerate(policy.get('include', [])):
                    for key, val in member.items():
                        m_id = f"m_{app_id}_{str(val)}"
                        if not any(n['id'] == m_id for n in nodes):
                            nodes.append({"id": m_id, "label": str(val), "group": "member", "level": 5})
                        edges.append({"from": app_uid, "to": m_id})

    except Exception as e:
        print(f"❌ [CARLA-CF] Graph Build Error: {e}")

    return {"nodes": nodes, "edges": edges}

def _fetch_and_cache_task():
    global is_fetching
    with fetch_lock:
        if is_fetching:
            return
        is_fetching = True

    print("\n" + "="*60)
    print("⏳ [CARLA] Live-Abfrage (SSH & Cloudflare) im Hintergrund gestartet...")
    print("="*60)

    try:
        docker_data = ssh_docker.fetch_docker_data(
            config.SSH_HOST,
            config.SSH_USER,
            config.SSH_PASS,
            config.GITHUB_TOKEN,
        )

        cf = cloudflare.CloudflareClient(config.CF_API_TOKEN, config.CF_ACCOUNT_ID)
        mapping = cf.get_tunnel_mapping()
        access_info = cf.get_access_info()

        for stack in docker_data["stacks"].values():
            for container in stack:
                container["cloudflares"] = []
                l_url = container["local_url"].rstrip("/")
                if l_url in mapping:
                    for entry in mapping[l_url]:
                        container["cloudflares"].append({
                            "public_domain": entry["hostname"],
                            "tunnel_name": entry["tunnel"],
                            "allowed_emails": access_info.get(entry["hostname"], [])
                        })

        # 3. Baue Cloudflare Dashboard Graph Struktur direkt mit:
        cf_graph = _build_cf_graph_data(cf)
        
        # Kombiniere Docker- und Cloudflare-Daten
        full_data = docker_data
        full_data["cf_graph"] = cf_graph
        
        # In den JSON-Cache!
        cache.save(CACHE_KEY, full_data)

        # Baseline zurücksetzen – Discovery soll nach diesem Fetch
        # einen frischen Vergleichspunkt haben
        try:
            discovery.force_reset_baseline()
        except Exception:
            pass
        
        print("✅ [CARLA] Hintergrund-Abfrage abgeschlossen! Daten im SQL-Cache abgelegt.\n")
    except Exception as e:
        print(f"❌ [CARLA] Abfrage fehlgeschlagen: {e}\n")
    finally:
        with fetch_lock:
            is_fetching = False

def start_background_fetch():
    """Startet die Live-Abfrage als unblockierenden Hintergrund-Thread."""
    threading.Thread(target=_fetch_and_cache_task, daemon=True).start()

# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------

@bp.route("/")
@bp.route("/stack/<stack_name>")
@bp.route("/stack/<stack_name>/performance")
@bp.route("/container/<name>/terminal")
@bp.route("/performance")
@bp.route("/timeline")
@bp.route("/carla")
@bp.route("/infrastructure")
@bp.route("/backup")
@bp.route("/settings")
@bp.route("/livemap")
@bp.route("/ports")
@bp.route("/filemanager")
def index(stack_name=None, name=None):
    return render_template("dashboard.html")

@bp.route("/api/timeline/snapshots", methods=["GET"])
def api_timeline_list():
    limit = request.args.get("limit", 100, type=int)
    snapshots = metrics_db.get_timeline_snapshots(limit=limit)
    return jsonify(snapshots)

@bp.route("/api/timeline/snapshot/<int:ts>", methods=["GET"])
def api_timeline_detail(ts):
    snapshot = metrics_db.get_full_snapshot(ts)
    return jsonify(snapshot)

@bp.route("/api/full-infrastructure")
def get_full_infrastructure():
    global is_fetching
    data, timestamp = cache.load(CACHE_KEY)

    if data is None:
        if not is_fetching:
            start_background_fetch()
        return jsonify({"status": "loading", "message": "Initiale Server-Abfrage läuft..."}), 202

    data["_cache_timestamp"] = timestamp
    data["_from_cache"] = True
    data["_is_updating"] = is_fetching
    return jsonify(data)

@bp.route("/api/discovery/status", methods=["GET"])
def api_discovery_status():
    """Gibt den Status des Auto-Discovery-Daemons zurück."""
    return jsonify({
        "running":          discovery._running,
        "last_fingerprint": (discovery._last_fingerprint or "")[:12] + "…"
                            if discovery._last_fingerprint else None,
        "check_interval_s": discovery.CHECK_INTERVAL,
        "is_fetching":      is_fetching,
    })

@bp.route("/api/metrics/server", methods=["GET"])
def api_metrics_server():
    limit = request.args.get("limit", 60, type=int)
    history = metrics_db.get_server_metrics_history(limit=limit)
    return jsonify(history)

@bp.route("/api/metrics/stacks", methods=["GET"])
def api_metrics_stacks():
    stacks = metrics_db.get_latest_stack_metrics()
    return jsonify(stacks)

@bp.route("/api/metrics/containers", methods=["GET"])
def api_metrics_containers():
    containers = metrics_db.get_latest_container_metrics()
    return jsonify(containers)

@bp.route("/api/metrics/network", methods=["GET"])
def api_metrics_network():
    """Gibt Live-Netzwerk-Aktivitaet pro Container zurueck (Delta zwischen letzten 2 Messungen)."""
    data = metrics_db.get_container_net_activity()
    return jsonify(data)


@bp.route("/api/metrics/stack/<stack_name>", methods=["GET"])
def api_stack_performance(stack_name):
    limit = request.args.get("limit", 60, type=int)
    history = metrics_db.get_stack_history(stack_name, limit=limit)
    return jsonify(history)

@bp.route("/api/container/<name>/logs", methods=["GET"])
def api_container_logs(name):
    # Security: In Produktion sollte hier eine Validierung gegen den Cache erfolgen
    logs = ssh_docker.fetch_container_logs(config.SSH_HOST, config.SSH_USER, config.SSH_PASS, name)
    return jsonify({"logs": logs})

@bp.route("/api/container/<name>/logs-since-start", methods=["GET"])
def api_container_logs_since_start(name):
    logs = ssh_docker.fetch_container_logs_since_last_start(config.SSH_HOST, config.SSH_USER, config.SSH_PASS, name)
    return jsonify({"logs": logs})

@bp.route("/api/container/<name>/exec", methods=["POST"])
def api_container_exec(name):
    cmd = request.json.get("command")
    if not cmd: return jsonify({"error": "Kein Befehl gesendet"}), 400
    output = ssh_docker.execute_container_command(config.SSH_HOST, config.SSH_USER, config.SSH_PASS, name, cmd)
    return jsonify({"output": output})

@bp.route("/api/container/<name>/<action>", methods=["POST"])
def api_container_action(name, action):
    allowed = ("start", "stop", "restart", "pause", "unpause")
    if action not in allowed:
        return jsonify({"error": f"Unerlaubte Aktion: {action}"}), 400
    output = ssh_docker.container_action(name, action)
    has_error = output and ("Error" in output or "error" in output)
    return jsonify({"output": output, "action": action, "container": name, "success": not has_error})

@bp.route("/api/stack/<name>/<action>", methods=["POST"])
def api_stack_action(name, action):
    allowed = ("start", "stop", "restart", "down", "update")
    if action not in allowed:
        return jsonify({"error": f"Unerlaubte Aktion: {action}"}), 400
    output = ssh_docker.stack_action(name, action)
    has_error = output and output.startswith("Fehler:")
    return jsonify({"output": output, "action": action, "stack": name, "success": not has_error})

@bp.route("/api/stack/deploy", methods=["POST"])
def api_stack_deploy():
    """Erstellt und startet einen neuen Stack aus einer Compose-Datei."""
    data = request.json
    if not data:
        return jsonify({"error": "Keine Daten erhalten"}), 400

    stack_name = data.get("stack_name", "").strip()
    compose_content = data.get("compose", "").strip()
    env_content = data.get("env", "").strip()

    if not stack_name:
        return jsonify({"error": "Stack-Name ist erforderlich."}), 400
    if not compose_content:
        return jsonify({"error": "Compose-Datei ist erforderlich."}), 400

    # Nur alphanumerisch, Bindestrich und Unterstrich erlauben
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', stack_name):
        return jsonify({"error": "Stack-Name darf nur Buchstaben, Zahlen, - und _ enthalten."}), 400

    result = ssh_docker.deploy_stack(stack_name, compose_content, env_content)
    if result.get("ok"):
        start_background_fetch()
        return jsonify(result)
    else:
        return jsonify(result), 500


@bp.route("/api/refresh", methods=["POST"])
def manual_refresh():
    global is_fetching
    if is_fetching:
        return jsonify({"status": "loading", "message": "Refresh läuft bereits im Hintergrund."}), 202

    start_background_fetch()
    return jsonify({"status": "started", "message": "Hintergrund-Refresh angestoßen."}), 202


# ---------------------------------------------------------------
# Setup Routes
# ---------------------------------------------------------------

@bp.route("/setup")
def setup_page():
    if setup.is_setup_done():
        return redirect("/")
    return render_template("setup.html")


@bp.route("/api/setup", methods=["POST"])
def api_setup_save():
    data = request.json
    if not data:
        return jsonify({"error": "Keine Daten erhalten"}), 400

    mode = data.get("mode")
    if mode not in ("local", "ssh"):
        return jsonify({"error": "Ungueltiger Modus"}), 400

    if mode == "ssh" and not data.get("ssh_host"):
        return jsonify({"error": "SSH Host ist erforderlich"}), 400

    setup.save_setup(data)
    config.reload()

    # Starte Hintergrund-Abfrage nach Setup
    from services import metrics_worker, system_executor
    system_executor.close_ssh()
    metrics_worker.start_daemon()
    updater.start_daemon()
    backup.start_scheduler()
    start_background_fetch()

    return jsonify({"status": "ok"})


@bp.route("/api/setup", methods=["GET"])
def api_setup_status():
    return jsonify({"setup_done": setup.is_setup_done()})


@bp.route("/api/setup/reset", methods=["POST"])
def api_setup_reset():
    setup.delete_setup()
    return jsonify({"status": "ok", "message": "Setup zurueckgesetzt. Neustart erforderlich."})


@bp.route("/api/setup/keys", methods=["GET"])
def api_setup_keys_get():
    """Gibt die aktuellen API-Keys maskiert zurueck."""
    data = setup.load_setup()
    def mask(val):
        if not val:
            return ""
        if len(val) <= 8:
            return "*" * len(val)
        return val[:4] + "*" * (len(val) - 8) + val[-4:]

    return jsonify({
        "github_token": mask(data.get("github_token", "")),
        "cf_api_token": mask(data.get("cf_api_token", "")),
        "cf_account_id": mask(data.get("cf_account_id", "")),
        "gdrive_client_id": mask(data.get("gdrive_client_id", "")),
        "gdrive_client_secret": mask(data.get("gdrive_client_secret", "")),
        "gdrive_refresh_token": mask(data.get("gdrive_refresh_token", "")),
        "mode": data.get("mode", "local"),
    })


@bp.route("/api/setup/keys", methods=["PUT"])
def api_setup_keys_update():
    """Aktualisiert einzelne API-Keys ohne das gesamte Setup zurueckzusetzen."""
    incoming = request.json
    if not incoming:
        return jsonify({"error": "Keine Daten erhalten"}), 400

    allowed_keys = ("github_token", "cf_api_token", "cf_account_id",
                     "gdrive_client_id", "gdrive_client_secret", "gdrive_refresh_token")
    data = setup.load_setup()

    changed = False
    for key in allowed_keys:
        if key in incoming and incoming[key]:
            data[key] = incoming[key]
            changed = True

    if not changed:
        return jsonify({"error": "Keine gültigen Keys angegeben"}), 400

    setup.save_setup(data)
    config.reload()

    # Cache leeren damit neue Keys verwendet werden
    cache.clear(CACHE_KEY)
    from services import system_executor
    system_executor.close_ssh()
    start_background_fetch()

    return jsonify({"status": "ok", "message": "API-Keys aktualisiert."})


# ---------------------------------------------------------------
# Cloudflare Tunnel Management Routes
# ---------------------------------------------------------------

def _get_cf_client():
    """Erstellt einen CloudflareClient mit aktuellen Config-Werten."""
    if not config.CF_API_TOKEN or not config.CF_ACCOUNT_ID:
        return None
    return cloudflare.CloudflareClient(config.CF_API_TOKEN, config.CF_ACCOUNT_ID)


@bp.route("/api/cf/tunnels", methods=["GET"])
def api_cf_tunnels():
    """Listet alle Cloudflare Tunnel auf."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400
    tunnels = cf.list_tunnels()
    return jsonify(tunnels)


@bp.route("/api/cf/tunnel/<tunnel_id>/ingress", methods=["GET"])
def api_cf_tunnel_ingress(tunnel_id):
    """Gibt die Ingress-Regeln eines Tunnels zurueck."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400
    rules = cf.get_tunnel_ingress(tunnel_id)
    return jsonify(rules)


@bp.route("/api/cf/tunnel/<tunnel_id>/ingress", methods=["PUT"])
def api_cf_tunnel_ingress_update(tunnel_id):
    """Aktualisiert die gesamte Ingress-Konfiguration eines Tunnels."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400

    data = request.json
    if not data or "ingress" not in data:
        return jsonify({"error": "Keine Ingress-Regeln angegeben"}), 400

    result = cf.update_tunnel_ingress(tunnel_id, data["ingress"])
    if result["success"]:
        # Cache invalidieren damit Dashboard neue Daten zeigt
        cache.clear(CACHE_KEY)
        return jsonify({"status": "ok", "message": "Tunnel-Konfiguration aktualisiert."})
    else:
        errors = result.get("errors", [])
        msg = errors[0].get("message", "Unbekannter Fehler") if errors else "Unbekannter Fehler"
        return jsonify({"error": msg}), 400


@bp.route("/api/cf/tunnel/<tunnel_id>/ingress/add", methods=["POST"])
def api_cf_tunnel_ingress_add(tunnel_id):
    """Fuegt eine einzelne Ingress-Regel hinzu."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400

    data = request.json
    hostname = (data or {}).get("hostname", "").strip()
    service = (data or {}).get("service", "").strip()
    if not hostname or not service:
        return jsonify({"error": "hostname und service sind erforderlich"}), 400

    # Bestehende Regeln laden und neue einfuegen (vor dem Catch-All)
    rules = cf.get_tunnel_ingress(tunnel_id)
    new_rules = [r for r in rules if not r.get("is_catchall")]
    new_rules.append({"hostname": hostname, "service": service})
    # Catch-All wieder anhaengen
    catchall = [r for r in rules if r.get("is_catchall")]
    if catchall:
        new_rules.append(catchall[0])
    else:
        new_rules.append({"service": "http_status:404", "is_catchall": True})

    result = cf.update_tunnel_ingress(tunnel_id, new_rules)
    if result["success"]:
        cache.clear(CACHE_KEY)
        return jsonify({"status": "ok", "message": f"Route {hostname} hinzugefuegt."})
    errors = result.get("errors", [])
    msg = errors[0].get("message", "Unbekannter Fehler") if errors else "Unbekannter Fehler"
    return jsonify({"error": msg}), 400


@bp.route("/api/cf/tunnel/<tunnel_id>/ingress/<int:index>", methods=["DELETE"])
def api_cf_tunnel_ingress_delete(tunnel_id, index):
    """Loescht eine Ingress-Regel anhand ihres Index."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400

    rules = cf.get_tunnel_ingress(tunnel_id)
    # Nur nicht-Catchall Regeln zaehlen
    non_catchall = [r for r in rules if not r.get("is_catchall")]
    if index < 0 or index >= len(non_catchall):
        return jsonify({"error": "Ungueltiger Index"}), 400

    removed = non_catchall.pop(index)
    # Catch-All wieder anhaengen
    catchall = [r for r in rules if r.get("is_catchall")]
    new_rules = non_catchall
    if catchall:
        new_rules.append(catchall[0])
    else:
        new_rules.append({"service": "http_status:404", "is_catchall": True})

    result = cf.update_tunnel_ingress(tunnel_id, new_rules)
    if result["success"]:
        cache.clear(CACHE_KEY)
        return jsonify({"status": "ok", "message": f"Route {removed.get('hostname', '')} entfernt."})
    errors = result.get("errors", [])
    msg = errors[0].get("message", "Unbekannter Fehler") if errors else "Unbekannter Fehler"
    return jsonify({"error": msg}), 400


@bp.route("/api/cf/tunnel/<tunnel_id>/ingress/<int:index>", methods=["PUT"])
def api_cf_tunnel_ingress_edit(tunnel_id, index):
    """Bearbeitet eine bestehende Ingress-Regel."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400

    data = request.json
    hostname = (data or {}).get("hostname", "").strip()
    service = (data or {}).get("service", "").strip()
    if not hostname or not service:
        return jsonify({"error": "hostname und service sind erforderlich"}), 400

    rules = cf.get_tunnel_ingress(tunnel_id)
    non_catchall = [r for r in rules if not r.get("is_catchall")]
    if index < 0 or index >= len(non_catchall):
        return jsonify({"error": "Ungueltiger Index"}), 400

    non_catchall[index] = {"hostname": hostname, "service": service}
    catchall = [r for r in rules if r.get("is_catchall")]
    new_rules = non_catchall
    if catchall:
        new_rules.append(catchall[0])
    else:
        new_rules.append({"service": "http_status:404", "is_catchall": True})

    result = cf.update_tunnel_ingress(tunnel_id, new_rules)
    if result["success"]:
        cache.clear(CACHE_KEY)
        return jsonify({"status": "ok", "message": f"Route {hostname} aktualisiert."})
    errors = result.get("errors", [])
    msg = errors[0].get("message", "Unbekannter Fehler") if errors else "Unbekannter Fehler"
    return jsonify({"error": msg}), 400


# ---------------------------------------------------------------
# Publish / DNS Routes ("interner Port -> oeffentliche Domain")
# ---------------------------------------------------------------

@bp.route("/api/cf/zones", methods=["GET"])
def api_cf_zones():
    """Listet alle Cloudflare-Zonen (Domains) des Accounts."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400
    return jsonify(cf.list_zones())


@bp.route("/api/cf/zone/<zone_id>/dns", methods=["GET"])
def api_cf_zone_dns(zone_id):
    """Listet alle DNS-Eintraege einer Zone."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400
    return jsonify(cf.list_dns_records(zone_id))


@bp.route("/api/cf/publish", methods=["POST"])
def api_cf_publish():
    """One-Shot: macht einen internen Service unter einer oeffentlichen Domain
    erreichbar (Tunnel-Ingress + DNS CNAME + optional Zero Trust Access).

    Body: { tunnel_id, hostname, service, access_emails?: ["a@b.de", "@firma.de"] }
    """
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400

    data = request.json or {}
    tunnel_id = (data.get("tunnel_id") or "").strip()
    hostname = (data.get("hostname") or "").strip()
    service = (data.get("service") or "").strip()
    access_emails = data.get("access_emails") or []

    if not tunnel_id or not hostname or not service:
        return jsonify({"error": "tunnel_id, hostname und service sind erforderlich"}), 400

    result = cf.publish_service(tunnel_id, hostname, service, access_emails)
    cache.clear(CACHE_KEY)

    if result["success"]:
        return jsonify({"status": "ok",
                        "message": f"{hostname} ist jetzt oeffentlich erreichbar.",
                        "steps": result["steps"]})
    # Teilweise/komplett fehlgeschlagen — Fehlermeldungen sammeln
    msgs = []
    for step, info in result["steps"].items():
        if not info.get("success"):
            errs = info.get("errors", [])
            detail = errs[0].get("message", "Fehler") if errs else "Fehler"
            msgs.append(f"{step}: {detail}")
    return jsonify({"error": " | ".join(msgs) or "Veroeffentlichung fehlgeschlagen.",
                    "steps": result["steps"]}), 400


@bp.route("/api/cf/dns", methods=["POST"])
def api_cf_dns_point():
    """Generisch: 'diese IP/dieses Ziel soll auf diese Domain/Subdomain zeigen'.

    Body: { name, content, type?: A|AAAA|CNAME, proxied?: bool }
    """
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400

    data = request.json or {}
    name = (data.get("name") or "").strip()
    content = (data.get("content") or "").strip()
    rtype = (data.get("type") or "").strip().upper() or None
    proxied = bool(data.get("proxied", True))

    if not name or not content:
        return jsonify({"error": "name und content (Ziel-IP/Domain) sind erforderlich"}), 400

    result = cf.point_dns(name, content, rtype, proxied)
    if result.get("success"):
        return jsonify({"status": "ok",
                        "message": f"{name} ({result.get('type')}) -> {content} {result.get('action', '')}.".strip()})
    errs = result.get("errors", [])
    msg = errs[0].get("message", "Unbekannter Fehler") if errs else "Unbekannter Fehler"
    return jsonify({"error": msg}), 400


@bp.route("/api/cf/dns/<zone_id>/<record_id>", methods=["DELETE"])
def api_cf_dns_delete(zone_id, record_id):
    """Loescht einen DNS-Eintrag."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert"}), 400
    result = cf.delete_dns_record(zone_id, record_id)
    if result.get("success"):
        return jsonify({"status": "ok", "message": "DNS-Eintrag entfernt."})
    errs = result.get("errors", [])
    msg = errs[0].get("message", "Unbekannter Fehler") if errs else "Unbekannter Fehler"
    return jsonify({"error": msg}), 400


# ---------------------------------------------------------------
# Auto-Update Routes
# ---------------------------------------------------------------

@bp.route("/api/updater/config", methods=["GET"])
def api_updater_config_get():
    cfg = updater.load_config()
    cfg["available_stacks"] = updater.get_available_stacks()
    return jsonify(cfg)


@bp.route("/api/updater/config", methods=["POST"])
def api_updater_config_save():
    data = request.json
    if not data:
        return jsonify({"error": "Keine Daten"}), 400
    cfg = updater.load_config()
    cfg["enabled"] = data.get("enabled", cfg.get("enabled", False))
    cfg["time"] = data.get("time", cfg.get("time", "04:00"))
    cfg["mode"] = data.get("mode", cfg.get("mode", "all"))
    cfg["stacks"] = data.get("stacks", cfg.get("stacks", []))
    updater.save_config(cfg)
    return jsonify({"status": "ok"})


@bp.route("/api/updater/run", methods=["POST"])
def api_updater_run_now():
    data = request.json or {}
    stacks = data.get("stacks", None)
    threading.Thread(target=updater.run_update, args=(stacks,), daemon=True).start()
    return jsonify({"status": "started", "message": "Update im Hintergrund gestartet."})


@bp.route("/api/updater/log", methods=["GET"])
def api_updater_log():
    return jsonify(updater.get_log())


# ---------------------------------------------------------------
# Backup Routes
# ---------------------------------------------------------------

@bp.route("/api/backup/config", methods=["GET"])
def api_backup_config_get():
    cfg = backup.load_config()
    return jsonify(cfg)


@bp.route("/api/backup/config", methods=["POST"])
def api_backup_config_save():
    data = request.json
    if not data:
        return jsonify({"error": "Keine Daten"}), 400
    cfg = backup.load_config()
    if "backup_dir" in data:
        cfg["backup_dir"] = data["backup_dir"]
    if "schedule_enabled" in data:
        cfg["schedule_enabled"] = bool(data["schedule_enabled"])
    if "schedule_time" in data:
        cfg["schedule_time"] = data["schedule_time"]
    if "schedule_mode" in data:
        cfg["schedule_mode"] = data["schedule_mode"]
    if "schedule_stacks" in data:
        cfg["schedule_stacks"] = data["schedule_stacks"] or []
    if "gdrive_auto_upload" in data:
        cfg["gdrive_auto_upload"] = bool(data["gdrive_auto_upload"])
    backup.save_config(cfg)
    return jsonify({"status": "ok"})


@bp.route("/api/backup/stacks", methods=["GET"])
def api_backup_stacks():
    """Listet alle verfuegbaren Stacks fuer die Schedule-Auswahl."""
    return jsonify(updater.get_available_stacks())


@bp.route("/api/backup/run", methods=["POST"])
def api_backup_run():
    data = request.json or {}
    stacks = data.get("stacks", None)
    threading.Thread(target=backup.run_backup, args=(stacks,), daemon=True).start()
    return jsonify({"status": "started", "message": "Backup im Hintergrund gestartet."})


@bp.route("/api/backup/progress", methods=["GET"])
def api_backup_progress():
    return jsonify(backup.get_progress())


@bp.route("/api/backup/log", methods=["GET"])
def api_backup_log():
    return jsonify(backup.get_log())


@bp.route("/api/backup/list", methods=["GET"])
def api_backup_list():
    return jsonify(backup.list_backups())


@bp.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    data = request.json
    if not data or not data.get("backup_id"):
        return jsonify({"error": "Keine Backup-ID angegeben"}), 400
    backup_id = data["backup_id"]
    stacks = data.get("stacks", None)
    threading.Thread(target=backup.run_restore, args=(backup_id, stacks), daemon=True).start()
    return jsonify({"status": "started", "message": "Restore im Hintergrund gestartet."})


# ---------------------------------------------------------------
# Google Drive Routes
# ---------------------------------------------------------------

@bp.route("/api/gdrive/test", methods=["POST"])
def api_gdrive_test():
    """Testet die Google Drive Verbindung."""
    if not config.GDRIVE_CLIENT_ID or not config.GDRIVE_CLIENT_SECRET or not config.GDRIVE_REFRESH_TOKEN:
        return jsonify({"ok": False, "error": "Google Drive nicht konfiguriert. Bitte Client ID, Client Secret und Refresh Token in den Einstellungen eingeben."})
    result = google_drive.test_connection(
        config.GDRIVE_CLIENT_ID, config.GDRIVE_CLIENT_SECRET, config.GDRIVE_REFRESH_TOKEN
    )
    return jsonify(result)


@bp.route("/api/gdrive/list", methods=["GET"])
def api_gdrive_list():
    """Listet alle Backups auf Google Drive."""
    if not config.GDRIVE_CLIENT_ID or not config.GDRIVE_CLIENT_SECRET or not config.GDRIVE_REFRESH_TOKEN:
        return jsonify({"ok": False, "error": "Google Drive nicht konfiguriert.", "backups": []})
    return jsonify(google_drive.list_backups(
        config.GDRIVE_CLIENT_ID, config.GDRIVE_CLIENT_SECRET, config.GDRIVE_REFRESH_TOKEN
    ))


@bp.route("/api/gdrive/upload", methods=["POST"])
def api_gdrive_upload():
    """Laedt ein lokales Backup auf Google Drive hoch."""
    if not config.GDRIVE_CLIENT_ID or not config.GDRIVE_CLIENT_SECRET or not config.GDRIVE_REFRESH_TOKEN:
        return jsonify({"ok": False, "error": "Google Drive nicht konfiguriert."}), 400
    data = request.json or {}
    backup_id = data.get("backup_id")
    if not backup_id:
        return jsonify({"ok": False, "error": "Keine Backup-ID angegeben."}), 400
    cfg = backup.load_config()
    backup_dir = cfg.get("backup_dir", "/backup/carla")

    def _do_upload():
        google_drive.upload_backup(
            config.GDRIVE_CLIENT_ID, config.GDRIVE_CLIENT_SECRET, config.GDRIVE_REFRESH_TOKEN,
            backup_dir, backup_id
        )

    threading.Thread(target=_do_upload, daemon=True).start()
    return jsonify({"status": "started", "message": "Upload gestartet."})


@bp.route("/api/gdrive/download", methods=["POST"])
def api_gdrive_download():
    """Laedt ein Backup von Google Drive herunter (ohne Restore)."""
    if not config.GDRIVE_CLIENT_ID or not config.GDRIVE_CLIENT_SECRET or not config.GDRIVE_REFRESH_TOKEN:
        return jsonify({"ok": False, "error": "Google Drive nicht konfiguriert."}), 400
    data = request.json or {}
    file_id = data.get("file_id")
    if not file_id:
        return jsonify({"ok": False, "error": "Keine Datei-ID angegeben."}), 400
    cfg = backup.load_config()
    backup_dir = cfg.get("backup_dir", "/backup/carla")

    def _do_download():
        google_drive.download_backup(
            config.GDRIVE_CLIENT_ID, config.GDRIVE_CLIENT_SECRET, config.GDRIVE_REFRESH_TOKEN,
            file_id, backup_dir
        )

    threading.Thread(target=_do_download, daemon=True).start()
    return jsonify({"status": "started", "message": "Download gestartet."})


@bp.route("/api/gdrive/restore", methods=["POST"])
def api_gdrive_restore():
    """Laedt ein Backup von Google Drive herunter und stellt es wieder her."""
    if not config.GDRIVE_CLIENT_ID or not config.GDRIVE_CLIENT_SECRET or not config.GDRIVE_REFRESH_TOKEN:
        return jsonify({"ok": False, "error": "Google Drive nicht konfiguriert."}), 400
    data = request.json or {}
    file_id = data.get("file_id")
    if not file_id:
        return jsonify({"ok": False, "error": "Keine Datei-ID angegeben."}), 400
    cfg = backup.load_config()
    backup_dir = cfg.get("backup_dir", "/backup/carla")

    def _do_restore():
        result = google_drive.download_backup(
            config.GDRIVE_CLIENT_ID, config.GDRIVE_CLIENT_SECRET, config.GDRIVE_REFRESH_TOKEN,
            file_id, backup_dir
        )
        if result.get("ok"):
            backup.run_restore(result["backup_id"])

    threading.Thread(target=_do_restore, daemon=True).start()
    return jsonify({"status": "started", "message": "Download und Wiederherstellung gestartet."})


@bp.route("/api/gdrive/backup/<file_id>", methods=["DELETE"])
def api_gdrive_delete(file_id):
    """Loescht ein Backup von Google Drive."""
    if not config.GDRIVE_CLIENT_ID or not config.GDRIVE_CLIENT_SECRET or not config.GDRIVE_REFRESH_TOKEN:
        return jsonify({"ok": False, "error": "Google Drive nicht konfiguriert."}), 400
    return jsonify(google_drive.delete_backup(
        config.GDRIVE_CLIENT_ID, config.GDRIVE_CLIENT_SECRET, config.GDRIVE_REFRESH_TOKEN,
        file_id
    ))


@bp.route("/api/gdrive/progress", methods=["GET"])
def api_gdrive_progress():
    """Gibt den aktuellen Google Drive Upload/Download Fortschritt zurueck."""
    return jsonify(google_drive.get_progress())


# ---------------------------------------------------------------
# Ports Routes
# ---------------------------------------------------------------

@bp.route("/api/ports", methods=["GET"])
def api_ports():
    """Gibt alle offenen Ports mit Docker/Cloudflare-Anreicherung zurueck."""
    host_ports = ports.get_host_ports()
    data, _ = cache.load(CACHE_KEY)
    docker_stacks = (data or {}).get("stacks", {}) if data else {}
    enriched = ports.enrich_with_docker(host_ports, docker_stacks)
    return jsonify(enriched)


# ---------------------------------------------------------------
# File Manager Routes
# ---------------------------------------------------------------

@bp.route("/api/files/browse", methods=["GET"])
def api_files_browse():
    """Listet Dateien und Ordner in einem Verzeichnis."""
    path = request.args.get("path", "/")
    return jsonify(file_manager.list_directory(path))


@bp.route("/api/files/read", methods=["GET"])
def api_files_read():
    """Liest den Inhalt einer Textdatei."""
    path = request.args.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "Kein Pfad angegeben."}), 400
    return jsonify(file_manager.read_file(path))


@bp.route("/api/files/write", methods=["POST"])
def api_files_write():
    """Schreibt Inhalt in eine Datei."""
    data = request.json
    if not data or not data.get("path") or "content" not in data:
        return jsonify({"ok": False, "error": "Pfad und Inhalt erforderlich."}), 400
    return jsonify(file_manager.write_file(data["path"], data["content"]))


@bp.route("/api/files/mkdir", methods=["POST"])
def api_files_mkdir():
    """Erstellt ein neues Verzeichnis."""
    data = request.json
    if not data or not data.get("path"):
        return jsonify({"ok": False, "error": "Pfad erforderlich."}), 400
    return jsonify(file_manager.create_directory(data["path"]))


@bp.route("/api/files/delete", methods=["POST"])
def api_files_delete():
    """Loescht eine Datei oder ein Verzeichnis."""
    data = request.json
    if not data or not data.get("path"):
        return jsonify({"ok": False, "error": "Pfad erforderlich."}), 400
    return jsonify(file_manager.delete_item(data["path"]))


@bp.route("/api/files/rename", methods=["POST"])
def api_files_rename():
    """Benennt eine Datei oder Ordner um."""
    data = request.json
    if not data or not data.get("path") or not data.get("new_name"):
        return jsonify({"ok": False, "error": "Pfad und neuer Name erforderlich."}), 400
    return jsonify(file_manager.rename_item(data["path"], data["new_name"]))


@bp.route("/api/files/stack/<stack_name>", methods=["GET"])
def api_files_stack(stack_name):
    """Gibt Arbeitsverzeichnis und Volumes eines Stacks zurueck."""
    return jsonify(file_manager.get_stack_paths(stack_name))


@bp.route("/api/files/stack/<stack_name>/compose", methods=["GET"])
def api_files_stack_compose(stack_name):
    """Liest die Compose-Datei und parst Volumes/Bind-Mounts."""
    return jsonify(file_manager.get_stack_compose(stack_name))
