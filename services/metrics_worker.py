import time
import threading
import paramiko
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
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(config.SSH_HOST, username=config.SSH_USER, password=config.SSH_PASS, timeout=10)
            
            # 1. System Overall CPU/RAM Metrics
            cmd_cpu = "top -bn1 | grep 'Cpu(s)' | sed 's/.*, *\\([0-9.]*\\)%* id.*/\\1/' | awk '{print 100 - $1}'"
            _, stdout, _ = ssh.exec_command(cmd_cpu)
            cpu_total_str = stdout.read().decode('utf-8').strip()
            server_cpu = float(cpu_total_str) if cpu_total_str else 0.0
            
            _, stdout, _ = ssh.exec_command("free -m | grep Mem | awk '{print $3, $2}'")
            ram_str = stdout.read().decode('utf-8').strip()
            if ram_str:
                used, total = ram_str.split()
                server_ram_used, server_ram_total = float(used), float(total)
            else:
                server_ram_used, server_ram_total = 0, 1000
                
            _, stdout, _ = ssh.exec_command("df -m / | awk 'NR==2{print $3, $2}'")
            disk_str = stdout.read().decode('utf-8').strip()
            if disk_str:
                d_parts = disk_str.split()
                server_disk_used = float(d_parts[0]) if len(d_parts) > 0 else 0.0
                server_disk_total = float(d_parts[1]) if len(d_parts) > 1 else 1000.0
            else:
                server_disk_used, server_disk_total = 0, 1000
                
            _, stdout, _ = ssh.exec_command("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo '0'")
            temp_str = stdout.read().decode('utf-8').strip()
            server_temp = float(temp_str) / 1000.0 if temp_str.isdigit() else 0.0
            
            # 2. Docker Names -> Stacks & Uptime Mapping
            # format: Names | Stack | Uptime
            cmd_ps = "docker ps --format '{{.Names}}|{{.Label \"com.docker.compose.project\"}}|{{.Status}}'"
            _, stdout, _ = ssh.exec_command(cmd_ps)
            ps_out = stdout.read().decode('utf-8').splitlines()
            c_meta = {}
            for line in ps_out:
                parts = line.split("|")
                if len(parts) >= 1:
                    name = parts[0]
                    stack = parts[1] if len(parts) > 1 and parts[1] else ""
                    # Uptime parsed from status like "Up 12 days" or "Up 3 seconds"
                    raw_uptime = parts[2] if len(parts) > 2 else "Unknown"
                    uptime = raw_uptime.replace("Up ", "").strip()
                    
                    if not stack:
                        if "-" in name: stack = name.split("-")[0]
                        elif "_" in name: stack = name.split("_")[0]
                    c_meta[name] = {"stack": stack or "Einzelne", "uptime": uptime}

            # 3. Docker Stats
            cmd_stats = "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}'"
            _, stdout, _ = ssh.exec_command(cmd_stats)
            stats_out = stdout.read().decode('utf-8').splitlines()
            
            stack_totals = {}
            container_stats = []
            
            for line in stats_out:
                parts = line.split("|")
                if len(parts) >= 3:
                    c_name = parts[0]
                    meta = c_meta.get(c_name, {"stack": "Einzelne", "uptime": "N/A"})
                    stack = meta["stack"]
                    uptime = meta["uptime"]
                    
                    cpu_perc = float(parts[1].replace("%", "").strip()) if parts[1] else 0.0
                    mem_raw = parts[2]
                    mem_used_part = mem_raw.split("/")[0].strip() if "/" in mem_raw else mem_raw.strip()
                    ram_mb = parse_docker_memory(mem_used_part)
                    
                    if stack not in stack_totals:
                        stack_totals[stack] = {"cpu": 0.0, "ram": 0.0}
                    stack_totals[stack]["cpu"] += cpu_perc
                    stack_totals[stack]["ram"] += ram_mb
                    
                    container_stats.append({
                        "name": c_name,
                        "stack": stack,
                        "cpu": cpu_perc,
                        "ram": ram_mb,
                        "uptime": uptime
                    })
            
            stack_data = [{"stack": k, "cpu": v["cpu"], "ram": v["ram"]} for k, v in stack_totals.items()]
            
            # Log all to DB
            metrics_db.log_metrics(server_cpu, server_ram_used, server_ram_total, server_disk_used, server_disk_total, server_temp, stack_data, container_stats)
            
            ssh.close()
        except Exception as e:
            print(f"❌ [METRICS-DAEMON] Error: {e}")
        
        time.sleep(5)

def start_daemon():
    thread = threading.Thread(target=run_metrics_daemon, daemon=True)
    thread.start()
    return thread
