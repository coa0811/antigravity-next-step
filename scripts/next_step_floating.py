#!/usr/bin/env python3
"""Compiler and single-instance launcher for the macOS native non-activating floating HUD."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import next_step_core as core  # noqa: E402
import next_step_hud_server as hud_server  # noqa: E402

SWIFT_SOURCE_PATH = SCRIPT_DIR / "floating_hud.swift"


def find_swiftc() -> tuple[str, dict[str, str], list[str]] | None:
    """Locate a working swiftc executable, environment, and SDK flags (supporting CommandLineTools)."""
    clt_dir = Path("/Library/Developer/CommandLineTools")
    clt_sdk = clt_dir / "SDKs" / "MacOSX.sdk"
    clt_swiftc = clt_dir / "usr" / "bin" / "swiftc"
    if clt_swiftc.is_file():
        env = os.environ.copy()
        env["DEVELOPER_DIR"] = str(clt_dir)
        extra = ["-sdk", str(clt_sdk)] if clt_sdk.is_dir() else []
        swift_bin = "/usr/bin/swiftc" if Path("/usr/bin/swiftc").is_file() else str(clt_swiftc)
        return swift_bin, env, extra

    sys_swiftc = shutil.which("swiftc")
    if sys_swiftc:
        return sys_swiftc, os.environ.copy(), []
    return None


def _floating_paths(data_dir: Path | None = None) -> tuple[Path, Path, Path, Path]:
    base = Path(data_dir).resolve() if data_dir else core.DATA_DIR
    bin_dir = base / "bin"
    binary_path = bin_dir / "next-step-floating-hud"
    hash_path = bin_dir / "next-step-floating-hud.sha256"
    state_path = base / "floating_hud.json"
    frame_path = base / "floating_hud_frame.json"
    return binary_path, hash_path, state_path, frame_path


def dismiss_marker_path(data_dir: Path | None = None) -> Path:
    base = Path(data_dir).resolve() if data_dir else core.DATA_DIR
    return base / "floating_hud_dismissed.json"


def is_floating_hud_dismissed(data_dir: Path | None = None) -> bool:
    return dismiss_marker_path(data_dir).is_file()


def clear_dismissed_marker(data_dir: Path | None = None) -> None:
    try:
        dismiss_marker_path(data_dir).unlink(missing_ok=True)
    except OSError:
        pass


def _auto_launch_disabled_by_env() -> bool:
    raw = str(os.environ.get("NEXT_STEP_NO_AUTO_FLOATING_HUD") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def compile_floating_hud(data_dir: Path | None = None) -> Path:
    """Compile floating_hud.swift into a cached Mach-O binary under <data_dir>/bin/."""
    if not SWIFT_SOURCE_PATH.is_file():
        raise FileNotFoundError(f"Missing Swift source: {SWIFT_SOURCE_PATH}")

    binary_path, hash_path, _, _ = _floating_paths(data_dir)
    binary_path.parent.mkdir(parents=True, exist_ok=True)
    src_sha = hashlib.sha256(SWIFT_SOURCE_PATH.read_bytes()).hexdigest()

    if binary_path.is_file() and os.access(binary_path, os.X_OK) and hash_path.is_file():
        cached_sha = hash_path.read_text(encoding="utf-8", errors="replace").strip()
        if cached_sha == src_sha:
            return binary_path

    found = find_swiftc()
    if found is None:
        raise RuntimeError("swiftc compiler not found on this machine.")
    swiftc_bin, env, extra_flags = found

    tmp_bin = binary_path.with_suffix(".tmp")
    cmd = [
        swiftc_bin,
        "-O",
        *extra_flags,
        str(SWIFT_SOURCE_PATH),
        "-framework",
        "Cocoa",
        "-framework",
        "WebKit",
        "-o",
        str(tmp_bin),
    ]
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if proc.returncode != 0:
        try:
            tmp_bin.unlink(missing_ok=True)
        except OSError:
            pass
        err_msg = (proc.stderr or proc.stdout or "swiftc compilation failed").strip()
        raise RuntimeError(f"Failed to compile floating HUD: {err_msg}")

    os.chmod(tmp_bin, 0o755)
    os.replace(tmp_bin, binary_path)
    hash_path.write_text(src_sha + "\n", encoding="utf-8")
    return binary_path


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _query_ps_field(pid: int, field: str) -> str | None:
    proc = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", f"{field}="],
        text=True,
        capture_output=True,
        timeout=1.0,
    )
    if proc.returncode != 0:
        return None
    val = (proc.stdout or "").strip()
    return val or None


def _verify_floating_hud_process(pid: int, expected_executable: str | None = None) -> bool:
    """Verify PID is alive and its actual executable & argv[0] match our next-step-floating-hud binary."""
    if not _is_pid_alive(pid):
        return False
    try:
        comm_exe = _query_ps_field(pid, "comm")
        cmdline = _query_ps_field(pid, "args")
        if not comm_exe or not cmdline:
            return False

        target_name = Path(expected_executable).name if expected_executable else "next-step-floating-hud"
        if Path(comm_exe).name != target_name:
            return False

        if expected_executable:
            expected_resolved = str(Path(expected_executable).resolve())
            if os.path.isabs(comm_exe):
                try:
                    if str(Path(comm_exe).resolve()) != expected_resolved:
                        return False
                except OSError:
                    return False
            if cmdline == expected_executable or cmdline.startswith(expected_executable + " "):
                return True
            try:
                argv = shlex.split(cmdline)
            except ValueError:
                return False
            return bool(argv and str(Path(argv[0]).resolve()) == expected_resolved)

        try:
            argv = shlex.split(cmdline)
        except ValueError:
            return False
        return bool(argv and Path(argv[0]).name == "next-step-floating-hud")
    except Exception:
        return False


def is_floating_hud_running(data_dir: Path | None = None) -> dict[str, Any] | None:
    """Return runtime metadata only if the floating HUD process is alive and passes identity verification."""
    binary_path, _, state_path, _ = _floating_paths(data_dir)
    if not state_path.is_file():
        return None
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        pid = int(raw.get("pid") or 0)
        if _verify_floating_hud_process(pid, expected_executable=str(binary_path)):
            return raw
    except Exception:
        pass
    try:
        state_path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def _build_hud_url(base_url: str, compact: bool) -> str:
    parsed = urlparse(base_url)
    query = "mode=compact" if compact else ""
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.params, query, ""))


def _wait_process_exit(pid: int, timeout: float = 0.8) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return
        time.sleep(0.05)


def launch_floating_hud(
    url: str | None = None,
    compact: bool = True,
    data_dir: Path | None = None,
    respect_dismiss: bool = False,
) -> dict[str, Any]:
    """Ensure the SSE HUD server is running and launch/surface the macOS floating HUD panel under a cross-process lock."""
    base = Path(data_dir).resolve() if data_dir else core.DATA_DIR
    base.mkdir(parents=True, exist_ok=True)

    if respect_dismiss:
        existing_fast = is_floating_hud_running(base)
        if existing_fast is not None:
            return {
                "ok": True,
                "alreadyRunning": True,
                "pid": int(existing_fast.get("pid") or 0),
                "url": str(existing_fast.get("url") or ""),
            }
        if is_floating_hud_dismissed(base):
            return {"ok": True, "skipped": True, "reason": "dismissed"}

    if not url:
        srv = hud_server.ensure_hud_server_running()
        if not srv.get("ok") or not str(srv.get("url") or "").startswith("http"):
            return {
                "ok": False,
                "error": "Unable to start local HUD HTTP server",
                "url": str(srv.get("url") or f"file://{core.SNAPSHOT_HTML_PATH}"),
            }
        url = str(srv["url"])

    target_url = _build_hud_url(url, compact=compact)
    expected_bin_path, _, state_path, frame_path = _floating_paths(base)
    dismiss_path = dismiss_marker_path(base)
    lock_path = base / "floating_hud.lock"

    with lock_path.open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            existing = is_floating_hud_running(base)
            if existing is not None:
                existing_pid = int(existing.get("pid") or 0)
                if respect_dismiss:
                    return {
                        "ok": True,
                        "alreadyRunning": True,
                        "pid": existing_pid,
                        "url": str(existing.get("url") or target_url),
                    }
                clear_dismissed_marker(base)
                if existing.get("url") == target_url:
                    if _verify_floating_hud_process(existing_pid, expected_executable=str(expected_bin_path)):
                        try:
                            os.kill(existing_pid, signal.SIGUSR1)
                            return {
                                "ok": True,
                                "alreadyRunning": True,
                                "pid": existing_pid,
                                "url": target_url,
                            }
                        except OSError:
                            pass
                else:
                    # URL/mode changed; stop verified older window and launch updated URL cleanly
                    if _verify_floating_hud_process(existing_pid, expected_executable=str(expected_bin_path)):
                        try:
                            os.kill(existing_pid, signal.SIGTERM)
                            _wait_process_exit(existing_pid, timeout=0.8)
                        except OSError:
                            pass
                    try:
                        state_path.unlink(missing_ok=True)
                    except OSError:
                        pass

            if respect_dismiss:
                if is_floating_hud_dismissed(base):
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": "dismissed",
                        "url": target_url,
                    }
            else:
                clear_dismissed_marker(base)

            try:
                binary_path = compile_floating_hud(base)
            except Exception as exc:
                core.append_error_log("compile_floating_hud", exc)
                return {
                    "ok": False,
                    "error": str(exc),
                    "url": target_url,
                }

            cmd = [
                str(binary_path),
                "--url",
                target_url,
                "--state-file",
                str(state_path),
                "--frame-file",
                str(frame_path),
                "--dismiss-file",
                str(dismiss_path),
            ]
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
            except Exception as exc:
                core.append_error_log("spawn_floating_hud", exc)
                return {
                    "ok": False,
                    "error": str(exc),
                    "url": target_url,
                    "binary": str(binary_path),
                }

            deadline = time.time() + 2.0
            while time.time() < deadline:
                running = is_floating_hud_running(base)
                if running is not None and int(running.get("pid") or 0) == proc.pid:
                    return {
                        "ok": True,
                        "alreadyRunning": False,
                        "pid": proc.pid,
                        "url": target_url,
                        "binary": str(binary_path),
                    }
                if proc.poll() is not None:
                    return {
                        "ok": False,
                        "error": f"Floating HUD exited early with code {proc.returncode}",
                        "url": target_url,
                        "binary": str(binary_path),
                    }
                time.sleep(0.08)

            if _verify_floating_hud_process(proc.pid, expected_executable=str(binary_path)):
                return {
                    "ok": True,
                    "alreadyRunning": False,
                    "pid": proc.pid,
                    "url": target_url,
                    "binary": str(binary_path),
                }
            return {
                "ok": False,
                "error": "Timed out waiting for floating HUD startup",
                "url": target_url,
                "binary": str(binary_path),
            }
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def ensure_floating_hud_auto_started(
    data_dir: Path | None = None,
    clear_dismiss: bool = False,
    reason: str = "hook",
) -> dict[str, Any]:
    """Non-blocking background trigger to auto-start the floating HUD if not already running or dismissed."""
    if sys.platform != "darwin":
        return {"ok": True, "skipped": True, "reason": "unsupported_platform"}
    if _auto_launch_disabled_by_env():
        return {"ok": True, "skipped": True, "reason": "disabled_by_env"}

    base = Path(data_dir).resolve() if data_dir else core.DATA_DIR
    base.mkdir(parents=True, exist_ok=True)

    if clear_dismiss:
        clear_dismissed_marker(base)

    existing = is_floating_hud_running(base)
    if existing is not None:
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_running",
            "pid": int(existing.get("pid") or 0),
        }

    if not clear_dismiss and is_floating_hud_dismissed(base):
        return {"ok": True, "skipped": True, "reason": "dismissed"}

    if not clear_dismiss:
        lock_path = base / "floating_hud.lock"
        with lock_path.open("a", encoding="utf-8") as lock_fh:
            acquired = False
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                return {"ok": True, "skipped": True, "reason": "launch_in_progress"}
            finally:
                if acquired:
                    try:
                        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--auto",
        "--data-dir",
        str(base),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return {
            "ok": True,
            "skipped": False,
            "spawned": True,
            "launcherPid": proc.pid,
            "reason": reason,
        }
    except Exception as exc:
        core.append_error_log(f"auto_start_floating_hud:{reason}", exc)
        return {"ok": False, "error": str(exc), "reason": reason}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile and launch Antigravity Next Step native floating HUD")
    parser.add_argument("--full", action="store_true", help="Open in full dashboard mode instead of compact mode")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-launch mode: respect manual dismiss marker and avoid disturbing an active window",
    )
    parser.add_argument("--compile-only", action="store_true", help="Compile the Swift binary and exit")
    parser.add_argument("--data-dir", default="", help="Optional PLUGIN_DATA override directory")
    args = parser.parse_args()

    if args.data_dir:
        core.set_data_dir(Path(args.data_dir))
    core.ensure_data_dirs()

    if args.compile_only:
        bin_path = compile_floating_hud()
        print(json.dumps({"ok": True, "binary": str(bin_path)}, ensure_ascii=False))
        return

    res = launch_floating_hud(compact=not args.full, respect_dismiss=args.auto)
    print(json.dumps(res, ensure_ascii=False))
    if not res.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
