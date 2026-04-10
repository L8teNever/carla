# ==============================================================
# CARLA – Ports Service
# Listet alle belegten TCP/UDP-Ports auf dem Host auf
# und reichert sie mit Docker- und Cloudflare-Informationen an.
# ==============================================================

import re
from . import system_executor


def _parse_ss_line(line: str) -> dict | None:
    """Parst eine Zeile aus 'ss -tulnp' Output.
    Beispiel:
    tcp   LISTEN 0  4096  0.0.0.0:8080  0.0.0.0:*  users:(("docker-proxy",pid=1234,fd=4))
    """
    parts = re.split(r"\s+", line.strip(), maxsplit=6)
    if len(parts) < 5:
        return None

    # Netid (tcp/udp), State, Recv-Q, Send-Q, Local Address:Port, Peer Address:Port, Process
    protocol = parts[0]
    if protocol not in ("tcp", "udp", "tcp6", "udp6"):
        return None

    # Local address ist meistens an Position 4 bei tcp/udp mit State
    # Format kann variieren, deshalb suchen wir nach dem Pattern IP:PORT
    local_addr = None
    for p in parts:
        if ":" in p and not p.startswith("users:") and p not in (protocol,):
            # IPv6 hat format [::]:port oder [::1]:port
            if p.startswith("[") or re.match(r"^[\d.]+:\d+$", p) or p.startswith("*:"):
                local_addr = p
                break

    if not local_addr:
        return None

    # IP und Port aus local_addr extrahieren
    if local_addr.startswith("["):
        # IPv6
        m = re.match(r"\[([^\]]+)\]:(\d+)", local_addr)
        if not m:
            return None
        ip, port = m.group(1), int(m.group(2))
    else:
        if ":" not in local_addr:
            return None
        ip, port_str = local_addr.rsplit(":", 1)
        if not port_str.isdigit():
            return None
        port = int(port_str)

    # Prozess-Info extrahieren: users:(("name",pid=X,fd=Y))
    process = ""
    pid = None
    proc_match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
    if proc_match:
        process = proc_match.group(1)
        try:
            pid = int(proc_match.group(2))
        except Exception:
            pass

    return {
        "ip": ip,
        "port": port,
        "protocol": protocol,
        "process": process,
        "pid": pid,
    }


def _classify_ip(ip: str) -> str:
    """Klassifiziert die IP-Adresse: public/private/local/loopback."""
    if ip in ("0.0.0.0", "::", "*"):
        return "public"
    if ip.startswith("127.") or ip == "::1":
        return "loopback"
    if (ip.startswith("10.") or
            ip.startswith("192.168.") or
            ip.startswith("172.16.") or ip.startswith("172.17.") or
            ip.startswith("172.18.") or ip.startswith("172.19.") or
            ip.startswith("172.2") or ip.startswith("172.30.") or
            ip.startswith("172.31.") or
            ip.startswith("fe80:") or ip.startswith("fc")):
        return "private"
    return "public"


def get_host_ports() -> list:
    """Gibt alle offenen Listening-Ports des Hosts zurueck."""
    # ss bevorzugt, sonst netstat
    out = system_executor.execute_command("ss -tulnp 2>/dev/null || netstat -tulnp 2>/dev/null")
    if not out or "Error" in out:
        return []

    ports = []
    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("Netid", "Active", "Proto", "State")):
            continue
        parsed = _parse_ss_line(line)
        if not parsed:
            continue
        key = (parsed["ip"], parsed["port"], parsed["protocol"])
        if key in seen:
            continue
        seen.add(key)
        parsed["scope"] = _classify_ip(parsed["ip"])
        ports.append(parsed)

    return ports


def _parse_docker_ports(ports_raw: str) -> list:
    """Parst einen 'docker ps' Ports-String in strukturierte Eintraege.
    Beispiel: '0.0.0.0:8080->80/tcp, 10.7.0.1:1070->8080/tcp, 443/tcp'
    """
    if not ports_raw:
        return []
    result = []
    for entry in ports_raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        # Format: HOST:PORT->CONTAINER_PORT/PROTO oder CONTAINER_PORT/PROTO
        m = re.match(r"(?:([^:]+):(\d+)->)?(\d+)/(\w+)", entry)
        if not m:
            continue
        host_ip = m.group(1) or ""
        host_port = int(m.group(2)) if m.group(2) else None
        container_port = int(m.group(3))
        protocol = m.group(4)
        result.append({
            "host_ip": host_ip,
            "host_port": host_port,
            "container_port": container_port,
            "protocol": protocol,
        })
    return result


def enrich_with_docker(host_ports: list, docker_stacks: dict) -> list:
    """Verknuepft Host-Ports mit Docker-Container-Daten.
    docker_stacks: dict von stack_name -> list of containers (aus fullInfrastructureData)
    """
    # Lookup-Tabelle: (host_ip, host_port) -> container_info
    docker_lookup = {}
    for stack_name, containers in (docker_stacks or {}).items():
        for cont in containers:
            parsed = _parse_docker_ports(cont.get("ports_raw", ""))
            for p in parsed:
                if p["host_port"] is None:
                    continue
                # Normalisiere leere IP auf 0.0.0.0
                host_ip = p["host_ip"] or "0.0.0.0"
                key = (host_ip, p["host_port"], p["protocol"])
                docker_lookup[key] = {
                    "container": cont.get("name", ""),
                    "stack": stack_name,
                    "image": cont.get("image", ""),
                    "state": cont.get("state", ""),
                    "container_port": p["container_port"],
                    "cloudflares": cont.get("cloudflares", []),
                    "local_url": cont.get("local_url", ""),
                }

    enriched = []
    matched_docker_keys = set()

    for hp in host_ports:
        ip = hp["ip"]
        port = hp["port"]
        proto = hp["protocol"].replace("6", "")  # tcp6 -> tcp

        # Exakter Match (IP + Port)
        info = docker_lookup.get((ip, port, proto))
        # Fallback: 0.0.0.0 matches wildcard auf jeder IP
        if not info:
            info = docker_lookup.get(("0.0.0.0", port, proto))
        # Fallback: host sieht :: aber docker exposed auf 0.0.0.0
        if not info and ip in ("::", "0.0.0.0"):
            # Suche beliebige IP mit diesem Port
            for k, v in docker_lookup.items():
                if k[1] == port and k[2] == proto:
                    info = v
                    break

        entry = {
            "ip": ip,
            "port": port,
            "protocol": hp["protocol"],
            "scope": hp["scope"],
            "process": hp.get("process", ""),
            "is_docker": bool(info),
        }

        if info:
            key = (info.get("_key_ip", "0.0.0.0"), port, proto)
            matched_docker_keys.add((ip, port, proto))
            entry.update({
                "container": info["container"],
                "stack": info["stack"],
                "image": info["image"],
                "state": info["state"],
                "container_port": info["container_port"],
                "local_url": info["local_url"] or f"http://{ip if ip not in ('0.0.0.0', '::', '*') else 'HOST'}:{port}",
                "cloudflares": info.get("cloudflares", []),
            })
        else:
            entry.update({
                "container": "",
                "stack": "",
                "image": "",
                "state": "",
                "container_port": None,
                "local_url": f"http://{ip if ip not in ('0.0.0.0', '::', '*') else 'localhost'}:{port}",
                "cloudflares": [],
            })

        enriched.append(entry)

    # Docker-Ports hinzufuegen, die nicht im ss-Output auftauchen
    # (z.B. wenn ss nicht verfuegbar oder Permission-Probleme)
    existing_keys = {(e["ip"], e["port"], e["protocol"].replace("6", "")) for e in enriched}
    for (host_ip, host_port, proto), info in docker_lookup.items():
        if (host_ip, host_port, proto) in existing_keys:
            continue
        if ("0.0.0.0", host_port, proto) in existing_keys:
            continue
        if (":", host_port, proto) in existing_keys:
            continue
        enriched.append({
            "ip": host_ip,
            "port": host_port,
            "protocol": proto,
            "scope": _classify_ip(host_ip),
            "process": "docker-proxy",
            "is_docker": True,
            "container": info["container"],
            "stack": info["stack"],
            "image": info["image"],
            "state": info["state"],
            "container_port": info["container_port"],
            "local_url": info["local_url"] or f"http://{host_ip}:{host_port}",
            "cloudflares": info.get("cloudflares", []),
        })

    # Sortiere nach Port
    enriched.sort(key=lambda e: (e["port"], e["ip"]))
    return enriched
