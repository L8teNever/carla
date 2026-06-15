# ==============================================================
# CARLA – GitHub Auto-Deploy Service
# Klont / pullt GitHub-Repos in Site-Verzeichnisse.
# Läuft als Daemon-Thread und deployed automatisch wenn
# auto_deploy_interval gesetzt ist.
# ==============================================================

import time
import re
import threading
from services import system_executor

_daemon_started = False
_lock = threading.Lock()


def _build_clone_url(repo_url: str, token: str) -> str:
    if token and "github.com" in repo_url:
        repo_url = re.sub(r'https://[^@]+@', 'https://', repo_url)
        repo_url = repo_url.replace('https://', f'https://{token}@')
    return repo_url


def clone_repo(site_dir: str, repo_url: str, branch: str = "main", token: str = "") -> dict:
    """Klont ein Repo und kopiert Dateien ins site_dir (überschreibt bestehende)."""
    clone_url = _build_clone_url(repo_url, token)
    tmp_dir = f"/tmp/carla-git-{int(time.time() * 1000)}"

    result = system_executor.execute_command(
        f"git clone --depth=1 --branch {branch} {clone_url} {tmp_dir} 2>&1",
        timeout=120
    )
    err = result.lower()
    if "error" in err or "fatal" in err or "could not" in err:
        system_executor.execute_command(f"rm -rf {tmp_dir}")
        return {"ok": False, "error": result[:400]}

    system_executor.execute_command(f"mkdir -p {site_dir}")
    system_executor.execute_command(f"cp -rf {tmp_dir}/. {site_dir}/")
    system_executor.execute_command(f"rm -rf {tmp_dir}")

    commit = system_executor.execute_command(
        f"git -C {site_dir} rev-parse --short HEAD 2>/dev/null"
    ).strip()
    return {"ok": True, "commit": commit or ""}


def pull_repo(site_dir: str, repo_url: str, branch: str = "main", token: str = "") -> dict:
    """Pulled neueste Änderungen. Fällt auf clone_repo zurück wenn kein .git."""
    git_check = system_executor.execute_command(
        f"test -d {site_dir}/.git && echo OK || echo NO"
    ).strip()
    if "NO" in git_check:
        return clone_repo(site_dir, repo_url, branch, token)

    if token:
        clone_url = _build_clone_url(repo_url, token)
        system_executor.execute_command(
            f"git -C {site_dir} remote set-url origin {clone_url} 2>/dev/null"
        )

    result = system_executor.execute_command(
        f"git -C {site_dir} fetch --depth=1 origin {branch} 2>&1 "
        f"&& git -C {site_dir} reset --hard origin/{branch} 2>&1",
        timeout=60
    )
    err = result.lower()
    if "error" in err or "fatal" in err:
        return {"ok": False, "error": result[:400]}

    commit = system_executor.execute_command(
        f"git -C {site_dir} rev-parse --short HEAD 2>/dev/null"
    ).strip()
    return {"ok": True, "commit": commit or ""}


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
                last = site.get("last_deployed_at", 0)
                if now - last < interval * 60:
                    continue

                name = site["name"]
                site_dir = f"{vhost_server.SITES_DIR}/{name}"
                res = pull_repo(
                    site_dir, repo_url,
                    site.get("github_branch", "main"),
                    site.get("github_token", ""),
                )
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
