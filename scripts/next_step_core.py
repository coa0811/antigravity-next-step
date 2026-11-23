#!/usr/bin/env python3
"""Core state machine, persistence, staleness detection, and HUD HTML rendering for next-step plugin."""

from __future__ import annotations

import copy
import datetime
import hashlib
import html
import json
import os
import re
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

MAX_HASH_FILE_BYTES = 2_000_000
WATCHDOG_TIMEOUT_SEC = 120.0
TOOL_APPROVAL_HINT_SEC = 8.0

MUTATING_FILE_TOOLS = {
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "delete_file",
}

APPROVAL_CANDIDATE_TOOLS = MUTATING_FILE_TOOLS | {
    "run_command",
    "ask_permission",
    "ask_question",
}

DATA_DIR = Path(
    os.environ.get("PLUGIN_DATA")
    or (Path.home() / ".gemini" / "antigravity" / "plugin_data" / "next-step")
)
SESSIONS_DIR = DATA_DIR / "sessions"
PENDING_DIR = DATA_DIR / "pending"
LATEST_PENDING_PATH = PENDING_DIR / "latest.json"
LATEST_MD_PATH = DATA_DIR / "latest_next_step.md"
SNAPSHOT_HTML_PATH = DATA_DIR / "hud_snapshot.html"
HUD_SERVER_JSON_PATH = DATA_DIR / "hud_server.json"
ERROR_LOG_PATH = DATA_DIR / "error.log"


def set_data_dir(new_dir: Path) -> None:
    """Redirect plugin data directory (used by tests and custom environments)."""
    global DATA_DIR, SESSIONS_DIR, PENDING_DIR, LATEST_PENDING_PATH
    global LATEST_MD_PATH, SNAPSHOT_HTML_PATH, HUD_SERVER_JSON_PATH, ERROR_LOG_PATH
    DATA_DIR = Path(new_dir).resolve()
    SESSIONS_DIR = DATA_DIR / "sessions"
    PENDING_DIR = DATA_DIR / "pending"
    LATEST_PENDING_PATH = PENDING_DIR / "latest.json"
    LATEST_MD_PATH = DATA_DIR / "latest_next_step.md"
    SNAPSHOT_HTML_PATH = DATA_DIR / "hud_snapshot.html"
    HUD_SERVER_JSON_PATH = DATA_DIR / "hud_server.json"
    ERROR_LOG_PATH = DATA_DIR / "error.log"


def ensure_data_dirs() -> None:
    """Create plugin data, session state, and pending buffer directories if absent."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)


def append_error_log(context: str, exc: BaseException) -> None:
    """Append diagnostic error entry without raising."""
    try:
        ensure_data_dirs()
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {context}: {exc}\n{tb}\n")
    except Exception:
        pass


def iso_now(ts: float | None = None) -> str:
    val = time.time() if ts is None else ts
    return datetime.datetime.fromtimestamp(val, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_arg(val: Any) -> Any:
    """Unwrap JSON-stringified argument values up to 2 levels."""
    cur = val
    for _ in range(2):
        if not isinstance(cur, str):
            break
        s = cur.strip()
        if (
            (s.startswith('"') and s.endswith('"'))
            or (s.startswith("[") and s.endswith("]"))
            or (s.startswith("{") and s.endswith("}"))
        ):
            try:
                cur = json.loads(s)
                continue
            except json.JSONDecodeError:
                break
        break
    return cur


def sanitize_user_input(raw: Any) -> str:
    """Strip wrapper XML tags and collapse whitespace into a concise user prompt summary."""
    text = str(raw or "")
    text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
    text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?USER_REQUEST>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def safe_session_filename(cid: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]", "_", (cid or "default").strip())
    return clean or "default"


def session_path(cid: str) -> Path:
    return SESSIONS_DIR / f"{safe_session_filename(cid)}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON via a temporary sibling file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def compute_file_sha256(path: Path) -> tuple[str | None, int | None]:
    """Compute SHA-256 and mtime_ns for a local file if it exists."""
    try:
        if not path.is_file():
            return None, None
        st = path.stat()
        h = hashlib.sha256()
        with path.open("rb") as fh:
            data = fh.read(MAX_HASH_FILE_BYTES)
            h.update(data)
            if st.st_size > MAX_HASH_FILE_BYTES:
                h.update(f":{st.st_size}:{st.st_mtime_ns}".encode("utf-8"))
        return h.hexdigest(), st.st_mtime_ns
    except OSError:
        return None, None


def resolve_plan_path(
    raw_path: str | None,
    workspace_paths: list[str] | None = None,
    artifact_dir: str | None = None,
) -> Path | None:
    """Resolve an absolute or relative artifact/workspace file path."""
    if not raw_path:
        return None
    s = str(raw_path).strip()
    if not s:
        return None
    if s.startswith("file:///"):
        s = s[len("file://") :]
    p = Path(s).expanduser()
    if p.is_absolute():
        try:
            return p.resolve()
        except OSError:
            return p

    candidates: list[Path] = []
    if artifact_dir:
        candidates.append(Path(artifact_dir).expanduser() / p)
    for ws in workspace_paths or []:
        if ws:
            candidates.append(Path(ws).expanduser() / p)

    for cand in candidates:
        if cand.exists():
            try:
                return cand.resolve()
            except OSError:
                return cand

    if candidates:
        try:
            return candidates[0].resolve()
        except OSError:
            return candidates[0]
    try:
        return p.resolve()
    except OSError:
        return p


def build_plan_artifact_entry(
    raw_path: str | None,
    workspace_paths: list[str] | None = None,
    artifact_dir: str | None = None,
    summary: str | None = None,
    request_feedback: bool = False,
) -> dict[str, Any] | None:
    resolved = resolve_plan_path(raw_path, workspace_paths, artifact_dir)
    if resolved is None:
        return None
    sha256, mtime_ns = compute_file_sha256(resolved)
    return {
        "path": str(resolved),
        "name": resolved.name,
        "sha256": sha256,
        "mtimeNs": mtime_ns,
        "summary": (str(summary).strip() or None) if summary else None,
        "requestFeedback": bool(request_feedback),
        "stale": False,
        "staleReason": None,
    }


def default_session_state(cid: str = "default") -> dict[str, Any]:
    now = time.time()
    clean_cid = (cid or "default").strip() or "default"
    return {
        "conversationId": clean_cid,
        "shortId": clean_cid[:8],
        "workspacePaths": [],
        "artifactDirectoryPath": "",
        "transcriptPath": "",
        "modelName": "",
        "lastUserInput": "",
        "turnUserInput": "",
        "userStepIndex": -1,
        "turnActive": False,
        "lastInvocationNum": -1,
        "lastTurnEndInvocationNum": -1,
        "invocationNum": 0,
        "stepIdx": 0,
        "lifecycle": "IDLE",
        "sourceType": "NONE",
        "confidence": "DETERMINISTIC",
        "waitingOnUser": False,
        "actor": "none",
        "nextAction": None,
        "detail": None,
        "suggestedPrompt": None,
        "options": [],
        "activeTool": None,
        "planArtifact": None,
        "pendingPlanReview": None,
        "pendingDeclaration": None,
        "fullyIdle": True,
        "terminationReason": None,
        "stale": False,
        "staleReason": None,
        "updatedAt": now,
        "updatedAtIso": iso_now(now),
    }


def load_session(cid: str) -> dict[str, Any]:
    ensure_data_dirs()
    p = session_path(cid)
    base = default_session_state(cid)
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                base.update(raw)
                base["conversationId"] = (str(raw.get("conversationId") or cid).strip() or "default")
                base["shortId"] = base["conversationId"][:8]
        except Exception:
            pass
    return base


def update_common_metadata(state: dict[str, Any], payload: dict[str, Any]) -> None:
    cid = str(payload.get("conversationId") or state.get("conversationId") or "default").strip() or "default"
    state["conversationId"] = cid
    state["shortId"] = cid[:8]

    ws = payload.get("workspacePaths")
    if isinstance(ws, list) and ws:
        state["workspacePaths"] = [str(x) for x in ws if x]
    elif payload.get("cwd") and not state.get("workspacePaths"):
        state["workspacePaths"] = [str(payload["cwd"])]

    if payload.get("artifactDirectoryPath"):
        state["artifactDirectoryPath"] = str(payload["artifactDirectoryPath"])
    if payload.get("transcriptPath"):
        state["transcriptPath"] = str(payload["transcriptPath"])
    if payload.get("modelName"):
        state["modelName"] = str(payload["modelName"])
    if payload.get("lastUserInput"):
        sanitized = sanitize_user_input(payload["lastUserInput"])
        if sanitized:
            state["lastUserInput"] = sanitized
    if payload.get("invocationNum") is not None:
        try:
            state["invocationNum"] = int(payload["invocationNum"])
        except (ValueError, TypeError):
            pass
    if payload.get("stepIdx") is not None:
        try:
            state["stepIdx"] = int(payload["stepIdx"])
        except (ValueError, TypeError):
            pass


def evaluate_staleness(state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Return a copy of session state with live staleness and tool approval timeout checks applied."""
    cur_now = time.time() if now is None else now
    out = copy.deepcopy(state)

    plan = out.get("planArtifact")
    if isinstance(plan, dict) and plan.get("path") and plan.get("sha256"):
        disk_sha, disk_mtime = compute_file_sha256(Path(str(plan["path"])))
        if disk_sha != plan.get("sha256"):
            plan["stale"] = True
            plan["staleReason"] = "关联计划文件已改写或移除"
            plan["currentSha256"] = disk_sha
            plan["currentMtimeNs"] = disk_mtime
            if (
                out.get("sourceType") == "AGENT_DECLARED"
                or out.get("lifecycle") == "WAITING_PLAN_REVIEW"
                or isinstance(out.get("pendingDeclaration"), dict)
                or isinstance(out.get("pendingPlanReview"), dict)
                or bool(out.get("suggestedPrompt"))
            ):
                out["stale"] = True
                out["staleReason"] = "关联计划文件已改写，当前下一步提示可能已过期"

    lifecycle = str(out.get("lifecycle") or "IDLE")
    updated_at = float(out.get("updatedAt") or cur_now)
    elapsed = max(0.0, cur_now - updated_at)

    if lifecycle in ("WORKING", "TOOL_RUNNING") and elapsed > WATCHDOG_TIMEOUT_SEC:
        out["stale"] = True
        out["staleReason"] = (
            f"运行态已超过 {int(elapsed)} 秒未更新，可能正在等待宿主权限弹窗确认或会话已中断"
        )

    active_tool = out.get("activeTool")
    if lifecycle == "TOOL_RUNNING" and isinstance(active_tool, dict):
        started_at = float(active_tool.get("startedAt") or updated_at)
        tool_elapsed = max(0.0, cur_now - started_at)
        active_tool["elapsedSec"] = round(tool_elapsed, 1)
        if active_tool.get("mayWaitApproval") and tool_elapsed >= TOOL_APPROVAL_HINT_SEC:
            active_tool["approvalHint"] = "工具耗时超 8 秒，若宿主弹出了权限确认框请先处理"

    return out


def save_session(cid: str, state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    ensure_data_dirs()
    cur_now = time.time() if now is None else now
    clean_cid = (cid or str(state.get("conversationId") or "default")).strip() or "default"
    state["conversationId"] = clean_cid
    state["shortId"] = clean_cid[:8]
    state["updatedAt"] = cur_now
    state["updatedAtIso"] = iso_now(cur_now)
    atomic_write_json(session_path(clean_cid), state)
    try:
        write_snapshots(state, now=cur_now)
    except Exception as exc:
        append_error_log("write_snapshots", exc)
    return state


def normalize_declaration(
    args: dict[str, Any],
    workspace_paths: list[str] | None = None,
    artifact_dir: str | None = None,
    step_idx: int | None = None,
) -> dict[str, Any] | None:
    """Validate and normalize a next_step_publish payload."""
    next_action = str(clean_arg(args.get("next_action") or args.get("nextAction") or "")).strip()
    if not next_action:
        return None

    raw_actor = str(clean_arg(args.get("actor") or "user")).strip().lower()
    if raw_actor not in ("user", "agent", "agent_background", "none"):
        raw_actor = "user"

    raw_waiting = clean_arg(args.get("waiting_on_user"))
    if raw_waiting is None:
        raw_waiting = clean_arg(args.get("waitingOnUser"))
    if isinstance(raw_waiting, bool):
        waiting_on_user = raw_waiting
    elif isinstance(raw_waiting, str):
        waiting_on_user = raw_waiting.strip().lower() not in ("false", "0", "no")
    else:
        waiting_on_user = raw_actor == "user"

    suggested_prompt_raw = clean_arg(args.get("suggested_prompt") or args.get("suggestedPrompt") or "")
    suggested_prompt = str(suggested_prompt_raw).strip() or None

    detail_raw = clean_arg(args.get("detail") or "")
    detail = str(detail_raw).strip() or None

    plan_path_raw = clean_arg(args.get("plan_path") or args.get("planPath") or "")
    plan_entry = None
    if isinstance(plan_path_raw, str) and plan_path_raw.strip():
        plan_entry = build_plan_artifact_entry(
            plan_path_raw.strip(),
            workspace_paths=workspace_paths,
            artifact_dir=artifact_dir,
        )

    now = time.time()
    return {
        "next_action": next_action,
        "actor": raw_actor,
        "waiting_on_user": waiting_on_user,
        "suggested_prompt": suggested_prompt,
        "detail": detail,
        "planArtifact": plan_entry,
        "stepIdx": int(step_idx or 0),
        "createdAt": now,
        "createdAtIso": iso_now(now),
    }


def stage_declaration(
    args: dict[str, Any],
    cid: str | None = None,
    workspace_paths: list[str] | None = None,
    artifact_dir: str | None = None,
    step_idx: int | None = None,
    write_pending_buffer: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Stage a normalized next-step declaration into session state and/or pending buffer."""
    ensure_data_dirs()
    eff_cid = (
        str(
            cid
            or clean_arg(args.get("_sys_conversation_id"))
            or clean_arg(args.get("conversation_id"))
            or clean_arg(args.get("conversationId"))
            or ""
        ).strip()
        or None
    )
    eff_ws = workspace_paths
    if not eff_ws:
        sys_ws = clean_arg(args.get("_sys_workspace_paths"))
        if isinstance(sys_ws, list):
            eff_ws = [str(x) for x in sys_ws if x]
    eff_artifact_dir = artifact_dir or str(clean_arg(args.get("_sys_artifact_dir") or "")).strip() or None
    eff_step_idx = step_idx
    if eff_step_idx is None and args.get("_sys_step_idx") is not None:
        try:
            eff_step_idx = int(clean_arg(args.get("_sys_step_idx")))
        except (ValueError, TypeError):
            eff_step_idx = 0

    state = load_session(eff_cid) if eff_cid else None
    if state is not None:
        if not eff_ws:
            eff_ws = state.get("workspacePaths") or []
        if not eff_artifact_dir:
            eff_artifact_dir = state.get("artifactDirectoryPath") or None

    decl = normalize_declaration(
        args,
        workspace_paths=eff_ws,
        artifact_dir=eff_artifact_dir,
        step_idx=eff_step_idx,
    )
    if decl is None:
        return None, state

    if write_pending_buffer and eff_cid:
        pending_payload = {
            "declaration": decl,
            "conversationId": eff_cid,
            "workspacePaths": eff_ws or [],
            "createdAt": decl["createdAt"],
            "claimedBy": eff_cid,
            "consumed": bool(state is not None),
        }
        atomic_write_json(LATEST_PENDING_PATH, pending_payload)

    if state is not None and eff_cid:
        state["pendingDeclaration"] = decl
        state["sourceType"] = "AGENT_DECLARED"
        state["confidence"] = "HIGH"
        state["waitingOnUser"] = bool(decl.get("waiting_on_user", True))
        state["actor"] = str(decl.get("actor") or ("user" if state["waitingOnUser"] else "agent"))
        state["nextAction"] = str(decl.get("next_action") or "").strip() or None
        state["detail"] = decl.get("detail")
        state["suggestedPrompt"] = decl.get("suggested_prompt")
        state["options"] = []
        state["planArtifact"] = decl.get("planArtifact")
        state["stale"] = False
        state["staleReason"] = None
        save_session(eff_cid, state)

    return decl, state


def clear_pending_buffer(cid: str | None = None) -> None:
    """Clear the pending declaration buffer if unattributed or belonging to cid."""
    if not LATEST_PENDING_PATH.is_file():
        return
    try:
        if cid is not None:
            raw = json.loads(LATEST_PENDING_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                owner = raw.get("conversationId")
                if owner and owner != cid:
                    return
        LATEST_PENDING_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def claim_latest_pending_declaration(
    cid: str,
    max_age_sec: float = 30.0,
    min_created_at: float = 0.0,
) -> dict[str, Any] | None:
    """Claim an unconsumed pending declaration strictly matching cid within max_age_sec."""
    if not cid or not LATEST_PENDING_PATH.is_file():
        return None
    try:
        raw = json.loads(LATEST_PENDING_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if raw.get("consumed") is True:
            return None
        target_cid = str(raw.get("conversationId") or "").strip()
        if not target_cid or target_cid != cid:
            return None
        created_at = float(raw.get("createdAt") or 0.0)
        if created_at < min_created_at:
            return None
        if time.time() - created_at > max_age_sec:
            return None
        decl = raw.get("declaration")
        if not isinstance(decl, dict):
            return None
        raw["claimedBy"] = cid
        raw["conversationId"] = cid
        raw["consumed"] = True
        atomic_write_json(LATEST_PENDING_PATH, raw)
        return decl
    except Exception:
        return None


def promote_plan_review_to_active(state: dict[str, Any], plan_review: dict[str, Any]) -> None:
    """Restore or promote a pending RequestFeedback plan review to active WAITING_PLAN_REVIEW state."""
    state["lifecycle"] = "WAITING_PLAN_REVIEW"
    state["sourceType"] = "APPROVAL_REQUIRED"
    state["confidence"] = "DETERMINISTIC"
    state["waitingOnUser"] = True
    state["actor"] = "user"
    state["nextAction"] = str(plan_review.get("nextAction") or "审阅计划工件并点击 Proceed 或提交评论")
    state["detail"] = plan_review.get("detail")
    state["options"] = []
    state["suggestedPrompt"] = None
    if isinstance(plan_review.get("planArtifact"), dict):
        state["planArtifact"] = plan_review["planArtifact"]
    state["stale"] = False
    state["staleReason"] = None
    evaluated = evaluate_staleness(state)
    state["stale"] = bool(evaluated.get("stale", False))
    state["staleReason"] = evaluated.get("staleReason")
    if isinstance(evaluated.get("planArtifact"), dict):
        state["planArtifact"] = evaluated["planArtifact"]


def promote_declaration_to_active(state: dict[str, Any], decl: dict[str, Any], fully_idle: bool = True) -> None:
    """Promote a staged declaration to the session's active AGENT_DECLARED next step."""
    waiting = bool(decl.get("waiting_on_user", True))
    actor = str(decl.get("actor") or ("user" if waiting else "agent"))
    state["lifecycle"] = "BACKGROUND_RUNNING" if not fully_idle else "IDLE"
    state["sourceType"] = "AGENT_DECLARED"
    state["confidence"] = "HIGH"
    state["waitingOnUser"] = waiting
    state["actor"] = actor
    state["nextAction"] = str(decl.get("next_action") or "").strip() or None
    state["detail"] = decl.get("detail")
    state["suggestedPrompt"] = decl.get("suggested_prompt")
    state["options"] = []
    state["planArtifact"] = (
        copy.deepcopy(decl.get("planArtifact"))
        if isinstance(decl.get("planArtifact"), dict)
        else None
    )
    state["pendingDeclaration"] = decl
    state["stale"] = False
    state["staleReason"] = None
    evaluated = evaluate_staleness(state)
    state["stale"] = bool(evaluated.get("stale", False))
    state["staleReason"] = evaluated.get("staleReason")
    if isinstance(evaluated.get("planArtifact"), dict):
        state["planArtifact"] = evaluated["planArtifact"]


def list_all_sessions(
    workspace_filter: str | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Load, evaluate staleness, filter, and sort all recorded sessions."""
    ensure_data_dirs()
    cur_now = time.time() if now is None else now
    norm_ws: str | None = None
    if workspace_filter:
        try:
            norm_ws = str(Path(workspace_filter).expanduser().resolve())
        except OSError:
            norm_ws = str(workspace_filter).strip()

    results: list[dict[str, Any]] = []
    for p in SESSIONS_DIR.glob("*.json"):
        if p.name.startswith("."):
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
        except Exception:
            continue

        evaluated = evaluate_staleness(raw, now=cur_now)
        if norm_ws:
            ws_list = [str(x) for x in (evaluated.get("workspacePaths") or []) if x]
            matched = False
            for w in ws_list:
                try:
                    rw = str(Path(w).expanduser().resolve())
                except OSError:
                    rw = w
                if rw == norm_ws or rw.startswith(norm_ws + "/") or norm_ws.startswith(rw + "/"):
                    matched = True
                    break
            if not matched:
                continue
        results.append(evaluated)

    results.sort(
        key=lambda s: (
            0 if s.get("waitingOnUser") else 1,
            -float(s.get("updatedAt") or 0.0),
            str(s.get("conversationId") or ""),
        )
    )
    return results


def format_actor_label(actor: str | None, waiting_on_user: bool = False) -> str:
    a = (actor or "").strip().lower()
    if a == "user":
        return "用户"
    if a == "agent":
        return "Agent（主会话）"
    if a == "agent_background":
        return "Agent（后台任务 / 子 Agent）"
    if waiting_on_user:
        return "用户"
    return "—"


def format_source_label(source_type: str | None, confidence: str | None = None) -> str:
    st = (source_type or "").strip().upper()
    if st == "APPROVAL_REQUIRED":
        return "系统待办 / 审批拦截（确定性事实）"
    if st == "AGENT_DECLARED":
        return "Agent 显式声明（高可信）"
    if st == "LIFECYCLE":
        return "生命周期运行态（系统事实）"
    return "Stop 终止态核实（无已声明下一步）"


def format_status_badge(state: dict[str, Any]) -> str:
    if state.get("stale"):
        return "🟡 状态可能已过期 (Stale)"
    if state.get("waitingOnUser"):
        lifecycle = str(state.get("lifecycle") or "")
        if lifecycle == "WAITING_PLAN_REVIEW":
            return "🔴 等我审阅计划 (Waiting Plan Review)"
        if lifecycle == "WAITING_USER_DECISION":
            return "🔴 等我回答/选择 (Waiting User Decision)"
        if lifecycle == "WAITING_USER_APPROVAL":
            return "🔴 等我授权 (Waiting Permission)"
        if lifecycle in ("ERROR", "CANCELED"):
            return f"🔴 已中止 / 待介入 ({lifecycle})"
        return "🔴 等我确认 / 发出指令 (Waiting on You)"
    lifecycle = str(state.get("lifecycle") or "IDLE")
    if lifecycle in ("WORKING", "TOOL_RUNNING"):
        return f"🟢 Agent 运行中 ({lifecycle})"
    if lifecycle == "BACKGROUND_RUNNING":
        return "🟢 后台任务运行中 (Background Active)"
    return "⚪ 会话空闲（无挂起待办）"


def format_session_markdown(state: dict[str, Any]) -> str:
    """Format a single evaluated session state into concise Markdown."""
    ev = evaluate_staleness(state)
    cid = str(ev.get("conversationId") or "default")
    short_id = str(ev.get("shortId") or cid[:8])
    ws_list = ev.get("workspacePaths") or []
    ws_name = Path(ws_list[0]).name if ws_list else "default-workspace"
    last_input = ev.get("lastUserInput") or "（尚无用户输入记录）"
    badge = format_status_badge(ev)
    actor_label = format_actor_label(ev.get("actor"), bool(ev.get("waitingOnUser")))
    source_label = format_source_label(ev.get("sourceType"), ev.get("confidence"))

    next_action = ev.get("nextAction")
    if not next_action:
        if ev.get("pendingDeclaration"):
            next_action = f"[本轮已暂存声明] {ev['pendingDeclaration'].get('next_action')}"
        else:
            next_action = "当前无已声明的下一步（上一轮已正常结束，等待你输入新指令）"

    lines = [
        f"### 会话 `#{short_id}` · `{ws_name}`",
        f"- **完整会话 ID**：`{cid}`",
        f"- **最近用户输入**：{last_input}",
        f"- **是否在等我**：`{badge}`",
        f"- **由谁做**：`{actor_label}`",
        f"- **接下来要做什么**：**{next_action}**",
        f"- **来源与可信度**：`{source_label}`",
        f"- **最后更新时间**：`{ev.get('updatedAtIso') or ''}`",
    ]
    if ev.get("detail"):
        lines.append(f"- **补充说明**：{ev['detail']}")
    if ev.get("stale") and ev.get("staleReason"):
        lines.append(f"- **过期提示**：`🟡 {ev['staleReason']}`")
    active_tool = ev.get("activeTool")
    if isinstance(active_tool, dict) and active_tool.get("approvalHint"):
        lines.append(f"- **权限弹窗提示**：`🟡 {active_tool['approvalHint']}`")
    options = ev.get("options") or []
    if options:
        lines.append("- **待选条目**：")
        for idx, opt in enumerate(options, 1):
            lines.append(f"  {idx}. `{opt}`")
    plan = ev.get("planArtifact")
    if isinstance(plan, dict) and plan.get("path"):
        plan_status = "🟡 已过期（文件已改写）" if plan.get("stale") else "未过期"
        lines.append(f"- **关联计划工件**：`{plan.get('name') or plan['path']}`（{plan_status}）")
    copy_blocked = bool(ev.get("stale") or (isinstance(plan, dict) and plan.get("stale")))
    if ev.get("suggestedPrompt") and not copy_blocked:
        lines.append(f"- **建议下一条指令（只读复制，由你决定是否提交）**：\n  ```text\n  {ev['suggestedPrompt']}\n  ```")
    elif ev.get("suggestedPrompt") and copy_blocked:
        lines.append("- **建议下一条指令**：`🟡 已过期，已阻止直接复制`")
    return "\n".join(lines)


def _clip_str(val: Any, max_len: int) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 1)] + "…"


def build_session_panel_item(state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Build a bounded, strictly read-only structured view model for one session."""
    ev = evaluate_staleness(state, now=now)
    cid = str(ev.get("conversationId") or "default")
    short_id = str(ev.get("shortId") or cid[:8])
    ws_list = [str(w) for w in (ev.get("workspacePaths") or []) if w]
    ws_name = Path(ws_list[0]).name if ws_list else "default-workspace"

    plan = ev.get("planArtifact")
    plan_item: dict[str, Any] | None = None
    if isinstance(plan, dict) and plan.get("path"):
        plan_item = {
            "path": _clip_str(plan.get("path"), 500),
            "name": _clip_str(plan.get("name") or Path(str(plan["path"])).name, 160),
            "sha256": plan.get("sha256"),
            "stale": bool(plan.get("stale", False)),
            "staleReason": _clip_str(plan.get("staleReason"), 200),
        }

    active_tool = ev.get("activeTool")
    tool_item: dict[str, Any] | None = None
    if isinstance(active_tool, dict) and active_tool.get("name"):
        tool_item = {
            "name": _clip_str(active_tool.get("name"), 120),
            "summary": _clip_str(active_tool.get("summary"), 240),
            "approvalHint": _clip_str(active_tool.get("approvalHint"), 240),
            "elapsedSec": active_tool.get("elapsedSec"),
        }

    is_stale = bool(ev.get("stale", False))
    copy_blocked = bool(is_stale or (isinstance(plan_item, dict) and plan_item.get("stale")))
    raw_opts = ev.get("options") or []
    options = [
        clipped
        for clipped in (_clip_str(opt, 300) for opt in raw_opts[:12])
        if clipped
    ]

    waiting_on_user = bool(ev.get("waitingOnUser", False))
    actor = str(ev.get("actor") or "none")
    raw_prompt = ev.get("suggestedPrompt")
    suggested_prompt = (
        str(raw_prompt).strip() or None
        if raw_prompt is not None
        else None
    )
    return {
        "conversationId": cid,
        "shortId": short_id,
        "workspaceName": ws_name,
        "workspacePaths": ws_list[:4],
        "modelName": _clip_str(ev.get("modelName"), 120),
        "lastUserInput": _clip_str(ev.get("lastUserInput"), 240),
        "lifecycle": str(ev.get("lifecycle") or "IDLE"),
        "sourceType": str(ev.get("sourceType") or "NONE"),
        "confidence": str(ev.get("confidence") or "DETERMINISTIC"),
        "waitingOnUser": waiting_on_user,
        "actor": actor,
        "actorLabel": format_actor_label(actor, waiting_on_user),
        "statusBadge": format_status_badge(ev),
        "sourceLabel": format_source_label(ev.get("sourceType"), ev.get("confidence")),
        "nextAction": _clip_str(ev.get("nextAction"), 500),
        "detail": _clip_str(ev.get("detail"), 1500),
        "options": options,
        "suggestedPrompt": suggested_prompt,
        "copyBlocked": copy_blocked,
        "stale": is_stale,
        "staleReason": _clip_str(ev.get("staleReason"), 240),
        "planArtifact": plan_item,
        "activeTool": tool_item,
        "updatedAt": ev.get("updatedAt"),
        "updatedAtIso": ev.get("updatedAtIso") or "",
    }


def build_side_panel_payload(
    bound_cid: str | None = None,
    binding_mode: str = "unbound",
    workspace_filter: str | None = None,
    limit: int = 20,
    now: float | None = None,
) -> dict[str, Any]:
    """Build strictly read-only, session-isolated payload for the MCP Apps Side Panel."""
    cur_now = time.time() if now is None else now
    clean_cid = str(bound_cid or "").strip() or None
    all_sessions = list_all_sessions(workspace_filter=workspace_filter, now=cur_now)
    all_global = (
        list_all_sessions(workspace_filter=None, now=cur_now)
        if workspace_filter and clean_cid
        else all_sessions
    )

    eff_mode = "unbound"
    resolved_cid: str | None = None
    selected_item: dict[str, Any] | None = None
    not_found = False
    ambiguous = False

    if clean_cid:
        sp = session_path(clean_cid)
        if sp.is_file():
            raw_state = load_session(clean_cid)
            selected_item = build_session_panel_item(raw_state, now=cur_now)
            resolved_cid = selected_item["conversationId"]
            eff_mode = binding_mode if binding_mode in ("hook_bound", "explicit") else "explicit"
        else:
            matches = [
                s for s in all_global if str(s.get("conversationId") or "").startswith(clean_cid)
            ]
            if len(matches) == 1:
                selected_item = build_session_panel_item(matches[0], now=cur_now)
                resolved_cid = selected_item["conversationId"]
                eff_mode = binding_mode if binding_mode in ("hook_bound", "explicit") else "explicit"
            elif len(matches) > 1:
                ambiguous = True
                eff_mode = "unbound"
            else:
                not_found = True
                resolved_cid = clean_cid
                eff_mode = binding_mode if binding_mode in ("hook_bound", "explicit") else "explicit"

    session_items = [build_session_panel_item(s, now=cur_now) for s in all_sessions[: max(1, limit)]]
    return {
        "version": "0.3.0",
        "generatedAtIso": iso_now(cur_now),
        "bindingMode": eff_mode,
        "boundConversationId": resolved_cid,
        "sessionNotFound": not_found,
        "ambiguousPrefix": ambiguous,
        "selectedSession": selected_item,
        "sessions": session_items,
        "totalSessions": len(all_sessions),
        "waitingCount": sum(1 for s in all_sessions if s.get("waitingOnUser")),
    }


def format_side_panel_text(payload: dict[str, Any]) -> str:
    """Format concise plain-text Markdown fallback for non-UI MCP clients."""
    mode = payload.get("bindingMode")
    cid = payload.get("boundConversationId")
    sessions = payload.get("sessions") or []

    if mode == "unbound":
        header = (
            "**Next Step HUD（未绑定当前会话）**："
            + (
                "传入的会话前缀匹配多个会话，已回退为未绑定状态。\n\n"
                if payload.get("ambiguousPrefix")
                else "未传入 `conversation_id` 且无宿主 Hook 绑定，以下列出已记录会话（不自动猜测当前会话）：\n\n"
            )
        )
        if not sessions:
            return header + "当前暂无已记录的会话状态。"
        lines = [header.rstrip()]
        for item in sessions[:10]:
            action = item.get("nextAction") or "当前无已声明的下一步"
            lines.append(
                f"- `#{item['shortId']}` · `{item['workspaceName']}` · `{item['statusBadge']}` · {action}"
            )
        return "\n".join(lines)

    if payload.get("sessionNotFound"):
        return f"**Next Step HUD（已绑定会话 `{str(cid)[:8]}`）**：尚无该会话的状态记录（尚未触发 Hook 或发布下一步）。"

    sel = payload.get("selectedSession")
    if isinstance(sel, dict):
        # Reuse format_session_markdown directly if the session file exists on disk
        if cid and session_path(str(cid)).is_file():
            return format_session_markdown(load_session(str(cid)))
        next_act = sel.get("nextAction") or "当前无已声明的下一步"
        lines = [
            f"### 会话 `#{sel['shortId']}` · `{sel['workspaceName']}`",
            f"- **完整会话 ID**：`{sel['conversationId']}`",
            f"- **是否在等我**：`{sel['statusBadge']}`",
            f"- **由谁做**：`{sel['actorLabel']}`",
            f"- **接下来要做什么**：**{next_act}**",
            f"- **来源与可信度**：`{sel['sourceLabel']}`",
        ]
        if sel.get("stale") and sel.get("staleReason"):
            lines.append(f"- **过期提示**：`🟡 {sel['staleReason']}`")
        if sel.get("suggestedPrompt") and not sel.get("copyBlocked"):
            lines.append(f"- **建议下一条指令（只读复制）**：`{sel['suggestedPrompt']}`")
        elif sel.get("suggestedPrompt") and sel.get("copyBlocked"):
            lines.append("- **建议下一条指令**：`🟡 已过期，已阻止直接复制`")
        return "\n".join(lines)

    return "当前暂无已记录的会话状态。"


def write_snapshots(latest_state: dict[str, Any] | None = None, now: float | None = None) -> None:
    """Write latest_next_step.md and static hud_snapshot.html to DATA_DIR."""
    ensure_data_dirs()
    sessions = list_all_sessions(now=now)
    if latest_state is not None and not sessions:
        sessions = [evaluate_staleness(latest_state, now=now)]

    md_parts = ["# Antigravity Next Step HUD 快照", ""]
    if not sessions:
        md_parts.append("当前暂无已记录的会话状态。")
    else:
        for s in sessions[:15]:
            md_parts.append(format_session_markdown(s))
            md_parts.append("")
    LATEST_MD_PATH.write_text("\n".join(md_parts), encoding="utf-8")
    SNAPSHOT_HTML_PATH.write_text(render_hud_html(sessions, live_sse=False), encoding="utf-8")


def render_hud_html(
    initial_sessions: list[dict[str, Any]] | None = None,
    live_sse: bool = True,
    compact: bool = False,
) -> str:
    """Render a self-contained light/dark adaptive HTML dashboard (full or compact) for the Next Step HUD."""
    sessions_json = json.dumps(initial_sessions or [], ensure_ascii=False).replace("</", "<\\/")
    live_flag = "true" if live_sse else "false"
    compact_flag = "true" if compact else "false"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Next Step HUD · Antigravity</title>
  <style>
    :root {{
      --bg: var(--background, #0f1117);
      --card-bg: var(--card, #181b23);
      --border-col: var(--card-border, #2a2f3d);
      --fg: var(--foreground, #f1f5f9);
      --muted: var(--muted-foreground, #94a3b8);
      --accent-col: var(--primary, #38bdf8);
      --danger: var(--error, #f43f5e);
      --ok: var(--success, #10b981);
      --warn: var(--warning, #f59e0b);
      --code-bg: rgba(15, 23, 42, 0.65);
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: var(--background, #f8fafc);
        --card-bg: var(--card, #ffffff);
        --border-col: var(--card-border, #e2e8f0);
        --fg: var(--foreground, #0f172a);
        --muted: var(--muted-foreground, #64748b);
        --accent-col: var(--primary, #0284c7);
        --code-bg: #f1f5f9;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 14px;
      background: var(--bg);
      color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 13px;
      line-height: 1.5;
    }}
    body.mode-compact {{ padding: 8px 10px; font-size: 12px; line-height: 1.35; }}
    .container {{ max-width: 760px; margin: 0 auto; }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding-bottom: 10px;
      margin-bottom: 12px;
      border-bottom: 1px solid var(--border-col);
    }}
    body.mode-compact .topbar {{ padding-bottom: 6px; margin-bottom: 6px; }}
    .title {{ font-weight: 700; font-size: 14px; display: flex; align-items: center; gap: 8px; }}
    body.mode-compact .title {{ font-size: 13px; }}
    .topbar-right {{ display: flex; align-items: center; gap: 6px; }}
    .mode-btn {{
      background: var(--card-bg);
      color: var(--fg);
      border: 1px solid var(--border-col);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      cursor: pointer;
    }}
    .mode-btn:hover {{ border-color: var(--accent-col); color: var(--accent-col); }}
    .conn-pill {{
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--border-col);
      color: var(--muted);
    }}
    .tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 12px;
    }}
    body.mode-compact .tabs {{ flex-wrap: nowrap; overflow-x: auto; margin-bottom: 6px; padding-bottom: 2px; }}
    .tab-btn {{
      background: var(--card-bg);
      color: var(--fg);
      border: 1px solid var(--border-col);
      border-radius: 6px;
      padding: 5px 10px;
      font-size: 12px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }}
    body.mode-compact .tab-btn {{ padding: 3px 8px; font-size: 11px; }}
    .tab-btn.active {{
      border-color: var(--accent-col);
      box-shadow: 0 0 0 1px var(--accent-col);
    }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
    .dot-wait {{ background: var(--danger); }}
    .dot-work {{ background: var(--ok); }}
    .dot-stale {{ background: var(--warn); }}
    .dot-idle {{ background: var(--muted); }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border-col);
      border-radius: 10px;
      padding: 14px 16px;
      margin-bottom: 12px;
    }}
    body.mode-compact .card {{ padding: 8px 10px; margin-bottom: 4px; border-radius: 8px; }}
    .session-meta {{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 10px;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 6px;
    }}
    body.mode-compact .session-meta {{ font-size: 11px; margin-bottom: 6px; }}
    .grid-3 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    body.mode-compact .grid-3 {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 6px;
    }}
    .metric-box {{
      border: 1px solid var(--border-col);
      border-radius: 8px;
      padding: 8px 10px;
    }}
    body.mode-compact .metric-box {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 11px;
    }}
    .metric-label {{ font-size: 11px; color: var(--muted); margin-bottom: 3px; }}
    body.mode-compact .metric-label {{ margin-bottom: 0; font-size: 10px; }}
    .metric-label::after {{ content: ""; }}
    body.mode-compact .metric-label::after {{ content: ":"; }}
    .metric-val {{ font-weight: 600; font-size: 13px; }}
    body.mode-compact .metric-val {{ font-size: 11px !important; }}
    .action-box {{
      border-left: 3px solid var(--accent-col);
      background: var(--code-bg);
      padding: 10px 12px;
      border-radius: 0 8px 8px 0;
      margin-bottom: 12px;
    }}
    body.mode-compact .action-box {{ padding: 6px 10px; margin-bottom: 6px; }}
    .action-box.waiting {{ border-left-color: var(--danger); }}
    .action-box.stale {{ border-left-color: var(--warn); }}
    .action-headline {{ font-size: 15px; font-weight: 700; margin-bottom: 4px; }}
    body.mode-compact .action-headline {{ font-size: 13px; margin-bottom: 2px; }}
    .action-detail {{ font-size: 12px; color: var(--muted); }}
    body.mode-compact .action-detail {{ font-size: 11px; }}
    .options-list {{ margin: 8px 0 0 0; padding-left: 18px; font-size: 12px; }}
    .options-list li {{ margin-bottom: 3px; }}
    .option-chip-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }}
    .opt-copy-btn {{
      background: var(--card-bg);
      color: var(--fg);
      border: 1px solid var(--border-col);
      border-radius: 6px;
      padding: 3px 8px;
      font-size: 11px;
      cursor: pointer;
      text-align: left;
    }}
    .opt-copy-btn:hover {{ border-color: var(--accent-col); color: var(--accent-col); }}
    .prompt-box {{
      border: 1px solid var(--border-col);
      border-radius: 8px;
      padding: 10px;
      background: var(--code-bg);
      margin-top: 10px;
    }}
    body.mode-compact .prompt-box {{
      padding: 6px 8px;
      margin-top: 6px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }}
    .prompt-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    body.mode-compact .prompt-header {{ margin-bottom: 0; order: 2; flex-shrink: 0; }}
    body.mode-compact .prompt-header span {{ display: none; }}
    .copy-btn {{
      background: transparent;
      color: var(--accent-col);
      border: 1px solid var(--accent-col);
      border-radius: 5px;
      padding: 2px 8px;
      font-size: 11px;
      cursor: pointer;
      white-space: nowrap;
    }}
    body.mode-compact .copy-btn {{
      background: var(--accent-col);
      color: #0b1120;
      font-weight: 700;
      padding: 4px 10px;
    }}
    .copy-btn:disabled {{
      opacity: 0.45;
      cursor: not-allowed;
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
    }}
    body.mode-compact pre {{
      order: 1;
      flex: 1;
      font-size: 11px;
      max-height: 36px;
      overflow-y: auto;
    }}
    .footer-meta {{
      font-size: 11px;
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 8px;
      border-top: 1px solid var(--border-col);
      padding-top: 8px;
      margin-top: 10px;
    }}
    body.mode-compact .footer-meta {{ display: none; }}
    .warn-banner {{
      background: rgba(245, 158, 11, 0.14);
      border: 1px solid var(--warn);
      color: var(--fg);
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 12px;
      margin-bottom: 10px;
    }}
    body.mode-compact .warn-banner {{ padding: 4px 8px; font-size: 11px; margin-bottom: 6px; }}
  </style>
</head>
<body class="{'mode-compact' if compact else ''}">
  <div class="container">
    <div class="topbar">
      <div class="title">
        <span>Next Step HUD</span>
        <span id="subtitleText" style="font-weight:400;font-size:11px;color:var(--muted);">只读悬浮提示</span>
      </div>
      <div class="topbar-right">
        <button id="modeToggleBtn" class="mode-btn" onclick="toggleCompactMode()">{'完整视图' if compact else '精简悬浮'}</button>
        <span id="connStatus" class="conn-pill">初始化中…</span>
      </div>
    </div>
    <div id="sessionTabs" class="tabs"></div>
    <div id="mainView"></div>
  </div>

  <script>
    let sessions = {sessions_json};
    let selectedId = null;
    const isLiveSse = {live_flag};
    let isCompact = {compact_flag};
    if (window.location && window.location.search && window.location.search.includes('mode=compact')) {{
      isCompact = true;
    }}

    function syncCompactClass() {{
      document.body.classList.toggle('mode-compact', isCompact);
      const btn = document.getElementById('modeToggleBtn');
      if (btn) btn.textContent = isCompact ? '完整视图' : '精简悬浮';
    }}

    window.toggleCompactMode = function() {{
      isCompact = !isCompact;
      syncCompactClass();
      render();
    }};

    function esc(str) {{
      const d = document.createElement('div');
      d.textContent = str == null ? '' : String(str);
      return d.innerHTML;
    }}

    function dotClass(s) {{
      if (s.stale) return 'dot dot-stale';
      if (s.waitingOnUser) return 'dot dot-wait';
      if (['WORKING', 'TOOL_RUNNING', 'BACKGROUND_RUNNING'].includes(s.lifecycle)) return 'dot dot-work';
      return 'dot dot-idle';
    }}

    function statusText(s) {{
      if (s.stale) return '🟡 状态可能已过期';
      if (s.waitingOnUser) {{
        if (s.lifecycle === 'WAITING_PLAN_REVIEW') return '🔴 等我审阅计划 (Proceed / 评论)';
        if (s.lifecycle === 'WAITING_USER_DECISION') return '🔴 等我回答选择题';
        if (s.lifecycle === 'WAITING_USER_APPROVAL') return '🔴 等我授权权限请求';
        if (s.lifecycle === 'ERROR' || s.lifecycle === 'CANCELED') return '🔴 已中止 / 待介入';
        return '🔴 等我确认 / 发出指令';
      }}
      if (s.lifecycle === 'WORKING' || s.lifecycle === 'TOOL_RUNNING') return '🟢 Agent 运行中（无需操作）';
      if (s.lifecycle === 'BACKGROUND_RUNNING') return '🟢 后台任务运行中';
      return '⚪ 会话空闲（无挂起待办）';
    }}

    function actorText(s) {{
      const a = (s.actor || '').toLowerCase();
      if (a === 'user') return '用户';
      if (a === 'agent') return 'Agent（主会话）';
      if (a === 'agent_background') return 'Agent（后台任务 / 子 Agent）';
      if (s.waitingOnUser) return '用户';
      return '—';
    }}

    function sourceText(s) {{
      const st = (s.sourceType || '').toUpperCase();
      if (st === 'APPROVAL_REQUIRED') return '系统待办 / 审批拦截（确定性事实）';
      if (st === 'AGENT_DECLARED') return 'Agent 显式声明（高可信）';
      if (st === 'LIFECYCLE') return '生命周期运行态（系统事实）';
      return 'Stop 终止态核实（无已声明下一步）';
    }}

    function wsName(s) {{
      const list = s.workspacePaths || [];
      if (!list.length) return 'workspace';
      const parts = String(list[0]).split('/').filter(Boolean);
      return parts[parts.length - 1] || list[0];
    }}

    window.copyText = async function(text, btnEl, origLabel) {{
      if (!text) return;
      let copied = false;
      try {{
        if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.copyText) {{
          window.webkit.messageHandlers.copyText.postMessage(String(text));
          copied = true;
        }}
      }} catch (_) {{}}

      if (!copied && navigator.clipboard && navigator.clipboard.writeText) {{
        try {{
          await navigator.clipboard.writeText(String(text));
          copied = true;
        }} catch (_) {{}}
      }}

      if (!copied) {{
        try {{
          const ta = document.createElement('textarea');
          ta.value = String(text);
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          copied = document.execCommand('copy');
          document.body.removeChild(ta);
        }} catch (_) {{}}
      }}

      if (copied && btnEl) {{
        const label = origLabel || btnEl.textContent;
        btnEl.textContent = '已复制 ✓';
        setTimeout(() => {{ btnEl.textContent = label; }}, 1400);
      }}
    }};

    window.copyOption = function(idx, btnEl) {{
      const cur = sessions.find(s => s.conversationId === selectedId) || sessions[0];
      if (!cur || !cur.options || cur.options[idx] == null) return;
      window.copyText(cur.options[idx], btnEl, btnEl ? btnEl.textContent : '');
    }};

    function render() {{
      syncCompactClass();
      const tabsEl = document.getElementById('sessionTabs');
      const mainEl = document.getElementById('mainView');

      if (!sessions || !sessions.length) {{
        tabsEl.innerHTML = '';
        mainEl.innerHTML = '<div class="card">当前暂无已记录的 Antigravity 会话状态。当会话触发 Hook 或调用 <code>next_step_publish</code> 时将自动显示。</div>';
        return;
      }}

      if (!selectedId || !sessions.some(s => s.conversationId === selectedId)) {{
        selectedId = sessions[0].conversationId;
      }}

      tabsEl.innerHTML = sessions.map(s => {{
        const active = s.conversationId === selectedId ? 'active' : '';
        const shortId = s.shortId || String(s.conversationId).slice(0, 8);
        return `<button class="tab-btn ${{active}}" onclick="selectSession('${{esc(s.conversationId)}}')">
          <span class="${{dotClass(s)}}"></span>
          <span>#${{esc(shortId)}}</span>
          <span style="color:var(--muted)">${{esc(wsName(s))}}</span>
        </button>`;
      }}).join('');

      const cur = sessions.find(s => s.conversationId === selectedId) || sessions[0];
      const shortId = cur.shortId || String(cur.conversationId).slice(0, 8);
      const nextHeadline = cur.nextAction
        ? cur.nextAction
        : '当前无已声明的下一步（上一轮已正常结束，等待你输入新指令）';
      const boxCls = cur.stale ? 'action-box stale' : (cur.waitingOnUser ? 'action-box waiting' : 'action-box');

      let staleBanner = '';
      if (cur.stale && cur.staleReason) {{
        staleBanner = `<div class="warn-banner">🟡 <strong>过期提示：</strong>${{esc(cur.staleReason)}}</div>`;
      }}
      if (cur.activeTool && cur.activeTool.approvalHint) {{
        staleBanner += `<div class="warn-banner">🟡 <strong>审批提示：</strong>${{esc(cur.activeTool.approvalHint)}}</div>`;
      }}

      let optionsHtml = '';
      if (cur.options && cur.options.length) {{
        if (isCompact) {{
          optionsHtml = '<div class="option-chip-row">' + cur.options.map((o, i) =>
            `<button class="opt-copy-btn" title="点击复制此选项" onclick="copyOption(${{i}}, this)">📋 ${{esc(o)}}</button>`
          ).join('') + '</div>';
        }} else {{
          optionsHtml = '<ol class="options-list">' + cur.options.map((o, i) =>
            `<li>${{esc(o)}} <button class="copy-btn" style="margin-left:6px;" onclick="copyOption(${{i}}, this)">复制</button></li>`
          ).join('') + '</ol>';
        }}
      }}

      let planHtml = '';
      if (cur.planArtifact && cur.planArtifact.path) {{
        const pStale = cur.planArtifact.stale ? '🟡 已过期：计划已改写' : '✓ 未过期';
        planHtml = `<span>关联计划工件：<code>${{esc(cur.planArtifact.name || cur.planArtifact.path)}}</code> (${{pStale}})</span>`;
      }}

      let promptHtml = '';
      if (cur.suggestedPrompt) {{
        const isPromptStale = Boolean(cur.stale || (cur.planArtifact && cur.planArtifact.stale));
        const disabledAttr = isPromptStale ? 'disabled title="状态已过期，请先确认最新计划"' : '';
        promptHtml = `<div class="prompt-box">
          <div class="prompt-header">
            <span>建议下一条指令（只读展示 · 复制后由你决定是否粘贴提交）</span>
            <button id="copyBtn" class="copy-btn" ${{disabledAttr}} onclick="copyPrompt()">复制指令</button>
          </div>
          <pre id="promptText">${{esc(cur.suggestedPrompt)}}</pre>
        </div>`;
      }}

      mainEl.innerHTML = `<div class="card">
        <div class="session-meta">
          <span><strong>#${{esc(shortId)}}</strong> · <code>${{esc(wsName(cur))}}</code> · ${{esc(cur.lastUserInput || '尚无用户输入')}}</span>
          <span>${{esc(cur.updatedAtIso || '')}}</span>
        </div>
        ${{staleBanner}}
        <div class="grid-3">
          <div class="metric-box">
            <div class="metric-label">是否在等我</div>
            <div class="metric-val">${{esc(statusText(cur))}}</div>
          </div>
          <div class="metric-box">
            <div class="metric-label">由谁做</div>
            <div class="metric-val">${{esc(actorText(cur))}}</div>
          </div>
          <div class="metric-box">
            <div class="metric-label">来源可信度</div>
            <div class="metric-val" style="font-size:12px;">${{esc(sourceText(cur))}}</div>
          </div>
        </div>
        <div class="${{boxCls}}">
          <div class="metric-label">接下来要做什么</div>
          <div class="action-headline">${{esc(nextHeadline)}}</div>
          ${{cur.detail ? `<div class="action-detail">${{esc(cur.detail)}}</div>` : ''}}
          ${{optionsHtml}}
        </div>
        ${{promptHtml}}
        <div class="footer-meta">
          <span>完整会话 ID: <code>${{esc(cur.conversationId)}}</code> (Step #${{esc(cur.stepIdx || 0)}})</span>
          ${{planHtml}}
        </div>
      </div>`;
    }}

    window.selectSession = function(id) {{
      selectedId = id;
      render();
    }};

    window.copyPrompt = function() {{
      const cur = sessions.find(s => s.conversationId === selectedId) || sessions[0];
      if (!cur || !cur.suggestedPrompt) return;
      const btn = document.getElementById('copyBtn');
      window.copyText(cur.suggestedPrompt, btn, '复制指令');
    }};

    async function fetchSessions() {{
      try {{
        const res = await fetch('/api/sessions');
        if (res.ok) {{
          const data = await res.json();
          sessions = data.sessions || [];
          render();
        }}
      }} catch (_) {{}}
    }}

    render();

    const connEl = document.getElementById('connStatus');
    if (!isLiveSse) {{
      connEl.textContent = '静态快照模式';
    }} else if (typeof EventSource !== 'undefined') {{
      const es = new EventSource('/api/events');
      es.onopen = () => {{ connEl.textContent = '🟢 实时'; }};
      es.onmessage = (ev) => {{
        try {{
          const data = JSON.parse(ev.data);
          if (data.sessions) {{
            sessions = data.sessions;
            render();
          }}
        }} catch (_) {{}}
      }};
      es.onerror = () => {{
        connEl.textContent = '🟡 轮询中';
      }};
      setInterval(fetchSessions, 3000);
    }} else {{
      connEl.textContent = '轮询模式';
      setInterval(fetchSessions, 1500);
    }}
  </script>
</body>
</html>"""
