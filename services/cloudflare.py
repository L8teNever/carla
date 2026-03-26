# ==============================================================
# CARLA – Cloudflare Service
# Kapselt alle Cloudflare API-Anfragen.
# ==============================================================

import requests


class CloudflareClient:
    def __init__(self, token: str, account_id: str):
        self.token = token
        self.account_id = account_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.base_url = "https://api.cloudflare.com/client/v4"
        self._cache = {}

    def fetch(self, endpoint: str) -> list:
        if endpoint in self._cache:
            return self._cache[endpoint]
        try:
            res = requests.get(
                f"{self.base_url}/accounts/{self.account_id}/{endpoint}",
                headers=self.headers,
                timeout=5,
            )
            data = res.json().get("result", [])
            self._cache[endpoint] = data
            return data
        except Exception:
            return []

    def get_tunnel_config(self, tunnel_id: str) -> dict:
        key = f"t_cfg_{tunnel_id}"
        if key in self._cache:
            return self._cache[key]
        try:
            res = requests.get(
                f"{self.base_url}/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations",
                headers=self.headers,
                timeout=5,
            )
            data = res.json().get("result", {})
            self._cache[key] = data
            return data
        except Exception:
            return {}

    def get_tunnel_mapping(self) -> dict:
        """Gibt ein Dict zurück: local_service_url -> [{'hostname': ..., 'tunnel': ...}, ...]"""
        tunnels = self.fetch("cfd_tunnel")
        mapping = {}
        for tun in tunnels:
            config = self.get_tunnel_config(tun["id"])
            for ingress in config.get("config", {}).get("ingress", []):
                if "hostname" in ingress:
                    svc = ingress["service"].rstrip("/")
                    if svc not in mapping:
                        mapping[svc] = []
                    mapping[svc].append({
                        "hostname": ingress["hostname"],
                        "tunnel": tun["name"],
                    })
        return mapping

    def get_access_info(self) -> dict:
        """Gibt ein Dict zurück: domain -> [erlaubte E-Mails / Gruppen]"""
        access_apps = self.fetch("access/apps")
        access_info = {}
        for app in access_apps:
            policies = self.fetch(f"access/apps/{app['id']}/policies")
            entries = []
            for pol in policies:
                for inc in pol.get("include", []):
                    if "email" in inc:
                        val = inc["email"]
                        entries.append(val.get("email", str(val)) if isinstance(val, dict) else str(val))
                    elif "email_domain" in inc:
                        val = inc["email_domain"]
                        domain = val.get("domain", str(val)) if isinstance(val, dict) else str(val)
                        entries.append(f"@{domain}")
                    elif "group" in inc:
                        group = inc["group"]
                        group_id = group.get("name", group.get("id", str(group))) if isinstance(group, dict) else str(group)
                        entries.append(f"Gruppe: {group_id}")
                    elif "everyone" in inc:
                        entries.append("Jeder")
            access_info[app["domain"]] = list(set(entries))
        return access_info
