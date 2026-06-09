# ==============================================================
# CARLA – Cloudflare Service
# Kapselt alle Cloudflare API-Anfragen.
# ==============================================================

import re
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

    # --------------------------------------------------------------
    # Generischer Request-Helfer (mit voller URL, account- oder zone-scoped)
    # --------------------------------------------------------------

    def _request(self, method: str, url: str, payload: dict | None = None,
                 params: dict | None = None) -> dict:
        """Fuehrt einen API-Request aus und gibt ein normalisiertes Resultat zurueck:
        {'success': bool, 'result': ..., 'errors': [...]}.
        """
        try:
            res = requests.request(
                method, url, headers=self.headers,
                json=payload, params=params, timeout=10,
            )
            data = res.json()
            return {
                "success": bool(data.get("success")),
                "result": data.get("result"),
                "errors": data.get("errors", []),
            }
        except Exception as e:
            return {"success": False, "result": None, "errors": [{"message": str(e)}]}

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
            if r.get("is_catchall") or "hostname" not in r:
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

    # ==============================================================
    # DNS / Zonen
    # ==============================================================

    def list_zones(self) -> list:
        """Gibt alle Zonen (Domains) des Accounts zurueck: [{'id','name'}, ...]."""
        if "zones" in self._cache:
            return self._cache["zones"]
        res = self._request(
            "GET", f"{self.base_url}/zones",
            params={"account.id": self.account_id, "per_page": 50},
        )
        zones = [{"id": z["id"], "name": z["name"]} for z in (res.get("result") or [])]
        self._cache["zones"] = zones
        return zones

    def find_zone_for_hostname(self, hostname: str) -> dict | None:
        """Findet die passende Zone fuer einen Hostnamen (laengste Uebereinstimmung).
        z.B. app.dev.example.com -> Zone example.com.
        """
        hostname = hostname.strip().lower().rstrip(".")
        best = None
        for z in self.list_zones():
            zn = z["name"].lower()
            if hostname == zn or hostname.endswith("." + zn):
                if best is None or len(zn) > len(best["name"]):
                    best = z
        return best

    def list_dns_records(self, zone_id: str) -> list:
        """Listet alle DNS-Eintraege einer Zone."""
        res = self._request(
            "GET", f"{self.base_url}/zones/{zone_id}/dns_records",
            params={"per_page": 200},
        )
        out = []
        for r in (res.get("result") or []):
            out.append({
                "id": r.get("id"),
                "type": r.get("type"),
                "name": r.get("name"),
                "content": r.get("content"),
                "proxied": r.get("proxied", False),
            })
        return out

    def upsert_dns_record(self, zone_id: str, name: str, rtype: str,
                          content: str, proxied: bool = True) -> dict:
        """Erstellt oder aktualisiert einen DNS-Eintrag (idempotent auf name+type)."""
        name = name.strip().lower().rstrip(".")
        existing = None
        for r in self.list_dns_records(zone_id):
            if r["name"].lower().rstrip(".") == name and r["type"] == rtype:
                existing = r
                break

        payload = {"type": rtype, "name": name, "content": content,
                   "proxied": proxied, "ttl": 1}
        if existing:
            res = self._request(
                "PUT",
                f"{self.base_url}/zones/{zone_id}/dns_records/{existing['id']}",
                payload=payload,
            )
            action = "updated"
        else:
            res = self._request(
                "POST", f"{self.base_url}/zones/{zone_id}/dns_records",
                payload=payload,
            )
            action = "created"
        res["action"] = action
        return res

    def delete_dns_record(self, zone_id: str, record_id: str) -> dict:
        return self._request(
            "DELETE",
            f"{self.base_url}/zones/{zone_id}/dns_records/{record_id}",
        )

    # ==============================================================
    # Zero Trust Access
    # ==============================================================

    def ensure_access_app(self, hostname: str, emails: list) -> dict:
        """Stellt sicher, dass fuer den Hostnamen eine Access-Application mit
        einer Allow-Policy fuer die angegebenen E-Mails existiert.
        emails: Liste von E-Mail-Adressen (exakt) oder '@domain.tld' fuer ganze Domains.
        """
        hostname = hostname.strip().lower().rstrip(".")
        # Existierende App suchen
        app_id = None
        for app in self.fetch("access/apps"):
            if app.get("domain", "").lower().rstrip("/") == hostname:
                app_id = app["id"]
                break

        if not app_id:
            res = self._request(
                "POST", f"{self.base_url}/accounts/{self.account_id}/access/apps",
                payload={
                    "name": hostname,
                    "domain": hostname,
                    "type": "self_hosted",
                    "session_duration": "24h",
                },
            )
            if not res["success"]:
                return res
            app_id = (res.get("result") or {}).get("id")
            self._cache.pop("access/apps", None)

        include = []
        for e in emails:
            e = e.strip()
            if not e:
                continue
            if e.startswith("@"):
                include.append({"email_domain": {"domain": e[1:]}})
            else:
                include.append({"email": {"email": e}})
        if not include:
            include = [{"everyone": {}}]

        res = self._request(
            "POST",
            f"{self.base_url}/accounts/{self.account_id}/access/apps/{app_id}/policies",
            payload={
                "name": f"Allow {hostname}",
                "decision": "allow",
                "include": include,
            },
        )
        res["app_id"] = app_id
        return res

    # ==============================================================
    # High-Level: Service veroeffentlichen / IP auf Domain zeigen
    # ==============================================================

    def publish_service(self, tunnel_id: str, hostname: str, service: str,
                        access_emails: list | None = None) -> dict:
        """One-Shot: macht einen internen Service unter einer oeffentlichen Domain
        erreichbar — komplett ueber Cloudflare Zero Trust Tunnel.

        Schritte:
          1. Ingress-Regel im Tunnel (hostname -> service)
          2. DNS CNAME (hostname -> <tunnel_id>.cfargotunnel.com, proxied)
          3. optional: Zero Trust Access-Policy (Zugriff auf E-Mails beschraenken)

        Gibt einen Report pro Schritt zurueck.
        """
        hostname = hostname.strip().lower().rstrip(".")
        service = service.strip()
        steps = {}

        # 1) Ingress-Regel hinzufuegen (vor Catch-All)
        rules = self.get_tunnel_ingress(tunnel_id)
        non_catchall = [r for r in rules if not r.get("is_catchall")]
        # Bestehende Regel fuer denselben Hostnamen ersetzen statt duplizieren
        non_catchall = [r for r in non_catchall if r.get("hostname") != hostname]
        non_catchall.append({"hostname": hostname, "service": service})
        catchall = [r for r in rules if r.get("is_catchall")]
        new_rules = non_catchall + (catchall or [{"service": "http_status:404", "is_catchall": True}])
        ing = self.update_tunnel_ingress(tunnel_id, new_rules)
        steps["ingress"] = {"success": ing.get("success", False),
                            "errors": ing.get("errors", [])}

        # 2) DNS CNAME auf den Tunnel zeigen lassen
        zone = self.find_zone_for_hostname(hostname)
        if not zone:
            steps["dns"] = {"success": False,
                            "errors": [{"message": f"Keine passende Cloudflare-Zone fuer '{hostname}' gefunden."}]}
        else:
            dns = self.upsert_dns_record(
                zone["id"], hostname, "CNAME",
                f"{tunnel_id}.cfargotunnel.com", proxied=True,
            )
            steps["dns"] = {"success": dns.get("success", False),
                            "action": dns.get("action"),
                            "errors": dns.get("errors", [])}

        # 3) Zero Trust Access (optional)
        if access_emails:
            acc = self.ensure_access_app(hostname, access_emails)
            steps["access"] = {"success": acc.get("success", False),
                               "errors": acc.get("errors", [])}

        self._cache.pop(f"t_cfg_{tunnel_id}", None)
        overall = all(s.get("success") for s in steps.values())
        return {"success": overall, "hostname": hostname, "steps": steps}

    def point_dns(self, name: str, content: str, rtype: str | None = None,
                  proxied: bool = True) -> dict:
        """Generisch: 'diese IP/dieses Ziel soll auf diese Domain/Subdomain zeigen'.
        rtype wird automatisch erkannt (IPv4 -> A, IPv6 -> AAAA, sonst CNAME),
        kann aber explizit gesetzt werden.
        """
        name = name.strip().lower().rstrip(".")
        content = content.strip()
        if not rtype:
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", content):
                rtype = "A"
            elif ":" in content:
                rtype = "AAAA"
            else:
                rtype = "CNAME"

        zone = self.find_zone_for_hostname(name)
        if not zone:
            return {"success": False,
                    "errors": [{"message": f"Keine passende Cloudflare-Zone fuer '{name}' gefunden."}]}
        res = self.upsert_dns_record(zone["id"], name, rtype, content, proxied=proxied)
        res["type"] = rtype
        res["zone"] = zone["name"]
        return res
