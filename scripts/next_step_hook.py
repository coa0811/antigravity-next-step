#!/usr/bin/env python3
"""Antigravity lifecycle hook handler (SessionStart, PreInvocation, PreToolUse, PostToolUse, Stop) for next-step plugin."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import next_step_core as core  # noqa: E402

INLINE_TAG_RE = re.compile(r"<!--\s*NEXT_STEP:\s*(\{.*?\})\s*-->", re.DOTALL)


def _resolve_transcript_path(payload: dict[str, Any], state: dict[str, Any]) -> Path | None:
    raw_tr = str(payload.get("transcriptPath") or state.get("transcriptPath") or "").strip()
    if raw_tr:
        p = Path(raw_tr).expanduser()
        if p.is_file():
            return p
    cid = str(payload.get("conversationId") or state.get("conversationId") or "").strip()
    if cid:
        for app_dir in ("antigravity", "antigravity-cli", "antigravity-ide"):
            cand = (
                Path.home()
                / ".gemini"
                / app_dir
                / "brain"
                / cid
                / ".system_generated"
                / "logs"
                / "transcript.jsonl"
            )
            if cand.is_file():
                return cand
    return None


def _load_transcript_steps(payload: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    tr_path = _resolve_transcript_path(payload, state)
    if tr_path is None:
        return []
    steps: list[dict[str, Any]] = []
    try:
        for line in tr_path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    steps.append(obj)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return steps


def _latest_user_step_index(steps: list[dict[str, Any]]) -> int:
    for i in range(len(steps) - 1, -1, -1):
        st = steps[i]
        if st.get("type") == "USER_INPUT" and st.get("source") == "USER_EXPLICIT":
            try:
                return int(st.get("step_index", i))
            except (ValueError, TypeError):
                return i
    return -1


def _extract_inline_declaration_from_transcript(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Fallback parser if the agent wrote <!-- NEXT_STEP: {...} --> in its final response."""
    last_user_pos = -1
    for i, st in enumerate(steps):
        if st.get("type") == "USER_INPUT" and st.get("source") == "USER_EXPLICIT":
            last_user_pos = i
    turn_steps = steps[last_user_pos + 1 :] if last_user_pos >= 0 else steps
    for st in reversed(turn_steps):
        if st.get("type") != "PLANNER_RESPONSE":
            continue
        content = str(st.get("content") or "")
        matches = INLINE_TAG_RE.findall(content)
        for raw_json in reversed(matches):
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


def _match_mcp_tool_call(tname: str, args: dict[str, Any], target_tool: str) -> tuple[bool, bool, dict[str, Any]]:
    """Return (matched, is_via_call_mcp_tool, effective_tool_args) for direct or wrapped MCP calls."""
    low = tname.lower()
    suffix = f"_{target_tool}"
    if low == target_tool or low.endswith(suffix):
        return True, False, args
    if low == "call_mcp_tool":
        inner_name = str(core.clean_arg(args.get("ToolName") or args.get("tool_name") or "")).strip().lower()
        if inner_name == target_tool or inner_name.endswith(suffix):
            inner_args = core.clean_arg(args.get("Arguments") or args.get("arguments") or {})
            if isinstance(inner_args, dict):
                return True, True, inner_args
            return True, True, {}
    return False, False, {}


def _is_next_step_publish_call(tname: str, args: dict[str, Any]) -> tuple[bool, bool, dict[str, Any]]:
    """Return (is_publish, is_via_call_mcp_tool, effective_publish_args)."""
    return _match_mcp_tool_call(tname, args, "next_step_publish")


def _is_next_step_side_panel_call(tname: str, args: dict[str, Any]) -> tuple[bool, bool, dict[str, Any]]:
    """Return (is_side_panel, is_via_call_mcp_tool, effective_panel_args)."""
    return _match_mcp_tool_call(tname, args, "next_step_side_panel")


def _summarize_tool_call(tname: str, args: dict[str, Any]) -> str:
    low = tname.lower()
    if low == "run_command":
        summary = core.clean_arg(args.get("toolSummary") or "")
        cmd = core.clean_arg(args.get("CommandLine") or args.get("command") or "")
        if isinstance(summary, str) and summary.strip():
            return f"{summary.strip()} ({str(cmd).strip()[:60]})" if cmd else summary.strip()
        return str(cmd).strip()[:80]
    for key in ("Path", "TargetFile", "file_path"):
        if key in args:
            val = core.clean_arg(args.get(key))
            if isinstance(val, str) and val.strip():
                return Path(val.strip()).name
    if low == "invoke_subagent":
        role = core.clean_arg(args.get("Role") or "")
        t_type = core.clean_arg(args.get("TypeName") or "")
        return f"{role} ({t_type})".strip()
    return ""


def _maybe_auto_start_floating_hud(reason: str) -> None:
    try:
        import next_step_floating  # local lazy import to keep PreToolUse/PostToolUse/Stop zero-overhead

        next_step_floating.ensure_floating_hud_auto_started(
            data_dir=core.DATA_DIR,
            clear_dismiss=False,
            reason=reason,
        )
    except Exception as exc:
        core.append_error_log(f"hook_auto_start_floating_hud:{reason}", exc)


def handle_session_start(payload: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("conversationId") or "default").strip() or "default"
    state = core.load_session(cid)
    core.update_common_metadata(state, payload)
    core.save_session(cid, state)
    _maybe_auto_start_floating_hud("session_start")
    return {}


def handle_pre_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("conversationId") or "default").strip() or "default"
    state = core.load_session(cid)
    prev_turn_input = str(state.get("turnUserInput") or state.get("lastUserInput") or "")
    prev_user_step = int(state.get("userStepIndex", -1))
    was_turn_active = bool(state.get("turnActive", False))
    last_inv_num = int(state.get("lastInvocationNum", -1))
    last_end_inv = int(state.get("lastTurnEndInvocationNum", -1))

    core.update_common_metadata(state, payload)
    raw_inv = payload.get("invocationNum")
    inv_num = int(raw_inv) if raw_inv is not None else 0
    steps = _load_transcript_steps(payload, state)
    cur_user_step = _latest_user_step_index(steps)

    is_new_user_turn = False
    if cur_user_step >= 0 and prev_user_step >= 0:
        if cur_user_step != prev_user_step:
            is_new_user_turn = True
        elif state.get("lastUserInput") and prev_turn_input and state.get("lastUserInput") != prev_turn_input:
            is_new_user_turn = True
        elif not was_turn_active and (last_end_inv < 0 or inv_num <= last_end_inv):
            is_new_user_turn = True
    else:
        if not was_turn_active:
            if last_end_inv < 0 or inv_num <= last_end_inv:
                is_new_user_turn = True
            elif state.get("lastUserInput") and prev_turn_input and state.get("lastUserInput") != prev_turn_input:
                is_new_user_turn = True
        elif last_inv_num >= 0 and inv_num <= last_inv_num:
            is_new_user_turn = True
        elif state.get("lastUserInput") and prev_turn_input and state.get("lastUserInput") != prev_turn_input:
            is_new_user_turn = True

    if cur_user_step >= 0:
        state["userStepIndex"] = cur_user_step
    state["turnActive"] = True
    state["lastInvocationNum"] = inv_num

    if is_new_user_turn:
        state["turnStartedAt"] = time.time()
        state["turnUserInput"] = str(state.get("lastUserInput") or "")
        state["pendingDeclaration"] = None
        state["pendingPlanReview"] = None
        state["planArtifact"] = None
        state["options"] = []
        state["suggestedPrompt"] = None
        state["detail"] = None
        state["stale"] = False
        state["staleReason"] = None
        core.clear_pending_buffer(cid)

    state["activeTool"] = None
    state["terminationReason"] = None

    if not is_new_user_turn and isinstance(state.get("pendingPlanReview"), dict):
        core.promote_plan_review_to_active(state, state["pendingPlanReview"])
    else:
        state["lifecycle"] = "WORKING"
        state["sourceType"] = "LIFECYCLE"
        state["confidence"] = "DETERMINISTIC"
        state["waitingOnUser"] = False
        state["actor"] = "agent"
        state["nextAction"] = "Agent 正在思考与规划…"

    core.save_session(cid, state)
    _maybe_auto_start_floating_hud("pre_invocation")
    return {}


def handle_pre_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("conversationId") or "default").strip() or "default"
    state = core.load_session(cid)
    core.update_common_metadata(state, payload)

    tc = payload.get("toolCall") or {}
    tname = str(tc.get("name") or "").strip()
    raw_args = tc.get("args") or {}
    args = raw_args if isinstance(raw_args, dict) else {}
    step_idx = int(payload.get("stepIdx") or state.get("stepIdx") or 0)
    state["stepIdx"] = step_idx

    low = tname.lower()

    if low == "ask_question":
        questions = core.clean_arg(args.get("questions"))
        q_text = "请回答 Agent 提出的问题"
        q_opts: list[str] = []
        if isinstance(questions, list) and questions:
            first_q = core.clean_arg(questions[0])
            if isinstance(first_q, dict):
                raw_q = str(core.clean_arg(first_q.get("question") or "")).strip()
                if raw_q:
                    q_text = raw_q
                raw_opts = core.clean_arg(first_q.get("options") or [])
                if isinstance(raw_opts, list):
                    q_opts = [str(core.clean_arg(o)) for o in raw_opts if o is not None]
            if len(questions) > 1:
                q_text = f"{q_text}（共 {len(questions)} 个问题）"

        state["lifecycle"] = "WAITING_USER_DECISION"
        state["sourceType"] = "APPROVAL_REQUIRED"
        state["confidence"] = "DETERMINISTIC"
        state["waitingOnUser"] = True
        state["actor"] = "user"
        state["nextAction"] = f"在聊天框回答选择题：{q_text}"
        state["detail"] = None
        state["options"] = q_opts
        state["suggestedPrompt"] = None
        state["activeTool"] = None
        state["stale"] = False
        state["staleReason"] = None
        core.save_session(cid, state)
        return {"decision": "allow"}

    if low == "ask_permission":
        action = str(core.clean_arg(args.get("Action") or args.get("action") or "权限")).strip()
        target = str(core.clean_arg(args.get("Target") or args.get("target") or "")).strip()
        reason = str(core.clean_arg(args.get("Reason") or args.get("reason") or "")).strip()
        target_part = f"({target})" if target else ""

        state["lifecycle"] = "WAITING_USER_APPROVAL"
        state["sourceType"] = "APPROVAL_REQUIRED"
        state["confidence"] = "DETERMINISTIC"
        state["waitingOnUser"] = True
        state["actor"] = "user"
        state["nextAction"] = f"审批权限请求：{action}{target_part}"
        state["detail"] = reason or None
        state["options"] = []
        state["suggestedPrompt"] = None
        state["activeTool"] = None
        state["stale"] = False
        state["staleReason"] = None
        core.save_session(cid, state)
        return {"decision": "allow"}

    is_pub, is_via_call_mcp, pub_args = _is_next_step_publish_call(tname, args)
    if is_pub:
        core.stage_declaration(
            pub_args,
            cid=cid,
            workspace_paths=state.get("workspacePaths") or [],
            artifact_dir=state.get("artifactDirectoryPath") or None,
            step_idx=step_idx,
            write_pending_buffer=True,
        )
        sys_inject = {
            "_sys_conversation_id": cid,
            "_sys_step_idx": step_idx,
            "_sys_artifact_dir": state.get("artifactDirectoryPath") or "",
            "_sys_workspace_paths": state.get("workspacePaths") or [],
        }
        if is_via_call_mcp:
            return {
                "decision": "allow",
                "overwrite": {
                    "Arguments": dict(pub_args, **sys_inject),
                },
            }
        return {
            "decision": "allow",
            "overwrite": sys_inject,
        }

    is_panel, is_panel_via_call_mcp, panel_args = _is_next_step_side_panel_call(tname, args)
    if is_panel:
        sys_inject_panel = {
            "_sys_conversation_id": cid,
            "_sys_workspace_paths": state.get("workspacePaths") or [],
        }
        if is_panel_via_call_mcp:
            return {
                "decision": "allow",
                "overwrite": {
                    "Arguments": dict(panel_args, **sys_inject_panel),
                },
            }
        return {
            "decision": "allow",
            "overwrite": sys_inject_panel,
        }

    tool_summary = _summarize_tool_call(tname, args)
    now = time.time()
    state["activeTool"] = {
        "id": str(tc.get("id") or ""),
        "name": tname,
        "startedAt": now,
        "summary": tool_summary,
        "mayWaitApproval": low in core.APPROVAL_CANDIDATE_TOOLS,
    }
    state["lifecycle"] = "TOOL_RUNNING"
    state["sourceType"] = "LIFECYCLE"
    state["confidence"] = "DETERMINISTIC"
    state["waitingOnUser"] = False
    state["actor"] = "agent"
    state["nextAction"] = f"正在执行工具：{tname}" + (f" · {tool_summary}" if tool_summary else "")
    state["detail"] = None
    core.save_session(cid, state, now=now)
    return {"decision": "allow"}


def handle_post_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("conversationId") or "default").strip() or "default"
    state = core.load_session(cid)
    core.update_common_metadata(state, payload)

    tc = payload.get("toolCall") or {}
    tname = str(tc.get("name") or "").strip()
    raw_args = tc.get("args") or {}
    args = raw_args if isinstance(raw_args, dict) else {}
    step_idx = int(payload.get("stepIdx") or state.get("stepIdx") or 0)
    error = str(payload.get("error") or "").strip()

    state["stepIdx"] = step_idx
    state["activeTool"] = None
    low = tname.lower()

    if low in ("ask_question", "ask_permission"):
        state["lifecycle"] = "WORKING"
        state["sourceType"] = "LIFECYCLE"
        state["confidence"] = "DETERMINISTIC"
        state["waitingOnUser"] = False
        state["actor"] = "agent"
        state["nextAction"] = "已收到用户决定，Agent 继续执行中…"
        state["options"] = []
        state["detail"] = None
        core.save_session(cid, state)
        return {}

    is_pub, _, pub_args = _is_next_step_publish_call(tname, args)
    if is_pub:
        core.clear_pending_buffer(cid)
        if error:
            pending = state.get("pendingDeclaration")
            if isinstance(pending, dict) and int(pending.get("stepIdx") or 0) == step_idx:
                state["pendingDeclaration"] = None
            core.save_session(cid, state)
            return {}

        decl, _ = core.stage_declaration(
            pub_args,
            cid=cid,
            workspace_paths=state.get("workspacePaths") or [],
            artifact_dir=state.get("artifactDirectoryPath") or None,
            step_idx=step_idx,
            write_pending_buffer=False,
        )
        if decl is None:
            claimed = core.claim_latest_pending_declaration(
                cid,
                min_created_at=float(state.get("turnStartedAt") or 0.0),
            )
            if claimed is not None:
                state["pendingDeclaration"] = claimed
                state["planArtifact"] = claimed.get("planArtifact")
                core.save_session(cid, state)
        return {}

    is_panel, _, _ = _is_next_step_side_panel_call(tname, args)
    if is_panel:
        return {}

    if low in core.MUTATING_FILE_TOOLS and not error:
        target_raw = None
        for key in ("Path", "TargetFile", "file_path"):
            if key in args:
                val = core.clean_arg(args.get(key))
                if isinstance(val, str) and val.strip():
                    target_raw = val.strip()
                    break

        resolved_target = core.resolve_plan_path(
            target_raw,
            workspace_paths=state.get("workspacePaths") or [],
            artifact_dir=state.get("artifactDirectoryPath") or None,
        )

        meta = core.clean_arg(args.get("ArtifactMetadata"))
        if isinstance(meta, dict) and meta.get("RequestFeedback") is True and target_raw:
            summary = str(core.clean_arg(meta.get("Summary") or "")).strip() or None
            plan_entry = core.build_plan_artifact_entry(
                target_raw,
                workspace_paths=state.get("workspacePaths") or [],
                artifact_dir=state.get("artifactDirectoryPath") or None,
                summary=summary,
                request_feedback=True,
            )
            fname = plan_entry["name"] if plan_entry else Path(target_raw).name
            plan_review = {
                "nextAction": f"审阅工件 {fname} 并点击 Proceed 或提交评论",
                "detail": summary,
                "planArtifact": plan_entry,
                "stepIdx": step_idx,
            }
            state["pendingPlanReview"] = plan_review
            core.promote_plan_review_to_active(state, plan_review)
            core.save_session(cid, state)
            return {}

        pending_pr = state.get("pendingPlanReview")
        if isinstance(meta, dict) and meta.get("RequestFeedback") is False and resolved_target is not None:
            if isinstance(pending_pr, dict) and isinstance(pending_pr.get("planArtifact"), dict):
                pr_path = pending_pr["planArtifact"].get("path")
                if pr_path and str(Path(str(pr_path)).resolve()) == str(resolved_target.resolve()):
                    state["pendingPlanReview"] = None
                    pending_pr = None

        # Check if an ordinary file mutation modified or deleted a linked plan artifact (without re-requesting feedback)
        existing_plan = state.get("planArtifact")
        pr_plan = pending_pr.get("planArtifact") if isinstance(pending_pr, dict) else None
        pending = state.get("pendingDeclaration")
        for tracked_plan in (existing_plan, pr_plan):
            if resolved_target is None or not isinstance(tracked_plan, dict) or not tracked_plan.get("path"):
                continue
            try:
                same_file = Path(str(tracked_plan["path"])).resolve() == resolved_target.resolve()
            except OSError:
                same_file = str(tracked_plan["path"]) == str(resolved_target)
            if not same_file:
                continue
            new_sha, new_mtime = core.compute_file_sha256(resolved_target)
            old_sha = tracked_plan.get("sha256")
            if old_sha and new_sha != old_sha:
                tracked_plan["stale"] = True
                tracked_plan["staleReason"] = "关联计划文件已改写或移除"
                tracked_plan["currentSha256"] = new_sha
                tracked_plan["currentMtimeNs"] = new_mtime
                if isinstance(pending, dict):
                    p_plan = pending.get("planArtifact")
                    if isinstance(p_plan, dict) and p_plan.get("sha256") == old_sha:
                        p_plan["stale"] = True
                        p_plan["staleReason"] = "关联计划文件已改写或移除"
                if (
                    state.get("sourceType") in ("AGENT_DECLARED", "APPROVAL_REQUIRED")
                    or isinstance(pending_pr, dict)
                    or isinstance(pending, dict)
                ):
                    state["stale"] = True
                    state["staleReason"] = "关联计划文件已改写或移除，当前下一步提示可能已过期"

    if isinstance(state.get("pendingPlanReview"), dict):
        core.promote_plan_review_to_active(state, state["pendingPlanReview"])
    elif state.get("lifecycle") == "TOOL_RUNNING":
        state["lifecycle"] = "WORKING"
        state["sourceType"] = "LIFECYCLE"
        state["waitingOnUser"] = False
        state["actor"] = "agent"
        state["nextAction"] = "工具执行完毕，Agent 正在整理结果…"
    core.save_session(cid, state)
    return {}


def handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("conversationId") or "default").strip() or "default"
    state = core.load_session(cid)
    core.update_common_metadata(state, payload)

    term_reason = str(payload.get("terminationReason") or "").strip()
    error = str(payload.get("error") or "").strip()
    fully_idle_raw = payload.get("fullyIdle")
    fully_idle = True if fully_idle_raw is None else bool(fully_idle_raw)

    state["activeTool"] = None
    if fully_idle:
        state["lastTurnEndInvocationNum"] = int(state.get("lastInvocationNum", -1))
        state["turnActive"] = False
        state["lastInvocationNum"] = -1
    state["fullyIdle"] = fully_idle
    state["terminationReason"] = term_reason or "NO_TOOL_CALL"

    low_reason = term_reason.lower()

    # 1. Canceled turn
    if term_reason == "USER_CANCELED" or "cancel" in low_reason:
        core.clear_pending_buffer(cid)
        state["pendingDeclaration"] = None
        state["pendingPlanReview"] = None
        state["lifecycle"] = "CANCELED"
        state["sourceType"] = "LIFECYCLE"
        state["confidence"] = "DETERMINISTIC"
        state["waitingOnUser"] = True
        state["actor"] = "user"
        state["nextAction"] = "当前回合已由用户手动中止，等待你输入新指令"
        state["detail"] = None
        state["options"] = []
        state["suggestedPrompt"] = None
        state["stale"] = False
        state["staleReason"] = None
        core.save_session(cid, state)
        return {}

    # 2. Errored turn
    if term_reason in ("ERROR", "error") or "error" in low_reason or bool(error):
        core.clear_pending_buffer(cid)
        state["pendingDeclaration"] = None
        state["pendingPlanReview"] = None
        state["lifecycle"] = "ERROR"
        state["sourceType"] = "APPROVAL_REQUIRED"
        state["confidence"] = "DETERMINISTIC"
        state["waitingOnUser"] = True
        state["actor"] = "user"
        err_desc = error or term_reason or "未知错误"
        state["nextAction"] = f"执行异常中止（{err_desc}），请决定重试或调整指令"
        state["detail"] = error or None
        state["options"] = []
        state["suggestedPrompt"] = None
        state["stale"] = False
        state["staleReason"] = None
        core.save_session(cid, state)
        return {}

    # 3. Deterministic approval state takes top precedence (ask_question, ask_permission, RequestFeedback plan)
    if state.get("sourceType") == "APPROVAL_REQUIRED" and state.get("lifecycle") in (
        "WAITING_USER_DECISION",
        "WAITING_USER_APPROVAL",
        "WAITING_PLAN_REVIEW",
    ):
        evaluated = core.evaluate_staleness(state)
        state["stale"] = bool(evaluated.get("stale", False))
        state["staleReason"] = evaluated.get("staleReason")
        if isinstance(evaluated.get("planArtifact"), dict):
            state["planArtifact"] = evaluated["planArtifact"]
        core.save_session(cid, state)
        return {}

    if isinstance(state.get("pendingPlanReview"), dict):
        core.promote_plan_review_to_active(state, state["pendingPlanReview"])
        core.save_session(cid, state)
        return {}

    # 4. Staged pendingDeclaration from next_step_publish in this turn
    pending = state.get("pendingDeclaration")
    if not isinstance(pending, dict):
        pending = core.claim_latest_pending_declaration(
            cid,
            max_age_sec=15.0,
            min_created_at=float(state.get("turnStartedAt") or 0.0),
        )
    if isinstance(pending, dict):
        core.promote_declaration_to_active(state, pending, fully_idle=fully_idle)
        core.clear_pending_buffer(cid)
        core.save_session(cid, state)
        return {}

    # 5. Inline transcript fallback (<!-- NEXT_STEP: {...} -->)
    steps = _load_transcript_steps(payload, state)
    inline_raw = _extract_inline_declaration_from_transcript(steps)
    if isinstance(inline_raw, dict):
        inline_decl = core.normalize_declaration(
            inline_raw,
            workspace_paths=state.get("workspacePaths") or [],
            artifact_dir=state.get("artifactDirectoryPath") or None,
            step_idx=int(state.get("stepIdx") or 0),
        )
        if inline_decl is not None:
            core.promote_declaration_to_active(state, inline_decl, fully_idle=fully_idle)
            core.save_session(cid, state)
            return {}

    # 6. Background tasks still running
    if not fully_idle:
        state["lifecycle"] = "BACKGROUND_RUNNING"
        state["sourceType"] = "LIFECYCLE"
        state["confidence"] = "DETERMINISTIC"
        state["waitingOnUser"] = False
        state["actor"] = "agent_background"
        state["nextAction"] = "主回合已暂停，正在等待后台任务或子 Agent 完成"
        state["detail"] = None
        state["options"] = []
        state["suggestedPrompt"] = None
        state["stale"] = False
        state["staleReason"] = None
        core.save_session(cid, state)
        return {}

    # 7. Deterministic empty state (no credible next step declared)
    state["lifecycle"] = "IDLE"
    state["sourceType"] = "NONE"
    state["confidence"] = "DETERMINISTIC"
    state["waitingOnUser"] = False
    state["actor"] = "none"
    state["nextAction"] = None
    state["detail"] = None
    state["options"] = []
    state["suggestedPrompt"] = None
    state["stale"] = False
    state["staleReason"] = None
    core.save_session(cid, state)
    return {}


def dispatch_event(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    ev = event_name.strip().lower()
    if ev in ("session-start", "sessionstart"):
        return handle_session_start(payload)
    if ev in ("pre-invocation", "preinvocation"):
        return handle_pre_invocation(payload)
    if ev in ("pre-tool-use", "pretooluse"):
        return handle_pre_tool_use(payload)
    if ev in ("post-tool-use", "posttooluse"):
        return handle_post_tool_use(payload)
    if ev in ("stop",):
        return handle_stop(payload)
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Next Step lifecycle hook handler")
    parser.add_argument(
        "--event",
        required=True,
        choices=["session-start", "pre-invocation", "pre-tool-use", "post-tool-use", "stop"],
        help="Lifecycle hook event type",
    )
    args = parser.parse_args()

    raw_stdin = sys.stdin.read().strip()
    payload: dict[str, Any] = {}
    if raw_stdin:
        try:
            parsed = json.loads(raw_stdin)
            if isinstance(parsed, dict):
                payload = parsed
        except Exception as exc:
            core.append_error_log(f"hook_stdin_parse:{args.event}", exc)

    try:
        resp = dispatch_event(args.event, payload)
    except Exception as exc:
        core.append_error_log(f"hook_dispatch:{args.event}", exc)
        resp = {"decision": "allow"} if args.event == "pre-tool-use" else {}

    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
