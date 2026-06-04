# ==============================================================
# CARLA – Flask Routes
# Definiert alle URL-Endpunkte der Webanwendung.
# ==============================================================

from flask import Blueprint, render_template, jsonify, request, redirect, url_for
import threading
from urllib.parse import urlparse
from services import cloudflare, docker_service, cache, metrics_db, setup, updater, backup, ports, discovery, google_drive, file_manager, redirect_service, error_server, static_server, vhost_server
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
                    label = ""
                    if "email" in member:
                        val = member["email"]
                        label = val.get("email", str(val)) if isinstance(val, dict) else str(val)
                    elif "email_domain" in member:
                        val = member["email_domain"]
                        dom = val.get("domain", str(val)) if isinstance(val, dict) else str(val)
                        label = f"@{dom}"
                    elif "group" in member:
                        group = member["group"]
                        group_name = group.get("name", group.get("id", str(group))) if isinstance(group, dict) else str(group)
                        label = f"Gruppe: {group_name}"
                    elif "everyone" in member:
                        label = "Jeder"
                    else:
                        for key, val in member.items():
                            label = f"{key}: {val}"
                            break
                    
                    if label:
                        m_id = f"m_{app_id}_{label}"
                        if not any(n['id'] == m_id for n in nodes):
                            nodes.append({"id": m_id, "label": label, "group": "member", "level": 5})
                        edges.append({"from": app_uid, "to": m_id})

    except Exception as e:
        print(f"❌ [CARLA-CF] Graph Build Error: {e}")

    return {"nodes": nodes, "edges": edges}

def _build_cf_index(mapping: dict) -> dict:
    """Baut einen exakten IP:PORT-Index aus dem CF Tunnel-Mapping.
    Keys: 'host_ip:port' (z.B. '10.7.0.1:1014') und 'localhost:port'.
    """
    index = {}
    for svc_url, entries in mapping.items():
        normalized = svc_url if "://" in svc_url else "http://" + svc_url
        try:
            p = urlparse(normalized)
            host = (p.hostname or "").lower()
            port = p.port
            if host and port:
                index.setdefault(f"{host}:{port}", []).extend(entries)
                # localhost-Aliase
                if host in ("127.0.0.1", "::1"):
                    index.setdefault(f"localhost:{port}", []).extend(entries)
        except Exception:
            pass
    return index


def _match_container_to_cf(container: dict, cf_index: dict, access_info: dict) -> list:
    """Findet CF-Domains per exaktem IP:PORT-Match gegen die Container-Port-Bindings."""
    matched = []

    for binding in container.get("port_bindings", []):
        host_ip = binding.get("host_ip", "")
        hp = binding.get("host_port")
        if not hp:
            continue
        # Exakter Match: z.B. '10.7.0.1:1014'
        if host_ip:
            matched.extend(cf_index.get(f"{host_ip}:{hp}", []))
        # Fallback: 0.0.0.0 bindet an alle Interfaces → auch localhost und andere IPs prüfen
        if host_ip in ("0.0.0.0", "", "::"):
            for key, entries in cf_index.items():
                if key.endswith(f":{hp}"):
                    matched.extend(entries)

    # Deduplizieren nach Domain + Access-Info anhängen
    seen = set()
    result = []
    for entry in matched:
        domain = entry["hostname"]
        if domain not in seen:
            seen.add(domain)
            result.append({
                "public_domain": domain,
                "tunnel_name": entry["tunnel"],
                "allowed_emails": access_info.get(domain, [])
            })
    return result


def _fetch_and_cache_task():
    global is_fetching
    with fetch_lock:
        if is_fetching:
            return
        is_fetching = True

    print("\n" + "="*60)
    print("⏳ [CARLA] Live-Abfrage im Hintergrund gestartet...")
    print("="*60)

    try:
        # Stufe 1: Docker-Daten holen und sofort cachen
        docker_data = docker_service.fetch_docker_data(config.GITHUB_TOKEN)

        try:
            docker_data["vhosts"] = vhost_server.list_sites()
            docker_data["redirects"] = redirect_service.list_redirects()
        except Exception as e:
            print(f"⚠️ [CARLA] vhosts/redirects Fehler: {e}")
            docker_data["vhosts"] = []
            docker_data["redirects"] = []

        # Local-URLs für CARLA-eigene Container setzen
        try:
            site_ports = {s["name"]: s["port"] for s in static_server.list_sites()}
        except Exception:
            site_ports = {}

        for stack in docker_data["stacks"].values():
            for container in stack:
                container["cloudflares"] = []
                c_name = container["name"]
                if c_name == "carla-vhost":
                    container["local_url"] = "http://localhost:10050"
                    container["host_ports"] = [10050]
                elif c_name.startswith("redirect-"):
                    try:
                        port = int(c_name.split("-")[1])
                        container["local_url"] = f"http://localhost:{port}"
                        container["host_ports"] = [port]
                    except Exception:
                        pass
                elif c_name.startswith("carla-site-"):
                    site_name = c_name[len("carla-site-"):]
                    port = site_ports.get(site_name)
                    if port:
                        container["local_url"] = f"http://localhost:{port}"
                        container["host_ports"] = [port]

        docker_data["cf_graph"] = {}
        cache.save(CACHE_KEY, docker_data)
        print("✅ [CARLA] Docker-Daten gecacht.")

        # Stufe 2: Cloudflare-Daten laden und Container matchen
        cf = _get_cf_client()
        if cf:
            try:
                mapping = cf.get_tunnel_mapping()
                access_info = cf.get_access_info()
                cf_index = _build_cf_index(mapping)

                print(f"☁️  [CARLA] CF Mapping: {len(mapping)} Service-URLs, Index-Keys: {len(cf_index)}")

                matched_total = 0
                for stack in docker_data["stacks"].values():
                    for container in stack:
                        container["cloudflares"] = _match_container_to_cf(
                            container, cf_index, access_info
                        )
                        if container["cloudflares"]:
                            domains = [c["public_domain"] for c in container["cloudflares"]]
                            print(f"  ✓ {container['name']} → {domains}")
                            matched_total += len(container["cloudflares"])

                docker_data["cf_graph"] = _build_cf_graph_data(cf)
                cache.save(CACHE_KEY, docker_data)
                print(f"✅ [CARLA] CF-Matching abgeschlossen: {matched_total} Domain(s) zugeordnet.")
            except Exception as e:
                print(f"❌ [CARLA] CF-Matching fehlgeschlagen: {e}")
        else:
            print("⚠️  [CARLA] Cloudflare nicht konfiguriert – übersprungen.")

        try:
            discovery.force_reset_baseline()
        except Exception:
            pass

        print("✅ [CARLA] Hintergrund-Abfrage abgeschlossen!\n")
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
@bp.route("/redirects")
@bp.route("/sites")
@bp.route("/domains")
def index(stack_name=None, name=None):
    return render_template("dashboard.html")

@bp.route("/editor")
def editor_view():
    return render_template("editor.html")

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

@bp.route("/api/cf-debug-log")
def api_cf_debug_log():
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare nicht konfiguriert (API Token oder Account ID fehlt)"})

    try:
        mapping = cf.get_tunnel_mapping()
        access_info = cf.get_access_info()
        cf_index = _build_cf_index(mapping)
        docker_data = docker_service.fetch_docker_data(config.GITHUB_TOKEN)

        container_matching = []
        for stack_name, stack in docker_data.get("stacks", {}).items():
            for container in stack:
                matched = _match_container_to_cf(container, cf_index, access_info)
                container_matching.append({
                    "container_name": container["name"],
                    "port_bindings": container.get("port_bindings", []),
                    "matched_domains": [m["public_domain"] for m in matched],
                })

        return jsonify({
            "cloudflare_configured": True,
            "service_urls": list(mapping.keys()),
            "port_index_keys": sorted(port_index.keys()),
            "name_index_keys": sorted(name_index.keys()),
            "access_apps_count": len(access_info),
            "container_matching": container_matching,
        })
    except Exception as e:
        return jsonify({"error": f"Fehler: {e}"})

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
    logs = docker_service.fetch_container_logs(name)
    return jsonify({"logs": logs})

@bp.route("/api/container/<name>/logs-since-start", methods=["GET"])
def api_container_logs_since_start(name):
    logs = docker_service.fetch_container_logs_since_last_start(name)
    return jsonify({"logs": logs})

@bp.route("/api/container/<name>/exec", methods=["POST"])
def api_container_exec(name):
    cmd = request.json.get("command")
    if not cmd: return jsonify({"error": "Kein Befehl gesendet"}), 400
    output = docker_service.execute_container_command(name, cmd)
    return jsonify({"output": output})

@bp.route("/api/container/<name>/<action>", methods=["POST"])
def api_container_action(name, action):
    allowed = ("start", "stop", "restart", "pause", "unpause")
    if action not in allowed:
        return jsonify({"error": f"Unerlaubte Aktion: {action}"}), 400
    output = docker_service.container_action(name, action)
    has_error = output and ("Error" in output or "error" in output)
    return jsonify({"output": output, "action": action, "container": name, "success": not has_error})

@bp.route("/api/stack/<name>/<action>", methods=["POST"])
def api_stack_action(name, action):
    allowed = ("start", "stop", "restart", "down", "update")
    if action not in allowed:
        return jsonify({"error": f"Unerlaubte Aktion: {action}"}), 400
    output = docker_service.stack_action(name, action)
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

    result = docker_service.deploy_stack(stack_name, compose_content, env_content)
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

    setup.save_setup(data)
    config.reload()

    # Starte Hintergrund-Abfrage nach Setup
    from services import metrics_worker
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


@bp.route("/api/cf/access/groups", methods=["GET"])
def api_cf_access_groups():
    """Listet alle CF Access Groups (wiederverwendbare Zugriffsregeln)."""
    cf = _get_cf_client()
    if not cf:
        return jsonify([])
    try:
        return jsonify(cf.list_access_groups())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/cf/access/app", methods=["POST"])
def api_cf_access_app_create():
    """Erstellt eine CF Access Application für eine Domain."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"ok": False, "error": "Cloudflare nicht konfiguriert"}), 400
    data = request.json or {}
    domain = data.get("domain", "").strip()
    group_ids = data.get("group_ids", [])
    name = data.get("name", domain)
    if not domain:
        return jsonify({"ok": False, "error": "Domain erforderlich"}), 400
    result = cf.create_access_app(name, domain, group_ids)
    result["ok"] = result.get("success", False)
    return jsonify(result)


@bp.route("/api/cf/access/app", methods=["DELETE"])
def api_cf_access_app_delete():
    """Entfernt die CF Access Application für eine Domain."""
    cf = _get_cf_client()
    if not cf:
        return jsonify({"ok": False, "error": "Cloudflare nicht konfiguriert"}), 400
    data = request.json or {}
    domain = data.get("domain", "").strip()
    if not domain:
        return jsonify({"ok": False, "error": "Domain erforderlich"}), 400
    success = cf.delete_access_app_by_domain(domain)
    return jsonify({"ok": success})


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


# ---------------------------------------------------------------
# Redirect Server Endpoints
# ---------------------------------------------------------------

@bp.route("/api/redirects", methods=["GET"])
def api_list_redirects():
    return jsonify(redirect_service.list_redirects())


@bp.route("/api/redirects", methods=["POST"])
def api_create_redirect():
    data = request.json
    if not data:
        return jsonify({"error": "Keine Daten empfangen"}), 400
        
    port_val = data.get("port")
    rules = data.get("rules", [])
    cloudflare_data = data.get("cloudflare", None)
    
    try:
        port = int(port_val)
    except (ValueError, TypeError):
        return jsonify({"error": "Ungültiger Port. Port muss eine Zahl sein."}), 400
        
    if port < 1 or port > 65535:
        return jsonify({"error": "Port muss zwischen 1 und 65535 liegen."}), 400
        
    if not rules:
        return jsonify({"error": "Mindestens eine Weiterleitungsregel ist erforderlich."}), 400
        
    res = redirect_service.create_redirect(port, rules, cloudflare_data)
    if res.get("ok"):
        start_background_fetch()
        return jsonify({"success": True, "port": port})
    else:
        return jsonify({"error": res.get("error", "Fehler beim Erstellen der Weiterleitung.")}), 500


@bp.route("/api/redirects/<int:port>", methods=["DELETE"])
def api_delete_redirect(port):
    res = redirect_service.delete_redirect(port)
    if res.get("ok"):
        start_background_fetch()
        return jsonify({"success": True})
    else:
        return jsonify({"error": res.get("error", "Fehler beim Löschen.")}), 500


@bp.route("/api/redirects/<int:port>/<action>", methods=["POST"])
def api_redirect_action(port, action):
    res = redirect_service.execute_action(port, action)
    if res.get("ok"):
        start_background_fetch()
        return jsonify({"success": True, "output": res.get("output", "")})
    else:
        return jsonify({"error": res.get("error", "Fehler bei der Ausführung."), "output": res.get("output", "")}), 500


@bp.route("/api/cloudflare/tunnels", methods=["GET"])
def api_cloudflare_tunnels():
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare ist nicht konfiguriert."}), 400
    try:
        tunnels = cf.list_tunnels()
        return jsonify(tunnels)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------
# Error Server Routes
# ---------------------------------------------------------------

@bp.route("/api/errorserver/status", methods=["GET"])
def api_errorserver_status():
    return jsonify(error_server.get_status())


@bp.route("/api/errorserver/pages", methods=["GET"])
def api_errorserver_list():
    return jsonify(error_server.list_pages())


@bp.route("/api/errorserver/pages", methods=["POST"])
def api_errorserver_add():
    data = request.json or {}
    path = data.get("path", "").strip()
    title = data.get("title", "").strip()
    message = data.get("message", "").strip()
    if not path or not title:
        return jsonify({"ok": False, "error": "Pfad und Titel sind erforderlich."}), 400
    result = error_server.add_page(
        path=path, title=title, message=message,
        code=data.get("code", ""), color=data.get("color", "#7c3aed")
    )
    return jsonify(result), 200 if result["ok"] else 400


@bp.route("/api/errorserver/pages/<path:page_path>", methods=["PUT"])
def api_errorserver_update(page_path):
    data = request.json or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"ok": False, "error": "Titel ist erforderlich."}), 400
    result = error_server.update_page(
        path=page_path, title=title,
        message=data.get("message", "").strip(),
        code=data.get("code", ""), color=data.get("color", "#7c3aed")
    )
    return jsonify(result), 200 if result["ok"] else 400


@bp.route("/api/errorserver/pages/<path:page_path>", methods=["DELETE"])
def api_errorserver_delete(page_path):
    result = error_server.delete_page(page_path)
    return jsonify(result), 200 if result["ok"] else 400


@bp.route("/api/errorserver/ensure", methods=["POST"])
def api_errorserver_ensure():
    """Startet den Error-Server falls er nicht läuft."""
    try:
        error_server.ensure_server()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------
# Static Server Routes
# ---------------------------------------------------------------

@bp.route("/api/sites", methods=["GET"])
def api_sites_list():
    return jsonify(static_server.list_sites())


@bp.route("/api/sites", methods=["POST"])
def api_sites_create():
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name ist erforderlich."}), 400
    port = None
    port_val = data.get("port")
    if port_val:
        try:
            port = int(port_val)
            if port < 1 or port > 65535:
                return jsonify({"ok": False, "error": "Port muss zwischen 1 und 65535 liegen."}), 400
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Ungültiger Port."}), 400
    result = static_server.create_site(
        name=name, port=port,
        spa=bool(data.get("spa", False)),
        cloudflare_data=data.get("cloudflare")
    )
    if result.get("ok"):
        start_background_fetch()
    return jsonify(result), 200 if result["ok"] else 400


@bp.route("/api/sites/<name>", methods=["DELETE"])
def api_sites_delete(name):
    result = static_server.delete_site(name)
    if result.get("ok"):
        start_background_fetch()
    return jsonify(result), 200 if result["ok"] else 400


@bp.route("/api/sites/<name>/<action>", methods=["POST"])
def api_sites_action(name, action):
    result = static_server.execute_action(name, action)
    if result.get("ok"):
        start_background_fetch()
    return jsonify(result), 200 if result["ok"] else 500


@bp.route("/api/sites/<name>/config", methods=["PUT"])
def api_sites_config(name):
    data = request.json or {}
    result = static_server.update_config(name, spa=bool(data.get("spa", False)))
    return jsonify(result), 200 if result["ok"] else 400


# ---------------------------------------------------------------
# Virtual Host Server Routes (geteilter nginx-Container)
# ---------------------------------------------------------------

@bp.route("/api/vhosts", methods=["GET"])
def api_vhosts_list():
    return jsonify(vhost_server.list_sites())


@bp.route("/api/vhosts", methods=["POST"])
def api_vhosts_add():
    data = request.json or {}
    name = data.get("name", "").strip()
    domain_input = data.get("domain", "").strip()
    tunnel_id = data.get("tunnel_id", "").strip()
    spa = bool(data.get("spa", False))
    if not name or not domain_input or not tunnel_id:
        return jsonify({"ok": False, "error": "Name, Domain und Tunnel sind erforderlich."}), 400
    extra = [h.strip() for h in data.get("extra_hostnames", []) if h.strip()]
    result = vhost_server.add_site(name=name, domain_input=domain_input, tunnel_id=tunnel_id, spa=spa, extra_hostnames=extra)
    if result.get("ok"):
        start_background_fetch()
    return jsonify(result), 200 if result["ok"] else 400


@bp.route("/api/vhosts/<name>", methods=["DELETE"])
def api_vhosts_delete(name):
    result = vhost_server.remove_site(name)
    if result.get("ok"):
        start_background_fetch()
    return jsonify(result), 200 if result["ok"] else 400


@bp.route("/api/vhosts/<name>", methods=["PUT"])
def api_vhosts_update(name):
    data = request.json or {}
    new_name = data.get("name", name).strip()
    domain_input = data.get("domain", "").strip()
    tunnel_id = data.get("tunnel_id", "").strip()
    spa = bool(data.get("spa", False))
    if not new_name or not domain_input or not tunnel_id:
        return jsonify({"ok": False, "error": "Name, Domain und Tunnel sind erforderlich."}), 400
    extra = [h.strip() for h in data.get("extra_hostnames", []) if h.strip()]
    result = vhost_server.update_site(old_name=name, new_name=new_name, domain_input=domain_input, tunnel_id=tunnel_id, spa=spa, extra_hostnames=extra)
    if result.get("ok"):
        start_background_fetch()
    return jsonify(result), 200 if result["ok"] else 400


# ---------------------------------------------------------------
# Cloudflare Public Domains Management Routes
# ---------------------------------------------------------------

@bp.route("/api/cloudflare/domains", methods=["GET"])
def api_cloudflare_domains_list():
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare ist nicht konfiguriert."}), 400

    try:
        # 1. Fetch zones, tunnels, and access policies
        tunnels = cf.list_tunnels()
        access_info = cf.get_access_info()
        
        # 2. Load cached infrastructure details for matching
        infra_data, _ = cache.load(CACHE_KEY)
        if infra_data is None:
            infra_data = {}

        domains = []
        
        # 3. Iterate over tunnels to retrieve ingress configurations
        for tun in tunnels:
            tun_id = tun["id"]
            tun_name = tun["name"]
            tun_status = tun.get("status", "unknown")
            
            # Fetch config for this specific tunnel
            cfg = cf.get_tunnel_config(tun_id)
            ingress_rules = cfg.get("config", {}).get("ingress", [])
            
            for rule in ingress_rules:
                hostname = rule.get("hostname")
                if not hostname:
                    continue  # skip catchall
                
                service = rule.get("service", "")
                
                # Zero trust detection
                zero_trust_emails = access_info.get(hostname, [])
                has_zero_trust = hostname in access_info
                
                # Match Zone ID
                zone_id = cf.find_zone_id(hostname)
                
                # Local service matching logic
                matched_target = None
                service_port = None
                
                normalized = service if "://" in service else "http://" + service
                try:
                    p = urlparse(normalized)
                    service_port = p.port
                except Exception:
                    pass
                
                if service_port is not None:
                    # a) Match against redirects
                    for red in infra_data.get("redirects", []):
                        if str(red.get("port")) == str(service_port):
                            matched_target = {
                                "type": "redirect",
                                "name": f"Umleitung (Port {service_port})",
                                "detail": f"→ {len(red.get('rules', []))} Pfade",
                                "status": "running" if red.get("state") == "running" else "stopped"
                            }
                            break
                    
                    # b) Match against containers
                    if not matched_target:
                        for stack_name, stack in infra_data.get("stacks", {}).items():
                            for container in stack:
                                host_ports = [str(pt) for pt in container.get("host_ports", [])]
                                if str(service_port) in host_ports:
                                    matched_target = {
                                        "type": "container",
                                        "name": container.get("name"),
                                        "stack": stack_name,
                                        "detail": f"Stack: {stack_name}",
                                        "status": container.get("state", "unknown")
                                    }
                                    break
                                
                                for binding in container.get("port_bindings", []):
                                    if str(binding.get("host_port")) == str(service_port):
                                        matched_target = {
                                            "type": "container",
                                            "name": container.get("name"),
                                            "stack": stack_name,
                                            "detail": f"Stack: {stack_name}",
                                            "status": container.get("state", "unknown")
                                        }
                                        break
                                if matched_target:
                                    break
                            if matched_target:
                                break
                    
                    # c) Match against sites (nginx vhosts)
                    if not matched_target:
                        for site in infra_data.get("vhosts", []):
                            if site.get("hostname") == hostname or hostname in site.get("extra_hostnames", []):
                                matched_target = {
                                    "type": "vhost",
                                    "name": f"Site: {site.get('name')}",
                                    "detail": f"Nginx-Pfad: {site.get('path', '/')}",
                                    "status": "running" if site.get("state") == "running" else "stopped"
                                }
                                break
                
                domains.append({
                    "hostname": hostname,
                    "tunnel_id": tun_id,
                    "tunnel_name": tun_name,
                    "tunnel_status": tun_status,
                    "service": service,
                    "zero_trust": has_zero_trust,
                    "access_policies": zero_trust_emails,
                    "zone_id": zone_id,
                    "matched_target": matched_target
                })
                
        return jsonify(domains)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@bp.route("/api/cloudflare/domains", methods=["DELETE"])
def api_cloudflare_domains_delete():
    cf = _get_cf_client()
    if not cf:
        return jsonify({"ok": False, "error": "Cloudflare ist nicht konfiguriert."}), 400

    data = request.json or {}
    hostname = data.get("hostname", "").strip()
    tunnel_id = data.get("tunnel_id", "").strip()
    zone_id = data.get("zone_id", "").strip()
    delete_dns = bool(data.get("delete_dns", True))
    delete_access = bool(data.get("delete_access", True))

    if not hostname or not tunnel_id:
        return jsonify({"ok": False, "error": "Hostname und Tunnel-ID sind erforderlich."}), 400

    try:
        errors = []
        
        # 1. Remove ingress rule from tunnel config
        rules = cf.get_tunnel_ingress(tunnel_id)
        # Filter out rules matching this hostname
        new_rules = [r for r in rules if r.get("hostname") != hostname]
        
        update_res = cf.update_tunnel_ingress(tunnel_id, new_rules)
        if not update_res.get("success"):
            update_errs = update_res.get("errors", [])
            err_msg = update_errs[0].get("message", "Fehler beim Aktualisieren des Ingress") if update_errs else "Fehler beim Ingress-Update"
            errors.append(f"Tunnel-Ingress: {err_msg}")

        # 2. Delete CNAME record if requested (non-fatal)
        if delete_dns:
            if not zone_id:
                # Find Zone ID dynamically if not provided
                zone_id = cf.find_zone_id(hostname)
            
            if zone_id:
                cf.delete_cname_record(zone_id, hostname)
            else:
                print(f"⚠️ [CARLA] DNS Zonen-ID für {hostname} nicht gefunden. DNS-Löschen übersprungen.")

        # 3. Delete Access App if requested
        if delete_access:
            cf.delete_access_app_by_domain(hostname)

        if errors:
            return jsonify({"ok": False, "error": "; ".join(errors)}), 400

        # Success - clean cache and trigger refresh in background
        cache.clear(CACHE_KEY)
        start_background_fetch()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------
# Cloudflare Zero Trust Access Applications Routes
# ---------------------------------------------------------------

@bp.route("/api/cloudflare/access-apps", methods=["GET"])
def api_cloudflare_access_apps_list():
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare ist nicht konfiguriert."}), 400
    
    try:
        apps = cf.fetch("access/apps")
        # Ensure apps is a list
        if not isinstance(apps, list):
            apps = []
            
        access_info = cf.get_access_info()
        
        result = []
        for app in apps:
            domain = app.get("domain", "")
            result.append({
                "id": app.get("id"),
                "name": app.get("name"),
                "domain": domain,
                "created_at": app.get("created_at"),
                "updated_at": app.get("updated_at"),
                "policies": access_info.get(domain, [])
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/cloudflare/access-apps/<app_id>", methods=["DELETE"])
def api_cloudflare_access_apps_delete(app_id):
    cf = _get_cf_client()
    if not cf:
        return jsonify({"ok": False, "error": "Cloudflare ist nicht konfiguriert."}), 400
    try:
        import requests
        url = f"{cf.base_url}/accounts/{cf.account_id}/access/apps/{app_id}"
        res = requests.delete(url, headers=cf.headers, timeout=10)
        resp = res.json()
        cf._cache.pop("access/apps", None) # clear cache
        return jsonify({"ok": resp.get("success", False), "error": resp.get("errors")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/cloudflare/access-apps/<app_id>/policies", methods=["GET"])
def api_cloudflare_access_apps_policies_get(app_id):
    cf = _get_cf_client()
    if not cf:
        return jsonify({"error": "Cloudflare ist nicht konfiguriert."}), 400
    try:
        import requests
        policies_url = f"{cf.base_url}/accounts/{cf.account_id}/access/apps/{app_id}/policies"
        res = requests.get(policies_url, headers=cf.headers, timeout=10)
        policies = res.json().get("result", [])
        if not isinstance(policies, list):
            policies = []
            
        selected_groups = []
        for policy in policies:
            for inc in policy.get("include", []):
                if "group" in inc:
                    g_id = inc["group"].get("id")
                    if g_id:
                        selected_groups.append(g_id)
        
        return jsonify({"group_ids": selected_groups})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/cloudflare/access-apps/<app_id>/policies", methods=["PUT"])
def api_cloudflare_access_apps_policies_update(app_id):
    cf = _get_cf_client()
    if not cf:
        return jsonify({"ok": False, "error": "Cloudflare ist nicht konfiguriert."}), 400
    
    data = request.json or {}
    group_ids = data.get("group_ids", [])
    
    try:
        import requests
        policies_url = f"{cf.base_url}/accounts/{cf.account_id}/access/apps/{app_id}/policies"
        res = requests.get(policies_url, headers=cf.headers, timeout=10)
        policies = res.json().get("result", [])
        
        if not isinstance(policies, list):
            policies = []
            
        if policies:
            policy_id = policies[0]["id"]
            policy_payload = {
                "name": "Ausgewählte Gruppen",
                "decision": "allow",
                "include": [{"group": {"id": gid}} for gid in group_ids]
            }
            put_res = requests.put(
                f"{policies_url}/{policy_id}",
                headers=cf.headers,
                json=policy_payload,
                timeout=10
            )
            ok = put_res.json().get("success", False)
            err = put_res.json().get("errors")
        else:
            policy_payload = {
                "name": "Ausgewählte Gruppen",
                "decision": "allow",
                "include": [{"group": {"id": gid}} for gid in group_ids]
            }
            post_res = requests.post(
                policies_url,
                headers=cf.headers,
                json=policy_payload,
                timeout=10
            )
            ok = post_res.json().get("success", False)
            err = post_res.json().get("errors")
            
        cf._cache.clear()
        return jsonify({"ok": ok, "error": err})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
