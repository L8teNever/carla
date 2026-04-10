# ==============================================================
# CARLA – Auto-Discovery Daemon
#
# Überwacht im Hintergrund alle 30 Sekunden:
#   • Docker-Container (neue / gestoppte / entfernte)
#   • Offene Host-Ports (ss / netstat)
#
# Bei Änderungen wird automatisch ein Hintergrund-Fetch
# ausgelöst, ohne dass Clara neu gestartet werden muss.
# ==============================================================

import threading
import hashlib
import time
import logging

from . import system_executor

logger = logging.getLogger("carla.discovery")

# Callback wird von main.py / routes.py gesetzt
_on_change_callback = None

# Interner Zustand
_last_fingerprint: str | None = None
_thread: threading.Thread | None = None
_running = False

# Wie oft checken (Sekunden)
CHECK_INTERVAL = 30


# ---------------------------------------------------------------
# Fingerprint-Berechnung
# ---------------------------------------------------------------

def _get_container_fingerprint() -> str:
    """
    Liefert ein kompaktes Hash-Abbild aller Container.
    Berücksichtigt: Name, Image, Status, Ports.
    Änderungen (neuer Container, State-Wechsel, neue Ports) → anderer Hash.
    """
    cmd = (
        "docker ps -a --no-trunc "
        "--format '{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Ports}}'"
    )
    out = system_executor.execute_command(cmd)
    if not out or out.startswith("Error") or out.startswith("SSH-Error"):
        # Bei Fehler bleiben wir beim alten Fingerprint → kein Trigger
        return _last_fingerprint or ""

    # Sortieren damit Reihenfolge keine Rolle spielt
    lines = sorted(out.strip().splitlines())
    raw = "\n".join(lines)
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_port_fingerprint() -> str:
    """
    Liefert einen Hash aller LISTEN-Ports auf dem Host.
    Funktioniert mit ss (bevorzugt) oder netstat als Fallback.
    """
    # ss ist auf modernen Linuxen default; netstat als Fallback
    for cmd in [
        "ss -tlnH 2>/dev/null | awk '{print $4}' | sort -u",
        "netstat -tlnH 2>/dev/null | awk '{print $4}' | sort -u",
    ]:
        out = system_executor.execute_command(cmd)
        if out and not out.startswith("Error") and not out.startswith("SSH-Error"):
            return hashlib.sha256(out.strip().encode()).hexdigest()
    return ""


def _compute_fingerprint() -> str:
    container_fp = _get_container_fingerprint()
    port_fp      = _get_port_fingerprint()
    combined     = container_fp + "|" + port_fp
    return hashlib.sha256(combined.encode()).hexdigest()


# ---------------------------------------------------------------
# Daemon-Loop
# ---------------------------------------------------------------

def _discovery_loop():
    global _last_fingerprint, _running

    logger.info("[Discovery] Auto-Discovery Daemon gestartet (Interval: %ds)", CHECK_INTERVAL)

    while _running:
        try:
            current = _compute_fingerprint()

            if _last_fingerprint is None:
                # Erster Lauf – Baseline setzen, kein Trigger
                _last_fingerprint = current
                logger.debug("[Discovery] Baseline gesetzt: %s…", current[:12])

            elif current != _last_fingerprint:
                logger.info(
                    "[Discovery] ⚡ Änderung erkannt! "
                    "Alt=%s… Neu=%s… → Refresh wird ausgelöst.",
                    _last_fingerprint[:12], current[:12]
                )
                _last_fingerprint = current

                if _on_change_callback:
                    try:
                        _on_change_callback()
                    except Exception as cb_err:
                        logger.warning("[Discovery] Callback-Fehler: %s", cb_err)
            else:
                logger.debug("[Discovery] Keine Änderung erkannt.")

        except Exception as loop_err:
            logger.warning("[Discovery] Fehler im Loop (nicht fatal): %s", loop_err)

        # In kleinen Schritten schlafen, damit _running-Flag schnell reagiert
        for _ in range(CHECK_INTERVAL * 2):
            if not _running:
                break
            time.sleep(0.5)

    logger.info("[Discovery] Daemon gestoppt.")


# ---------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------

def set_change_callback(fn) -> None:
    """
    Registriert eine Callback-Funktion, die aufgerufen wird,
    wenn eine Änderung erkannt wurde.

    Beispiel:
        from services import discovery
        from routes import start_background_fetch
        discovery.set_change_callback(start_background_fetch)
    """
    global _on_change_callback
    _on_change_callback = fn


def start_daemon() -> None:
    """Startet den Auto-Discovery-Daemon als Hintergrund-Thread."""
    global _thread, _running

    if _thread and _thread.is_alive():
        logger.debug("[Discovery] Daemon läuft bereits.")
        return

    _running = True
    _thread = threading.Thread(target=_discovery_loop, name="carla-discovery", daemon=True)
    _thread.start()


def stop_daemon() -> None:
    """Stoppt den Daemon (optional, da daemon=True)."""
    global _running
    _running = False


def force_reset_baseline() -> None:
    """
    Erzwingt eine neue Baseline-Berechnung beim nächsten Tick.
    Nützlich nach einem manuellen Refresh, damit der nächste
    automatische Check korrekt verglichen wird.
    """
    global _last_fingerprint
    _last_fingerprint = None
