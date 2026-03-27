import time
import threading
from . import system_executor
from . import metrics_db
import config


def parse_docker_memory(mem_str: str) -> float:
    # memory values like "59.21MiB" or "1.34GiB" or "800KiB"
    mem_str = mem_str.strip().upper()
    val = "".join(c for c in mem_str if c.isdigit() or c == '.')
    if not val:
        return 0.0
    val_float = float(val)
    if "GIB" in mem_str or "GB" in mem_str:
        return val_float * 1024.0
    if "KIB" in mem_str or "KB" in mem_str:
        return val_float / 1024.0
    return val_float  # fallback to MiB / MB


def run_metrics_daemon():
    while True:
        try:
            # 1. System Overall CPU/RAM Metrics
            cmd_cpu = "top -bn1 | grep 'Cpu(s)' | sed 's/.*, *\\([0-9.]*\\)%* id.*/\\1/' | awk '{print 100 - $1}'"
            cpu_total_str = system_executor.execute_command(cmd_cpu)
            server_cpu = float(cpu_total_str) if cpu_total_str and "Error" not in cpu_total_str else 0.0
            
            ram_str = system_executor.execute_command("free -m | grep Mem | awk '{print $3, $2}'")
            if ram_str and "Error" not in ram_str:
                used, total = ram_str.split()
                server_ram_used, server_ram_total = float(used), float(total)
            else:
                server_ram_used, server_ram_total = 0, 1000
                
            disk_str = system_executor.execute_command("df -m / | awk 'NR==2{print $3, $2}'")
            if disk_str and "Error" not in disk_str:
                d_parts = disk_str.split()
                server_disk_used = float(d_parts[0]) if len(d_parts) > 0 else 0.0
                server_disk_total = float(d_parts[1]) if len(d_parts) > 1 else 1000.0
            else:
                server_disk_used, server_disk_total = 0, 1000
                
            temp_str = system_executor.execute_command("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo '0'")
            server_temp = float(temp_str) / 1000.0 if temp_str.isdigit() else 0.0
            
            # 2. Docker Names -> Stacks & Uptime Mapping
            cmd_ps = "docker ps --format '{{.Names}}|{{.Label \"com.docker.compose.project\"}}|{{.Status}}'"
            ps_out = system_executor.execute_command(cmd_ps).splitlines()
            c_meta = {}
            for line in ps_out:
                parts = line.split("|")
                if len(parts) >= 1:
                    name = parts[0]
                    stack = parts[1] if len(parts) > 1 and parts[1] else ""
                    raw_uptime = parts[2] if len(parts) > 2 else "Unknown"
                    uptime = raw_uptime.replace("Up ", "").strip()
                    if not stack:
                        if "-" in name: stack = name.split("-")[0]
                        elif "_" in name: stack = name.split("_")[0]
                    c_meta[name] = {"stack": stack or "Einzelne", "uptime": uptime}

            # 3. Docker Stats
            cmd_stats = "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.NetIO}}'"
            stats_out = system_executor.execute_command(cmd_stats).splitlines()
            
            stack_totals = {}
            container_stats = []
            
            for line in stats_out:
                parts = line.split("|")
                if len(parts) >= 3:
                    c_name = parts[0]
                    meta = c_meta.get(c_name, {"stack": "Einzelne", "uptime": "N/A"})
                    stack = meta["stack"]
                    uptime = meta["uptime"]

                    cpu_perc = float(parts[1].replace("%", "").strip()) if parts[1] and "%" in parts[1] else 0.0
                    mem_raw = parts[2]
                    mem_used_part = mem_raw.split("/")[0].strip() if "/" in mem_raw else mem_raw.strip()
                    ram_mb = parse_docker_memory(mem_used_part)

                    # Network I/O parsing (e.g. "1.5MB / 2.3MB")
                    net_rx, net_tx = 0.0, 0.0
                    if len(parts) >= 4 and "/" in parts[3]:
                        try:
                            net_parts = parts[3].split("/")
                            net_rx = parse_docker_memory(net_parts[0])
                            net_tx = parse_docker_memory(net_parts[1])
                        except Exception:
                            pass

                    if stack not in stack_totals:
                        stack_totals[stack] = {"cpu": 0.0, "ram": 0.0}
                    stack_totals[stack]["cpu"] += cpu_perc
                    stack_totals[stack]["ram"] += ram_mb

                    container_stats.append({
                        "name": c_name,
                        "stack": stack,
                        "cpu": cpu_perc,
                        "ram": ram_mb,
                        "uptime": uptime,
                        "net_rx": net_rx,
                        "net_tx": net_tx,
                    })
            
            stack_data = [{"stack": k, "cpu": v["cpu"], "ram": v["ram"]} for k, v in stack_totals.items()]
            metrics_db.log_metrics(server_cpu, server_ram_used, server_ram_total, server_disk_used, server_disk_total, server_temp, stack_data, container_stats)
            
        except Exception as e:
            print(f"❌ [METRICS-DAEMON] Error: {e}")

        
        time.sleep(5)

_daemon_started = False

def start_daemon():
    global _daemon_started
    if _daemon_started:
        return None
    _daemon_started = True
    thread = threading.Thread(target=run_metrics_daemon, daemon=True)
    thread.start()
    return thread
