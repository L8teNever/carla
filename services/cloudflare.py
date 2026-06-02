# ==============================================================
# CARLA – Cloudflare Service
# Kapselt alle Cloudflare API-Anfragen.
# ==============================================================

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed


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

        def _fetch_app_policies(app):
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
            return app["domain"], list(set(entries))

        access_info = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_app_policies, app): app for app in access_apps}
            for future in as_completed(futures):
                domain, entries = future.result()
                access_info[domain] = entries
        return access_info

    def list_tunnels(self) -> list:
        """Gibt alle Tunnel mit ID und Name zurueck."""
        tunnels = self.fetch("cfd_tunnel")
        return [{"id": t["id"], "name": t["name"], "status": t.get("status", "unknown")} for t in tunnels]

    def get_tunnel_ingress(self, tunnel_id: str) -> list:
        """Gibt alle Ingress-Regeln eines Tunnels zurueck."""
        cfg = self.get_tunnel_config(tunnel_id)
        ingress = cfg.get("config", {}).get("ingress", [])
        result = []
        for i, rule in enumerate(ingress):
            result.append({
                "index": i,
                "hostname": rule.get("hostname", ""),
                "service": rule.get("service", ""),
                "is_catchall": "hostname" not in rule,
            })
        return result

    def update_tunnel_ingress(self, tunnel_id: str, ingress_rules: list) -> dict:
        """Schreibt die komplette Ingress-Konfiguration eines Tunnels.
        ingress_rules: Liste von dicts mit 'hostname' und 'service'.
        Der letzte Eintrag muss der Catch-All (ohne hostname) sein.
        """
        # Stelle sicher, dass ein Catch-All existiert
        has_catchall = any("hostname" not in r or r.get("is_catchall") for r in ingress_rules)
        cleaned = []
        for r in ingress_rules:
            if r.get("is_catchall") or not r.get("hostname"):
                continue
            entry = {"hostname": r["hostname"], "service": r["service"]}
            if r.get("path"):
                entry["path"] = r["path"]
            cleaned.append(entry)

        # Catch-All am Ende (Pflicht bei Cloudflare)
        catchall_service = "http_status:404"
        for r in ingress_rules:
            if r.get("is_catchall") or "hostname" not in r:
                catchall_service = r.get("service", "http_status:404")
                break
        cleaned.append({"service": catchall_service})

        payload = {"config": {"ingress": cleaned}}

        try:
            res = requests.put(
                f"{self.base_url}/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations",
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            resp = res.json()
            # Cache invalidieren
            self._cache.pop(f"t_cfg_{tunnel_id}", None)
            if resp.get("success"):
                return {"success": True}
            return {"success": False, "errors": resp.get("errors", [])}
        except Exception as e:
            return {"success": False, "errors": [{"message": str(e)}]}

    def list_zones(self) -> list:
        """Gibt eine Liste aller Zonen (Domains) zurück."""
        key = "cf_zones"
        if key in self._cache:
            return self._cache[key]
        try:
            res = requests.get(
                f"{self.base_url}/zones",
                headers=self.headers,
                timeout=5,
            )
            data = res.json().get("result", [])
            self._cache[key] = data
            return data
        except Exception:
            return []

    def find_zone_id(self, hostname: str) -> str:
        """Findet die passende Zone-ID für einen gegebenen Hostname."""
        zones = self.list_zones()
        # Sortiere Zonen nach Namenslänge absteigend, um das spezifischste Suffix zuerst zu finden
        sorted_zones = sorted(zones, key=lambda z: len(z.get("name", "")), reverse=True)
        for zone in sorted_zones:
            name = zone.get("name", "")
            if hostname == name or hostname.endswith(f".{name}"):
                return zone.get("id")
        return ""

    def create_cname_record(self, zone_id: str, hostname: str, target: str) -> dict:
        """Erstellt einen CNAME-Eintrag in der Zone."""
        url = f"{self.base_url}/zones/{zone_id}/dns_records"
        payload = {
            "type": "CNAME",
            "name": hostname,
            "content": target,
            "ttl": 1,          # 1 = Automatic
            "proxied": True
        }
        try:
            res = requests.post(
                url,
                headers=self.headers,
                json=payload,
                timeout=5
            )
            resp = res.json()
            if resp.get("success"):
                return {"success": True, "id": resp.get("result", {}).get("id")}
            return {"success": False, "errors": resp.get("errors", [])}
        except Exception as e:
            return {"success": False, "errors": [{"message": str(e)}]}

    def delete_cname_record(self, zone_id: str, hostname: str) -> bool:
        """Sucht nach CNAME-Einträgen für den Hostname in der Zone und löscht sie."""
        url = f"{self.base_url}/zones/{zone_id}/dns_records"
        try:
            # 1. Datensatz suchen
            res = requests.get(
                url,
                headers=self.headers,
                params={"name": hostname, "type": "CNAME"},
                timeout=5
            )
            records = res.json().get("result", [])
            
            # 2. Alle passenden Datensätze löschen
            success = True
            for record in records:
                rec_id = record.get("id")
                del_res = requests.delete(
                    f"{url}/{rec_id}",
                    headers=self.headers,
                    timeout=5
                )
                if not del_res.json().get("success"):
                    success = False
            return success
        except Exception:
            return False
