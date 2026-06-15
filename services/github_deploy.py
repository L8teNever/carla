# ==============================================================
# CARLA – GitHub Auto-Deploy Service
# ==============================================================

import subprocess
import time
import re
import threading

_daemon_started = False
_lock = threading.Lock()


def _run(cmd: str, timeout: int = 120) -> tuple[bool, str]:
    """Führt einen Shell-Befehl aus. Gibt (success, combined_output) zurück."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        out = (r.stdout + "\n" + r.stderr).strip()
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "Timeout beim Ausführen des Befehls."
    except Exception as e:
        return False, str(e)


def _build_clone_url(repo_url: str, token: str) -> str:
    if token and "github.com" in repo_url:
        repo_url = re.sub(r'https://[^@]+@', 'https://', repo_url)
        repo_url = repo_url.replace('https://', f'https://{token}@')
    return repo_url


def clone_repo(site_dir: str, repo_url: str, branch: str = "main", token: str = "") -> dict:
    """Klont ein Repo in site_dir (überschreibt bestehende Dateien)."""
    clone_url = _build_clone_url(repo_url, token)
    tmp_dir = f"/tmp/carla-git-{int(time.time() * 1000)}"

    ok, out = _run(f"git clone --depth=1 --branch {branch} {clone_url} {tmp_dir}", timeout=120)
    if not ok:
        _run(f"rm -rf {tmp_dir}")
        return {"ok": False, "error": out[:400] or "Clone fehlgeschlagen (unbekannter Fehler)."}

    ok2, _ = _run(f"mkdir -p {site_dir} && cp -rf {tmp_dir}/. {site_dir}/")
    _run(f"rm -rf {tmp_dir}")

    if not ok2:
        return {"ok": False, "error": "Dateien konnten nicht ins Site-Verzeichnis kopiert werden."}

    _, commit = _run(f"git -C {site_dir} rev-parse --short HEAD")
    commit = commit.strip().splitlines()[0] if commit.strip() else ""
    return {"ok": True, "commit": commit}


def pull_repo(site_dir: str, repo_url: str, branch: str = "main", token: str = "") -> dict:
    """Pulled neueste Änderungen. Fällt auf clone_repo zurück wenn kein .git."""
    has_git, _ = _run(f"test -d {site_dir}/.git")
    if not has_git:
        return clone_repo(site_dir, repo_url, branch, token)

    if token:
        clone_url = _build_clone_url(repo_url, token)
        _run(f"git -C {site_dir} remote set-url origin {clone_url}")

    ok, out = _run(
        f"git -C {site_dir} fetch --depth=1 origin {branch} "
        f"&& git -C {site_dir} reset --hard origin/{branch}",
        timeout=60,
    )
    if not ok:
        return {"ok": False, "error": out[:400] or "Pull fehlgeschlagen."}

    _, commit = _run(f"git -C {site_dir} rev-parse --short HEAD")
    commit = commit.strip().splitlines()[0] if commit.strip() else ""
    return {"ok": True, "commit": commit}


def _auto_deploy_loop():
    from services import vhost_server
    while True:
        try:
            sites = vhost_server._load_meta()
            now = int(time.time())
            changed = False
            for site in sites:
                repo_url = site.get("github_repo", "")
                if not repo_url:
                    continue
                interval = site.get("auto_deploy_interval", 0)
                if not interval:
                    continue
                if now - site.get("last_deployed_at", 0) < interval * 60:
                    continue

                name = site["name"]
                site_dir = f"{vhost_server.SITES_DIR}/{name}"
                res = pull_repo(site_dir, repo_url,
                                site.get("github_branch", "main"),
                                site.get("github_token", ""))
                if res.get("ok"):
                    site["last_deployed_at"] = now
                    site["last_commit"] = res.get("commit", "")
                    changed = True
                    print(f"✅ [GH-Deploy] {name}: {res.get('commit', '')}")
                else:
                    print(f"❌ [GH-Deploy] {name}: {res.get('error', '')[:100]}")

            if changed:
                vhost_server._save_meta(sites)
        except Exception as e:
            print(f"❌ [GH-Deploy] Loop error: {e}")
        time.sleep(60)


def start_daemon():
    global _daemon_started
    with _lock:
        if _daemon_started:
            return
        _daemon_started = True
    threading.Thread(target=_auto_deploy_loop, daemon=True).start()
    print("[CARLA] GitHub Auto-Deploy Daemon gestartet.")
