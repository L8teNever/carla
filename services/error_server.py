# ==============================================================
# CARLA – Error Server Service
# Verwaltet einen geteilten nginx-Container für benutzerdefinierte
# Fehler- und Statusseiten. Erreichbar unter error.{domain}/{path}.
# ==============================================================

import json
import base64
from services import system_executor

ERROR_SERVER_DIR = "/opt/stacks/carla-error-server"
ERROR_SERVER_PORT = 8404
ERROR_SERVER_NAME = "carla-error-server"
PAGES_DIR = f"{ERROR_SERVER_DIR}/pages"
PAGES_META_FILE = f"{ERROR_SERVER_DIR}/pages.json"


def _load_pages_meta() -> list:
    content = system_executor.execute_command(f"cat {PAGES_META_FILE} 2>/dev/null")
    if content and "Error" not in content and "No such file" not in content:
        try:
            return json.loads(content)
        except Exception:
            pass
    return []


def _save_pages_meta(pages: list):
    data = json.dumps(pages, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(data.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {PAGES_META_FILE}")


def _generate_page_html(page: dict) -> str:
    title = page.get("title", "Fehler")
    message = page.get("message", "")
    code = page.get("code", "")
    color = page.get("color", "#7c3aed")
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d0d0d;color:#ccc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{text-align:center;max-width:520px;padding:2rem}}
h1{{font-size:7rem;color:{color};line-height:1;font-weight:700}}
h2{{font-size:1.6rem;margin-top:.5rem;color:#fff;font-weight:500}}
p{{margin-top:1rem;font-size:1rem;line-height:1.7;color:#aaa}}
a{{color:{color};text-decoration:none;margin-top:2rem;display:inline-block;border:1px solid {color};padding:.5rem 1.4rem;border-radius:8px;transition:all .2s}}
a:hover{{background:{color};color:#fff}}
</style>
</head>
<body>
<div class="box">
{"<h1>" + code + "</h1>" if code else ""}
<h2>{title}</h2>
{"<p>" + message + "</p>" if message else ""}
<a href="javascript:history.back()">← Zurück</a>
</div>
</body>
</html>"""


def _write_page_html(page: dict):
    path = page.get("path", "").strip("/")
    html = _generate_page_html(page)
    encoded = base64.b64encode(html.encode()).decode()
    system_executor.execute_command(f"mkdir -p {PAGES_DIR}")
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {PAGES_DIR}/{path}.html")


def _generate_nginx_config(pages: list) -> str:
    lines = [
        "server {",
        f"    listen {ERROR_SERVER_PORT};",
        "    server_name localhost;",
        ""
    ]
    for page in pages:
        path = page.get("path", "").strip("/")
        lines += [
            f"    location = /{path} {{",
            f"        alias {PAGES_DIR}/{path}.html;",
            "        default_type text/html;",
            "    }",
            ""
        ]
    lines += [
        "    location / {",
        "        return 302 /404;",
        "    }",
        "}"
    ]
    return "\n".join(lines)


def _write_nginx_config(pages: list):
    nginx_conf = _generate_nginx_config(pages)
    encoded = base64.b64encode(nginx_conf.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {ERROR_SERVER_DIR}/nginx.conf")


def _reload_nginx():
    system_executor.execute_command(
        f"docker exec {ERROR_SERVER_NAME} nginx -s reload 2>/dev/null", timeout=10
    )


def is_running() -> bool:
    state = system_executor.execute_command(
        f"docker inspect --format '{{{{.State.Status}}}}' {ERROR_SERVER_NAME} 2>/dev/null"
    ).strip()
    return state == "running"


def ensure_server() -> bool:
    """Stellt sicher dass der Error-Server existiert und läuft."""
    system_executor.execute_command(f"mkdir -p {PAGES_DIR}")

    pages = _load_pages_meta()
    if not pages:
        default_pages = [
            {"path": "404", "code": "404", "title": "Seite nicht gefunden",
             "message": "Die angeforderte Seite existiert nicht.", "color": "#7c3aed"},
            {"path": "503", "code": "503", "title": "Service nicht verfügbar",
             "message": "Der Dienst ist gerade nicht erreichbar. Bitte versuche es später erneut.", "color": "#dc2626"},
            {"path": "maintenance", "code": "", "title": "Wartungsarbeiten",
             "message": "Diese Seite wird gerade gewartet. Wir sind bald wieder da.", "color": "#d97706"},
        ]
        _save_pages_meta(default_pages)
        pages = default_pages

    for page in pages:
        _write_page_html(page)

    _write_nginx_config(pages)

    compose = f"""services:
  nginx:
    image: nginx:alpine
    container_name: {ERROR_SERVER_NAME}
    restart: always
    network_mode: host
    volumes:
      - {ERROR_SERVER_DIR}/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - {PAGES_DIR}:{PAGES_DIR}:ro
"""
    encoded = base64.b64encode(compose.encode()).decode()
    system_executor.execute_command(f"echo '{encoded}' | base64 -d > {ERROR_SERVER_DIR}/docker-compose.yml")

    if not is_running():
        system_executor.execute_command(
            f"cd {ERROR_SERVER_DIR} && docker compose up -d 2>&1", timeout=120
        )
    else:
        _reload_nginx()

    return True


def list_pages() -> list:
    return _load_pages_meta()


def get_status() -> dict:
    return {
        "running": is_running(),
        "port": ERROR_SERVER_PORT,
        "pages": _load_pages_meta(),
    }


def add_page(path: str, title: str, message: str, code: str = "", color: str = "#7c3aed") -> dict:
    path = path.strip("/").strip()
    if not path:
        return {"ok": False, "error": "Pfad darf nicht leer sein."}

    pages = _load_pages_meta()
    if any(p.get("path") == path for p in pages):
        return {"ok": False, "error": f"Seite '/{path}' existiert bereits."}

    page = {"path": path, "code": code, "title": title, "message": message, "color": color}
    pages.append(page)

    ensure_server()
    _write_page_html(page)
    _save_pages_meta(pages)
    _write_nginx_config(pages)
    _reload_nginx()

    return {"ok": True}


def update_page(path: str, title: str, message: str, code: str = "", color: str = "#7c3aed") -> dict:
    path = path.strip("/")
    pages = _load_pages_meta()

    found = False
    for p in pages:
        if p.get("path") == path:
            p.update({"title": title, "message": message, "code": code, "color": color})
            found = True
            _write_page_html(p)
            break

    if not found:
        return {"ok": False, "error": f"Seite '/{path}' nicht gefunden."}

    _save_pages_meta(pages)
    _write_nginx_config(pages)
    _reload_nginx()

    return {"ok": True}


def delete_page(path: str) -> dict:
    path = path.strip("/")
    if path == "404":
        return {"ok": False, "error": "Die Standard-404-Seite kann nicht gelöscht werden."}

    pages = _load_pages_meta()
    new_pages = [p for p in pages if p.get("path") != path]

    if len(new_pages) == len(pages):
        return {"ok": False, "error": f"Seite '/{path}' nicht gefunden."}

    system_executor.execute_command(f"rm -f {PAGES_DIR}/{path}.html")
    _save_pages_meta(new_pages)
    _write_nginx_config(new_pages)
    _reload_nginx()

    return {"ok": True}
