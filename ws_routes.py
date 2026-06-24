# ==============================================================
# CARLA – WebSocket PTY Terminal
# Provides a real pseudo-terminal over WebSocket using:
#   - pywinpty  on Windows
#   - pty module on Linux/macOS
# Protocol (JSON messages):
#   Client → Server:  {"type": "input",  "data": "<chars>"}
#                     {"type": "resize", "cols": N, "rows": N}
#   Server → Client:  raw bytes / text (terminal output)
# ==============================================================

import sys
import os
import json
import threading
import time
import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------
IS_WINDOWS = sys.platform == "win32"

def init(sock):
    """Register WebSocket routes on the given flask-sock Sock instance."""

    @sock.route("/ws/terminal")
    def ws_terminal(ws):
        """
        Full PTY session over WebSocket.
        Each connection spawns a fresh shell process bound to a PTY.
        """
        # Default terminal size
        cols, rows = 220, 50

        if IS_WINDOWS:
            _run_winpty_session(ws, cols, rows)
        else:
            _run_pty_session(ws, cols, rows)


# ---------------------------------------------------------------
# Windows: pywinpty
# ---------------------------------------------------------------
def _run_winpty_session(ws, cols, rows):
    try:
        import winpty
    except ImportError:
        ws.send("[CARLA] pywinpty nicht installiert. pip install pywinpty\r\n")
        return

    # Launch PowerShell (falls back to cmd)
    shell = os.environ.get("COMSPEC", "powershell.exe")
    try:
        proc = winpty.PtyProcess.spawn(shell, dimensions=(rows, cols))
    except Exception as e:
        ws.send(f"[CARLA] Shell konnte nicht gestartet werden: {e}\r\n")
        return

    stop_event = threading.Event()

    # Reader thread: PTY → WebSocket
    def reader():
        try:
            while not stop_event.is_set():
                try:
                    data = proc.read(4096)
                    if data:
                        ws.send(data)
                    else:
                        time.sleep(0.005)
                except EOFError:
                    break
                except Exception:
                    break
        finally:
            stop_event.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # Main loop: WebSocket → PTY
    try:
        while not stop_event.is_set():
            try:
                msg = ws.receive(timeout=1)
            except Exception:
                break
            if msg is None:
                break
            try:
                obj = json.loads(msg)
                if obj.get("type") == "resize":
                    c = int(obj.get("cols", cols))
                    r = int(obj.get("rows", rows))
                    proc.setwinsize(r, c)
                elif obj.get("type") == "input":
                    proc.write(obj.get("data", ""))
            except (json.JSONDecodeError, TypeError):
                # Raw text input fallback
                if isinstance(msg, str):
                    proc.write(msg)
    finally:
        stop_event.set()
        try:
            proc.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------
# Linux / macOS: built-in pty module
# ---------------------------------------------------------------
def _run_pty_session(ws, cols, rows):
    import pty
    import select
    import struct
    import fcntl
    import termios
    import subprocess

    shell = os.environ.get("SHELL", "/bin/bash")

    master_fd, slave_fd = pty.openpty()

    # Set initial terminal size
    def _set_winsize(fd, r, c):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", r, c, 0, 0))
        except Exception:
            pass

    _set_winsize(master_fd, rows, cols)

    proc = subprocess.Popen(
        [shell],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave_fd)

    stop_event = threading.Event()

    # Reader thread: PTY → WebSocket
    def reader():
        try:
            while not stop_event.is_set():
                r, _, _ = select.select([master_fd], [], [], 0.05)
                if r:
                    try:
                        data = os.read(master_fd, 4096)
                        if data:
                            ws.send(data.decode("utf-8", errors="replace"))
                    except OSError:
                        break
        finally:
            stop_event.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # Main loop: WebSocket → PTY
    try:
        while not stop_event.is_set() and proc.poll() is None:
            try:
                msg = ws.receive(timeout=1)
            except Exception:
                break
            if msg is None:
                break
            try:
                obj = json.loads(msg)
                if obj.get("type") == "resize":
                    c = int(obj.get("cols", cols))
                    r = int(obj.get("rows", rows))
                    _set_winsize(master_fd, r, c)
                elif obj.get("type") == "input":
                    os.write(master_fd, obj.get("data", "").encode())
            except (json.JSONDecodeError, TypeError):
                if isinstance(msg, str):
                    os.write(master_fd, msg.encode())
                elif isinstance(msg, bytes):
                    os.write(master_fd, msg)
    finally:
        stop_event.set()
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            os.close(master_fd)
        except Exception:
            pass
