# ==============================================================
# CARLA – System Executor
# Führt Shell-Befehle lokal aus.
# ==============================================================
import subprocess

def execute_command(cmd: str, timeout: int = 15) -> str:
    """Führt einen Shell-Befehl lokal aus."""
    try:
        # Shell=True ist hier notwendig für Pipes/Awk/Grep im Befehl
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return result.stderr.strip()
    except Exception as e:
        return f"Error: {e}"

def get_host_ip():
    """Gibt die IP der Zielmaschine zurück (lokal immer localhost)."""
    return "localhost"

