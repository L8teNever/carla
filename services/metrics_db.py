import sqlite3
import os
import time

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "storage", "metrics.db")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Server Metrics: cpu_usage in %, ram_usage in MB, ram_total in MB
    c.execute('''
        CREATE TABLE IF NOT EXISTS server_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            cpu_usage REAL,
            ram_usage REAL,
            ram_total REAL,
            disk_used REAL,
            disk_total REAL,
            cpu_temp REAL
        )
    ''')
    
    # Stack Metrics: cpu_usage in %, ram_usage in MB
    c.execute('''
        CREATE TABLE IF NOT EXISTS stack_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            stack_name TEXT,
            cpu_usage REAL,
            ram_usage REAL
        )
    ''')

    # Container Metrics: Jedes einzelne Docker-Subjekt
    c.execute('''
        CREATE TABLE IF NOT EXISTS container_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            container_name TEXT,
            stack_name TEXT,
            cpu_usage REAL,
            ram_usage REAL,
            uptime TEXT
        )
    ''')

    # Network I/O Spalten nachrüsten (ignoriert Fehler wenn schon vorhanden)
    try:
        c.execute("ALTER TABLE container_metrics ADD COLUMN net_rx REAL DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE container_metrics ADD COLUMN net_tx REAL DEFAULT 0")
    except Exception:
        pass
    
    conn.commit()
    conn.close()

def log_metrics(server_cpu, server_ram_used, server_ram_total, disk_used, disk_total, cpu_temp, stack_data, container_stats):
    """
    stack_data: list of dicts [{'stack': 'carlin', 'cpu': 12.5, 'ram': 512.0}, ...]
    container_stats: list of dicts [{'name': 'grafana', 'stack': 'monitoring', 'cpu': 1.2, 'ram': 256.0, 'uptime': '12d 4h'}]
    """
    init_db()
    ts = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO server_metrics (timestamp, cpu_usage, ram_usage, ram_total, disk_used, disk_total, cpu_temp) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (ts, server_cpu, server_ram_used, server_ram_total, disk_used, disk_total, cpu_temp))
    for s in stack_data:
        c.execute("INSERT INTO stack_metrics (timestamp, stack_name, cpu_usage, ram_usage) VALUES (?, ?, ?, ?)",
                  (ts, s['stack'], s['cpu'], s['ram']))
                  
    for cs in container_stats:
        c.execute("INSERT INTO container_metrics (timestamp, container_name, stack_name, cpu_usage, ram_usage, uptime, net_rx, net_tx) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (ts, cs['name'], cs['stack'], cs['cpu'], cs['ram'], cs['uptime'], cs.get('net_rx', 0), cs.get('net_tx', 0)))
    conn.commit()
    conn.close()

def get_server_metrics_history(limit=100):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM server_metrics ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]

def get_latest_stack_metrics():
    """Gibt die Stack-Metriken des neuesten Zeitstempels zurück für ein Pie-Chart"""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("SELECT MAX(timestamp) as ts FROM stack_metrics")
    row = c.fetchone()
    if not row or not row['ts']:
        return []
        
    latest_ts = row['ts']
    c.execute("SELECT stack_name, cpu_usage, ram_usage FROM stack_metrics WHERE timestamp = ?", (latest_ts,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_latest_container_metrics():
    """Gibt die aktuellsten Metriken für alle Container zurück."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT MAX(timestamp) as ts FROM container_metrics")
    row = c.fetchone()
    if not row or not row['ts']: return []
    latest_ts = row['ts']
    c.execute("SELECT * FROM container_metrics WHERE timestamp = ?", (latest_ts,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_container_net_activity():
    """Gibt die Netzwerk-Delta-Werte pro Container zurueck (Differenz letzte zwei Messungen)."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Letzte 2 Timestamps holen
    c.execute("SELECT DISTINCT timestamp FROM container_metrics ORDER BY timestamp DESC LIMIT 2")
    ts_rows = c.fetchall()
    if len(ts_rows) < 2:
        conn.close()
        return []

    ts_new, ts_old = ts_rows[0]['timestamp'], ts_rows[1]['timestamp']
    dt = max(ts_new - ts_old, 1)

    c.execute("SELECT container_name, stack_name, cpu_usage, ram_usage, net_rx, net_tx FROM container_metrics WHERE timestamp = ?", (ts_new,))
    new_rows = {r['container_name']: dict(r) for r in c.fetchall()}

    c.execute("SELECT container_name, net_rx, net_tx FROM container_metrics WHERE timestamp = ?", (ts_old,))
    old_rows = {r['container_name']: dict(r) for r in c.fetchall()}

    conn.close()

    result = []
    for name, data in new_rows.items():
        old = old_rows.get(name, {})
        rx_delta = max(0, (data.get('net_rx', 0) or 0) - (old.get('net_rx', 0) or 0))
        tx_delta = max(0, (data.get('net_tx', 0) or 0) - (old.get('net_tx', 0) or 0))
        result.append({
            "name": name,
            "stack": data.get('stack_name', ''),
            "cpu": data.get('cpu_usage', 0),
            "ram": data.get('ram_usage', 0),
            "net_rx": data.get('net_rx', 0) or 0,
            "net_tx": data.get('net_tx', 0) or 0,
            "rx_rate": round(rx_delta / dt, 2),
            "tx_rate": round(tx_delta / dt, 2),
            "activity": rx_delta + tx_delta,
        })
    return result


def get_stack_history(stack_name, limit=60):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM stack_metrics WHERE stack_name = ? ORDER BY timestamp DESC LIMIT ?", (stack_name, limit))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def get_timeline_snapshots(limit=100):
    """Gibt eine Liste aller verfügbaren Zeitstempel zurück."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT timestamp, cpu_usage FROM server_metrics ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_full_snapshot(timestamp):
    """Gibt den kompletten Zustand des Systems zu einem bestimmten Zeitpunkt zurück."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. Server Metrics
    c.execute("SELECT * FROM server_metrics WHERE timestamp = ?", (timestamp,))
    server = c.fetchone()
    
    # 2. Stack Metrics
    c.execute("SELECT * FROM stack_metrics WHERE timestamp = ?", (timestamp,))
    stacks = c.fetchall()
    
    # 3. Container Metrics
    c.execute("SELECT * FROM container_metrics WHERE timestamp = ?", (timestamp,))
    containers = c.fetchall()
    
    conn.close()
    return {
        "timestamp": timestamp,
        "server": dict(server) if server else None,
        "stacks": [dict(s) for s in stacks],
        "containers": [dict(co) for co in containers]
    }
