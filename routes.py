# ==============================================================
# CARLA – Flask Routes
# Definiert alle URL-Endpunkte der Webanwendung.
# ==============================================================

from flask import Blueprint, render_template, jsonify, request, redirect, url_for
import threading
from urllib.parse import urlparse
from services import cloudflare, ssh_docker, cache, metrics_db, setup, updater, backup
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
@bp.route("/backup")
@bp.route("/settings")
@bp.route("/livemap")
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
    return jsonify({"output": output, "action": action, "container": name})

@bp.route("/api/stack/<name>/<action>", methods=["POST"])
def api_stack_action(name, action):
    allowed = ("start", "stop", "restart", "down", "update")
    if action not in allowed:
        return jsonify({"error": f"Unerlaubte Aktion: {action}"}), 400
    output = ssh_docker.stack_action(name, action)
    return jsonify({"output": output, "action": action, "stack": name})

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
        "mode": data.get("mode", "local"),
    })


@bp.route("/api/setup/keys", methods=["PUT"])
def api_setup_keys_update():
    """Aktualisiert einzelne API-Keys ohne das gesamte Setup zurueckzusetzen."""
    incoming = request.json
    if not incoming:
        return jsonify({"error": "Keine Daten erhalten"}), 400

    allowed_keys = ("github_token", "cf_api_token", "cf_account_id")
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
    cfg["backup_dir"] = data.get("backup_dir", cfg.get("backup_dir", "/backup/carla"))
    backup.save_config(cfg)
    return jsonify({"status": "ok"})


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
