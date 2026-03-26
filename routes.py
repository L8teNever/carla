# ==============================================================
# CARLA – Flask Routes
# Definiert alle URL-Endpunkte der Webanwendung.
# ==============================================================

from flask import Blueprint, render_template, jsonify, request, redirect, url_for
import threading
from urllib.parse import urlparse
from services import cloudflare, ssh_docker, cache, metrics_db, setup, updater
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
