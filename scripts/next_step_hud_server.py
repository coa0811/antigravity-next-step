#!/usr/bin/env python3
"""Read-only local HTTP + SSE HUD server (127.0.0.1) for the Antigravity Next Step plugin."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import next_step_core as core  # noqa: E402

DEFAULT_HUD_PORT = 18765
HUD_SERVER_VERSION = "0.3.0"


def _compute_sessions_signature() -> tuple[Any, ...]:
    """Build a lightweight signature of all session files and their linked plan artifacts."""
    core.ensure_data_dirs()
    sig: list[tuple[str, int, str | None, int | None]] = []
    for p in sorted(core.SESSIONS_DIR.glob("*.json")):
        if p.name.startswith("."):
            continue
        try:
            st = p.stat()
            raw = json.loads(p.read_text(encoding="utf-8"))
            plan_path = None
            plan_mtime = None
            if isinstance(raw, dict) and isinstance(raw.get("planArtifact"), dict):
                plan_path = raw["planArtifact"].get("path")
                if plan_path and Path(str(plan_path)).is_file():
                    plan_mtime = Path(str(plan_path)).stat().st_mtime_ns
            sig.append((p.name, st.st_mtime_ns, str(plan_path) if plan_path else None, plan_mtime))
        except Exception:
            continue
    return tuple(sig)


class NextStepHUDServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[BaseHTTPRequestHandler]):
        super().__init__(server_address, RequestHandlerClass)
        self.stop_event = threading.Event()

    def service_actions(self) -> None:
        if self.stop_event.is_set():
            raise KeyboardInterrupt


class NextStepHUDHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy access logs in stdio/daemon mode
        return

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html_text: str) -> None:
        body = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        ws_filter = query.get("workspace", [None])[0]

        if route == "/healthz":
            self._send_json(
                200,
                {
                    "ok": True,
                    "version": HUD_SERVER_VERSION,
                    "pid": os.getpid(),
                    "dataDir": str(core.DATA_DIR),
                    "serverTime": time.time(),
                },
            )
            return

        if route == "/api/sessions":
            sessions = core.list_all_sessions(workspace_filter=ws_filter)
            self._send_json(
                200,
                {
                    "ok": True,
                    "sessions": sessions,
                    "serverTime": time.time(),
                },
            )
            return

        if route in ("/", "/ui.html", "/index.html"):
            compact = query.get("mode", [""])[0].strip().lower() == "compact"
            sessions = core.list_all_sessions(workspace_filter=ws_filter)
            html_page = core.render_hud_html(initial_sessions=sessions, live_sse=True, compact=compact)
            self._send_html(200, html_page)
            return

        if route == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            stop_ev = getattr(self.server, "stop_event", None)
            last_sig: tuple[Any, ...] | None = None
            last_emit_ts = 0.0

            try:
                while stop_ev is None or not stop_ev.is_set():
                    now = time.time()
                    cur_sig = _compute_sessions_signature()
                    # Re-emit immediately when files change or every 2.5s to update live elapsed timers/staleness
                    if cur_sig != last_sig or (now - last_emit_ts) >= 2.5:
                        sessions = core.list_all_sessions(workspace_filter=ws_filter, now=now)
                        payload = json.dumps(
                            {"sessions": sessions, "serverTime": now},
                            ensure_ascii=False,
                        )
                        chunk = f"data: {payload}\n\n".encode("utf-8")
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        last_sig = cur_sig
                        last_emit_ts = now
                    if stop_ev is not None:
                        if stop_ev.wait(0.25):
                            break
                    else:
                        time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            return

        self._send_json(404, {"ok": False, "error": f"Not found: {route}"})


def start_in_process_server(
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[NextStepHUDServer, threading.Thread, str]:
    """Start the HUD server on a background thread (ideal for tests and embedded use)."""
    core.ensure_data_dirs()
    httpd = NextStepHUDServer((host, port), NextStepHUDHandler)
    actual_port = int(httpd.server_address[1])
    url = f"http://{host}:{actual_port}/"
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    return httpd, thread, url


def stop_in_process_server(httpd: NextStepHUDServer, thread: threading.Thread | None = None) -> None:
    """Cleanly stop an in-process HUD server."""
    httpd.stop_event.set()
    httpd.shutdown()
    httpd.server_close()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)


def _probe_healthz(url: str, timeout: float = 0.35) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url.rstrip("/") + "/healthz", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(data, dict) and data.get("ok") is True:
                    return data
    except Exception:
        pass
    return None


def _bind_server(host: str, preferred_port: int) -> NextStepHUDServer:
    for candidate_port in (preferred_port, 0):
        try:
            return NextStepHUDServer((host, candidate_port), NextStepHUDHandler)
        except OSError:
            continue
    return NextStepHUDServer((host, 0), NextStepHUDHandler)


def _verify_hud_server_process(pid: int) -> bool:
    """Verify PID's executable is Python and its positional script argument is this exact next_step_hud_server.py."""
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        comm_proc = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "comm="],
            text=True,
            capture_output=True,
            timeout=1.0,
        )
        args_proc = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "args="],
            text=True,
            capture_output=True,
            timeout=1.0,
        )
        if comm_proc.returncode != 0 or args_proc.returncode != 0:
            return False
        comm_exe = (comm_proc.stdout or "").strip()
        cmdline = (args_proc.stdout or "").strip()
        if not comm_exe or not cmdline:
            return False
        if "python" not in Path(comm_exe).name.lower():
            return False

        if cmdline.startswith(comm_exe + " "):
            rest = shlex.split(cmdline[len(comm_exe) :])
        else:
            parsed = shlex.split(cmdline)
            rest = parsed[1:] if len(parsed) > 1 else []

        script_arg: str | None = None
        idx = 0
        while idx < len(rest):
            tok = rest[idx]
            if tok in ("-c", "-m"):
                return False
            if tok in ("-W", "-X", "--check-hash-based-pycs"):
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            script_arg = tok
            break

        if not script_arg:
            return False
        if Path(script_arg).resolve() != Path(__file__).resolve():
            return False

        tail_args = rest[idx + 1 :]
        if "--data-dir" in tail_args:
            dd_pos = tail_args.index("--data-dir")
            if dd_pos + 1 < len(tail_args):
                if Path(tail_args[dd_pos + 1]).resolve() != core.DATA_DIR.resolve():
                    return False
        return True
    except Exception:
        return False


def _terminate_stale_server(pid: int) -> None:
    if not _verify_hud_server_process(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return
    deadline = time.time() + 0.5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.05)
        except OSError:
            break


def ensure_hud_server_running(preferred_port: int = DEFAULT_HUD_PORT) -> dict[str, Any]:
    """Ensure a local read-only HUD server is running on 127.0.0.1 and return its metadata."""
    core.ensure_data_dirs()
    lock_path = core.DATA_DIR / "hud_server.lock"
    with lock_path.open("w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            return _ensure_hud_server_locked(preferred_port=preferred_port)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _ensure_hud_server_locked(preferred_port: int = DEFAULT_HUD_PORT) -> dict[str, Any]:

    if core.HUD_SERVER_JSON_PATH.is_file():
        try:
            info = json.loads(core.HUD_SERVER_JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(info, dict) and info.get("url"):
                health = _probe_healthz(str(info["url"]))
                if health is not None and str(health.get("dataDir") or "") == str(core.DATA_DIR):
                    if health.get("version") == HUD_SERVER_VERSION:
                        return {
                            "ok": True,
                            "version": HUD_SERVER_VERSION,
                            "url": str(info["url"]),
                            "port": int(info.get("port") or 0),
                            "pid": int(health.get("pid") or info.get("pid") or 0),
                            "snapshotHtmlPath": str(core.SNAPSHOT_HTML_PATH),
                        }
                    _terminate_stale_server(int(health.get("pid") or info.get("pid") or 0))
        except Exception:
            pass

    # Also check preferred_port directly in case hud_server.json was missing
    direct_url = f"http://127.0.0.1:{preferred_port}/"
    health = _probe_healthz(direct_url)
    if health is not None and str(health.get("dataDir") or "") == str(core.DATA_DIR):
        if health.get("version") == HUD_SERVER_VERSION:
            meta = {
                "ok": True,
                "version": HUD_SERVER_VERSION,
                "url": direct_url,
                "port": preferred_port,
                "pid": int(health.get("pid") or 0),
                "dataDir": str(core.DATA_DIR),
                "snapshotHtmlPath": str(core.SNAPSHOT_HTML_PATH),
            }
            core.atomic_write_json(core.HUD_SERVER_JSON_PATH, meta)
            return meta
        _terminate_stale_server(int(health.get("pid") or 0))

    env = os.environ.copy()
    env["PLUGIN_DATA"] = str(core.DATA_DIR)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--host",
        "127.0.0.1",
        "--port",
        str(preferred_port),
        "--data-dir",
        str(core.DATA_DIR),
    ]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception as exc:
        core.append_error_log("spawn_hud_server", exc)
        return {
            "ok": False,
            "url": f"file://{core.SNAPSHOT_HTML_PATH}",
            "port": 0,
            "pid": 0,
            "snapshotHtmlPath": str(core.SNAPSHOT_HTML_PATH),
            "error": str(exc),
        }

    deadline = time.time() + 1.8
    while time.time() < deadline:
        if core.HUD_SERVER_JSON_PATH.is_file():
            try:
                info = json.loads(core.HUD_SERVER_JSON_PATH.read_text(encoding="utf-8"))
                if isinstance(info, dict) and info.get("url"):
                    health = _probe_healthz(str(info["url"]), timeout=0.25)
                    if health is not None:
                        info["ok"] = True
                        info["snapshotHtmlPath"] = str(core.SNAPSHOT_HTML_PATH)
                        return info
            except Exception:
                pass
        time.sleep(0.08)

    return {
        "ok": False,
        "url": f"file://{core.SNAPSHOT_HTML_PATH}",
        "port": 0,
        "pid": 0,
        "snapshotHtmlPath": str(core.SNAPSHOT_HTML_PATH),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Next Step local read-only SSE HUD server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_HUD_PORT, help="Preferred bind port")
    parser.add_argument("--data-dir", default="", help="Optional PLUGIN_DATA override directory")
    args = parser.parse_args()

    if args.data_dir:
        core.set_data_dir(Path(args.data_dir))
    core.ensure_data_dirs()

    httpd = _bind_server(args.host, args.port)
    actual_port = int(httpd.server_address[1])
    url = f"http://{args.host}:{actual_port}/"
    meta = {
        "version": HUD_SERVER_VERSION,
        "url": url,
        "host": args.host,
        "port": actual_port,
        "pid": os.getpid(),
        "dataDir": str(core.DATA_DIR),
        "startedAt": time.time(),
        "startedAtIso": core.iso_now(),
    }
    core.atomic_write_json(core.HUD_SERVER_JSON_PATH, meta)

    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
