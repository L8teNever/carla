# ==============================================================
# CARLA – System Executor
# Abstraktionsschicht für SSH vs. Lokale Befehlsführung.
# ==============================================================
import subprocess
import paramiko
import config

_ssh_client = None

def get_ssh_client():
    global _ssh_client
    if _ssh_client is None:
        try:
            _ssh_client = paramiko.SSHClient()
            _ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            _ssh_client.connect(config.SSH_HOST, username=config.SSH_USER, password=config.SSH_PASS, timeout=10)
        except Exception as e:
            _ssh_client = None
            raise e
    return _ssh_client

def close_ssh():
    global _ssh_client
    if _ssh_client:
        _ssh_client.close()
        _ssh_client = None

def execute_command(cmd: str, timeout: int = 15) -> str:
    """Führt einen Shell-Befehl entweder lokal oder via SSH aus."""
    if config.MODE == "local":
        try:
            # Shell=True ist hier notwendig für Pipes/Awk/Grep im Befehl
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                return result.stderr.strip()
        except Exception as e:
            return f"Error: {e}"
    else:
        # SSH-Modus
        try:
            client = get_ssh_client()
            _, stdout, stderr = client.exec_command(cmd)
            out = stdout.read().decode("utf-8").strip()
            err = stderr.read().decode("utf-8").strip()
            return out if out else err
        except Exception as e:
            # Versuche Reconnect bei Fehlern
            close_ssh()
            return f"SSH-Error: {e}"

def get_host_ip():
    """Gibt die IP der Zielmaschine zurück."""
    if config.MODE == "local":
        return "localhost"
    return config.SSH_HOST
