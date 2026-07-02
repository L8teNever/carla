# ==============================================================
# CARLA – WebSocket PTY Terminal
#
# Critical constraint: flask-sock (simple_websocket) requires
# ALL ws.send() calls to happen on the SAME THREAD that owns
# the handler. We therefore use a single-thread design:
#
#   Main handler thread:
#     - spawns PTY process
#     - spawns ws_reader_thread  (pushes to inp_q)
#     - spawns pty_reader_thread (pushes to out_q)
#     - loops: drain out_q → ws.send(), drain inp_q → pty.write()
#
# This guarantees ws.send() is always called from the handler thread.
# ==============================================================

import sys, os, json, threading, time, queue as _queue

IS_WINDOWS = sys.platform == "win32"


def init(sock):
    @sock.route("/ws/terminal")
    def ws_terminal(ws):
        from flask import request
        container_name = request.args.get("container")
        cols = request.args.get("cols", 100, type=int)
        rows = request.args.get("rows", 30, type=int)
        
        if IS_WINDOWS:
            _run_winpty(ws, cols, rows, container_name)
        else:
            _run_unix_pty(ws, cols, rows, container_name)


# ─────────────────────────────────────────────────────────────
# Windows  (pywinpty)
# ─────────────────────────────────────────────────────────────
def _run_winpty(ws, cols, rows, container_name=None):
    try:
        import winpty
    except ImportError:
        ws.send("[CARLA] pywinpty fehlt\r\n"); return

    import shutil
    proc = None
    cwd = None
    
    if container_name:
        docker_path = shutil.which("docker")
        if docker_path:
            import subprocess
            has_bash = False
            try:
                res = subprocess.run([docker_path, "exec", container_name, "which", "bash"], 
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
                if res.returncode == 0:
                    has_bash = True
            except Exception:
                pass
            shell_cmd = f"exec -it -u root -w / {container_name} {'bash' if has_bash else 'sh'}"
            try:
                proc = winpty.PtyProcess.spawn(docker_path, cmdline=shell_cmd, cwd=cwd, dimensions=(rows, cols))
            except Exception as e:
                ws.send(f"[CARLA] Docker winpty Fehler: {e}\r\n")
                return
        else:
            ws.send("\r\n\x1b[33m[CARLA] Docker CLI nicht gefunden. Fallback zu Powershell...\x1b[0m\r\n")
            shell = os.environ.get("COMSPEC", "powershell.exe")
            cwd = "C:\\"
            proc = winpty.PtyProcess.spawn(shell, cwd=cwd, dimensions=(rows, cols))
    else:
        shell = os.environ.get("COMSPEC", "powershell.exe")
        cwd = "C:\\"
        proc = winpty.PtyProcess.spawn(shell, cwd=cwd, dimensions=(rows, cols))

    stop  = threading.Event()
    inp_q = _queue.Queue()   # ws messages  → main thread → PTY
    out_q = _queue.Queue()   # PTY output   → main thread → ws

    # Thread A: ws.receive() – must NOT call ws.send()
    def ws_reader():
        try:
            while not stop.is_set():
                try:
                    msg = ws.receive()
                except Exception:
                    break
                if msg is None:
                    break
                inp_q.put(msg)
        finally:
            stop.set()

    # Thread B: read PTY output, push to out_q
    def pty_reader():
        try:
            while not stop.is_set():
                try:
                    data = proc.read(4096)
                except EOFError:
                    break
                except Exception:
                    time.sleep(0.01); continue
                if data:
                    out_q.put(data)
                else:
                    time.sleep(0.005)
        finally:
            stop.set()

    threading.Thread(target=ws_reader,  daemon=True).start()
    threading.Thread(target=pty_reader, daemon=True).start()

    # Main thread: forward out_q → ws.send()  AND  inp_q → PTY
    try:
        while not stop.is_set():
            # ── send pending PTY output ─────────────────────
            sent = 0
            while True:
                try:
                    chunk = out_q.get_nowait()
                    ws.send(chunk)
                    sent += 1
                except _queue.Empty:
                    break
                except Exception:
                    stop.set(); break

            # ── forward keyboard input to PTY ───────────────
            while True:
                try:
                    msg = inp_q.get_nowait()
                    try:
                        obj = json.loads(msg)
                        if obj.get("type") == "resize":
                            proc.setwinsize(int(obj["rows"]), int(obj["cols"]))
                        elif obj.get("type") == "input":
                            proc.write(obj.get("data", ""))
                    except (json.JSONDecodeError, TypeError):
                        if isinstance(msg, str):
                            proc.write(msg)
                except _queue.Empty:
                    break
                except Exception:
                    break

            # ── tiny sleep to avoid busy loop ────────────────
            if sent == 0:
                time.sleep(0.008)
    finally:
        stop.set()
        try: proc.terminate()
        except Exception: pass


# ─────────────────────────────────────────────────────────────
# Linux / macOS  (built-in pty)
# ─────────────────────────────────────────────────────────────
def _run_unix_pty(ws, cols, rows, container_name=None):
    import pty, select, struct, fcntl, termios, subprocess

    if container_name:
        # Check if bash exists in the container
        has_bash = False
        try:
            res = subprocess.run(["docker", "exec", container_name, "which", "bash"], 
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
            if res.returncode == 0:
                has_bash = True
        except Exception:
            pass
        shell_args = ["docker", "exec", "-it", "-u", "root", "-w", "/", container_name, "bash" if has_bash else "sh"]
        cwd = None
    else:
        shell = os.environ.get("SHELL", "/bin/bash")
        shell_args = [shell]
        cwd = "/"

    master_fd, slave_fd = pty.openpty()

    def _winsize(fd, r, c):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", r, c, 0, 0))
        except Exception: pass

    _winsize(master_fd, rows, cols)
    proc = subprocess.Popen(
        shell_args, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        close_fds=True, env={**os.environ, "TERM": "xterm-256color"},
        cwd=cwd,
    )
    os.close(slave_fd)

    stop  = threading.Event()
    inp_q = _queue.Queue()
    out_q = _queue.Queue()

    def ws_reader():
        try:
            while not stop.is_set():
                try:
                    msg = ws.receive()
                except Exception:
                    break
                if msg is None:
                    break
                inp_q.put(msg)
        finally:
            stop.set()

    def pty_reader():
        try:
            while not stop.is_set() and proc.poll() is None:
                r, _, _ = select.select([master_fd], [], [], 0.05)
                if r:
                    try:
                        data = os.read(master_fd, 4096)
                        if data:
                            out_q.put(data.decode("utf-8", errors="replace"))
                    except OSError:
                        break
        finally:
            stop.set()

    threading.Thread(target=ws_reader,  daemon=True).start()
    threading.Thread(target=pty_reader, daemon=True).start()

    try:
        while not stop.is_set() and proc.poll() is None:
            sent = 0
            while True:
                try:
                    chunk = out_q.get_nowait()
                    ws.send(chunk)
                    sent += 1
                except _queue.Empty:
                    break
                except Exception:
                    stop.set(); break

            while True:
                try:
                    msg = inp_q.get_nowait()
                    try:
                        obj = json.loads(msg)
                        if obj.get("type") == "resize":
                            _winsize(master_fd, int(obj["rows"]), int(obj["cols"]))
                        elif obj.get("type") == "input":
                            os.write(master_fd, obj.get("data", "").encode())
                    except (json.JSONDecodeError, TypeError):
                        if isinstance(msg, str):
                            os.write(master_fd, msg.encode())
                except _queue.Empty:
                    break
                except Exception:
                    break

            if sent == 0:
                time.sleep(0.008)
    finally:
        stop.set()
        try: proc.terminate()
        except Exception: pass
        try: os.close(master_fd)
        except Exception: pass
