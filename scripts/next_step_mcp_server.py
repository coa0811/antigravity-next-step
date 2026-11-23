#!/usr/bin/env python3
"""Zero-dependency Stdio MCP server for the Antigravity Next Step HUD plugin."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import next_step_core as core  # noqa: E402
import next_step_floating  # noqa: E402
import next_step_hud_server as hud_server  # noqa: E402

SERVER_NAME = "next-step"
SERVER_VERSION = "0.3.0"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-01-26",
}

SIDE_PANEL_URI = "ui://next-step/side-panel.html"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
SIDE_PANEL_HTML_PATH = SCRIPT_DIR.parent / "ui" / "side-panel.html"
MAX_UI_RESOURCE_BYTES = 10 * 1024 * 1024

RESOURCES: list[dict[str, Any]] = [
    {
        "uri": SIDE_PANEL_URI,
        "name": "Next Step HUD Side Panel",
        "description": "Read-only Next Step HUD interactive Side Panel UI (MCP App)",
        "mimeType": MCP_APP_MIME_TYPE,
    }
]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "next_step_publish",
        "description": (
            "Explicitly publish or update the 'Next Step' action hint for the current Antigravity session "
            "(what needs to happen next, who performs it, and whether it is waiting on the user). "
            "Read-only display only: even when `suggested_prompt` is provided, the user decides whether to copy or run it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "next_action": {
                    "type": "string",
                    "description": "Concise 1-sentence description of what should happen next.",
                },
                "actor": {
                    "type": "string",
                    "enum": ["user", "agent", "agent_background"],
                    "default": "user",
                    "description": "Who performs the next action ('user', 'agent', or 'agent_background').",
                },
                "waiting_on_user": {
                    "type": "boolean",
                    "default": True,
                    "description": "True if the session is waiting on the user's decision, confirmation, or next command.",
                },
                "suggested_prompt": {
                    "type": "string",
                    "description": "Optional follow-up command or prompt shown with a read-only Copy button in the HUD.",
                },
                "detail": {
                    "type": "string",
                    "description": "Optional supplementary context, verification target, or constraint note.",
                },
                "plan_path": {
                    "type": "string",
                    "description": "Optional path to the associated plan/task artifact (tracked by SHA-256 to detect plan rewrites).",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Optional conversation UUID (automatically injected/verified by PreToolUse/PostToolUse hooks).",
                },
            },
            "required": ["next_action"],
        },
    },
    {
        "name": "next_step_get",
        "description": (
            "Query the current 'Next Step' status (next action, actor, waiting_on_user, source credibility, "
            "and plan staleness) for a specific conversation or all active conversations, and ensure the local read-only HUD is accessible."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "Optional conversation UUID. If omitted, lists active sessions sorted by waiting_on_user and recency.",
                },
                "workspace_path": {
                    "type": "string",
                    "description": "Optional workspace path filter.",
                },
                "ensure_hud": {
                    "type": "boolean",
                    "default": True,
                    "description": "Start or verify the local 127.0.0.1 SSE HUD server and return its URL.",
                },
                "open_window": {
                    "type": "boolean",
                    "default": False,
                    "description": "Open or surface the macOS native always-on-top non-activating floating HUD window.",
                },
            },
        },
    },
    {
        "name": "next_step_side_panel",
        "title": "Next Step HUD",
        "description": (
            "Open or update the read-only Next Step HUD native Side Panel for Antigravity. "
            "Displays session status, next actor, waiting-on-user state, next action, plan staleness, "
            "and read-only Suggested Prompt copy controls. All arguments are optional; manual launches pass {}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "Optional conversation UUID (automatically injected/verified by PreToolUse hook when called in chat).",
                },
                "workspace_path": {
                    "type": "string",
                    "description": "Optional workspace path filter.",
                },
            },
        },
        "_meta": {
            "ui": {
                "resourceUri": SIDE_PANEL_URI,
                "visibility": ["model", "app"],
            },
            "com.google.antigravity/ui": {
                "entrypoints": [
                    {"type": "aux_pane", "title": "Next Step HUD"},
                ]
            },
        },
    },
    {
        "name": "next_step_panel_state",
        "title": "Next Step Panel State (App Only)",
        "description": (
            "Read-only structured session status query for the Next Step HUD MCP App UI. "
            "App-only tool; never triggers inference or mutates session state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "Optional conversation UUID selected explicitly by the user or bound by the panel.",
                },
                "workspace_path": {
                    "type": "string",
                    "description": "Optional workspace path filter.",
                },
            },
        },
        "_meta": {
            "ui": {
                "visibility": ["app"],
            }
        },
    },
]


def _load_side_panel_html() -> str:
    override = os.environ.get("NEXT_STEP_SIDE_PANEL_HTML", "").strip()
    path = Path(override).expanduser() if override else SIDE_PANEL_HTML_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Side panel UI bundle not found: {path}")
    html_text = path.read_text(encoding="utf-8")
    if not html_text.strip():
        raise ValueError(f"Side panel UI bundle is empty: {path}")
    if len(html_text.encode("utf-8")) > MAX_UI_RESOURCE_BYTES:
        raise ValueError(f"Side panel UI bundle exceeds {MAX_UI_RESOURCE_BYTES} bytes: {path}")
    return html_text


def _read_message() -> tuple[dict[str, Any] | None, str]:
    while True:
        raw_line = sys.stdin.buffer.readline()
        if not raw_line:
            return None, "eof"
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
            while True:
                hdr = sys.stdin.buffer.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
                hdr_str = hdr.decode("utf-8", errors="replace").strip()
                if hdr_str.lower().startswith("content-length:"):
                    length = int(hdr_str.split(":", 1)[1].strip())
            body = sys.stdin.buffer.read(length)
            return json.loads(body.decode("utf-8", errors="replace")), "lsp"
        return json.loads(line), "jsonl"


def _write_message(payload: dict[str, Any], framing: str) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if framing == "lsp":
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(header + encoded)
    else:
        sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def _handle_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "next_step_publish":
        decl, state = core.stage_declaration(args, write_pending_buffer=True)
        if decl is None:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Next Step error: `next_action` is required and cannot be empty.",
                    }
                ],
                "isError": True,
            }

        ensure_hud = args.get("_ensure_hud", True)
        hud_info = hud_server.ensure_hud_server_running() if ensure_hud else {"url": f"file://{core.SNAPSHOT_HTML_PATH}"}
        cid = (
            str(state.get("conversationId"))
            if isinstance(state, dict) and state.get("conversationId")
            else str(core.clean_arg(args.get("_sys_conversation_id") or args.get("conversation_id") or "pending-hook-claim"))
        )
        actor_label = core.format_actor_label(decl.get("actor"), bool(decl.get("waiting_on_user")))
        wait_label = "🔴 等我处理/确认" if decl.get("waiting_on_user") else "🟢 Agent 继续/后台执行"
        plan = decl.get("planArtifact")
        plan_line = f"\n- **关联计划工件**：`{plan['name']}` (SHA-256 已记录)" if isinstance(plan, dict) else ""
        prompt_line = (
            f"\n- **建议指令（只读复制）**：`{decl['suggested_prompt']}`"
            if decl.get("suggested_prompt")
            else ""
        )

        text = (
            f"**已发布下一步指令**（会话 `{cid[:8]}`）\n"
            f"- **是否在等我**：`{wait_label}`\n"
            f"- **由谁做**：`{actor_label}`\n"
            f"- **接下来要做什么**：**{decl['next_action']}**"
            f"{plan_line}{prompt_line}\n"
            f"- **HUD 面板**：{hud_info.get('url')} （静态快照：`{core.SNAPSHOT_HTML_PATH}`）"
        )
        return {"content": [{"type": "text", "text": text}], "isError": False}

    if name == "next_step_get":
        cid = str(core.clean_arg(args.get("conversation_id") or args.get("_sys_conversation_id") or "")).strip() or None
        ws_path = str(core.clean_arg(args.get("workspace_path") or "")).strip() or None
        raw_ensure = core.clean_arg(args.get("ensure_hud"))
        ensure_hud = True if raw_ensure is None else bool(raw_ensure)
        raw_open_win = core.clean_arg(args.get("open_window"))
        open_window = bool(raw_open_win) if raw_open_win is not None else False

        hud_info = (
            hud_server.ensure_hud_server_running()
            if ensure_hud
            else {"ok": False, "url": f"file://{core.SNAPSHOT_HTML_PATH}"}
        )

        floating_line = ""
        if open_window and ensure_hud:
            win_res = next_step_floating.launch_floating_hud(
                url=str(hud_info.get("url") or "") if hud_info.get("ok") else None,
                compact=True,
            )
            if win_res.get("ok"):
                state_tag = "已置顶唤醒" if win_res.get("alreadyRunning") else "已打开悬浮窗"
                floating_line = f"**原生悬浮窗**：`{state_tag}` (PID `{win_res.get('pid')}`, {win_res.get('url')})"
            else:
                floating_line = (
                    f"**原生悬浮窗启动失败**：`{win_res.get('error', 'unknown')}`"
                    f"（请回退访问 {hud_info.get('url')}）"
                )

        header_lines = [
            f"**Next Step HUD 地址**：{hud_info.get('url')}",
            f"**精简悬浮视图**：{str(hud_info.get('url') or '').rstrip('/')}/?mode=compact"
            if str(hud_info.get("url") or "").startswith("http")
            else f"**静态 HTML 快照**：`{core.SNAPSHOT_HTML_PATH}`",
            f"**静态 HTML 快照**：`{core.SNAPSHOT_HTML_PATH}`",
            f"**Markdown 快照**：`{core.LATEST_MD_PATH}`",
        ]
        if floating_line:
            header_lines.append(floating_line)
        header_lines.append("")

        if cid:
            sp = core.session_path(cid)
            if not sp.is_file():
                # Fallback prefix match by shortId
                matches = [
                    s
                    for s in core.list_all_sessions(workspace_filter=ws_path)
                    if str(s.get("conversationId") or "").startswith(cid)
                ]
                if matches:
                    body = "\n\n".join(core.format_session_markdown(m) for m in matches)
                else:
                    body = f"未找到会话 `{cid}` 的状态记录（该会话尚未触发 Hook 或发布下一步）。"
            else:
                state = core.load_session(cid)
                body = core.format_session_markdown(state)
        else:
            sessions = core.list_all_sessions(workspace_filter=ws_path)
            if not sessions:
                body = "当前暂无已记录的会话状态。"
            else:
                body = "\n\n".join(core.format_session_markdown(s) for s in sessions[:10])

        return {
            "content": [{"type": "text", "text": "\n".join(header_lines) + body}],
            "isError": False,
        }

    if name in ("next_step_side_panel", "next_step_panel_state"):
        sys_cid = str(core.clean_arg(args.get("_sys_conversation_id") or "")).strip() or None
        explicit_cid = str(core.clean_arg(args.get("conversation_id") or "")).strip() or None
        ws_path = str(core.clean_arg(args.get("workspace_path") or "")).strip() or None

        if sys_cid:
            target_cid = sys_cid
            binding_mode = "hook_bound"
        elif explicit_cid:
            target_cid = explicit_cid
            binding_mode = "explicit"
        else:
            target_cid = None
            binding_mode = "unbound"

        payload = core.build_side_panel_payload(
            bound_cid=target_cid,
            binding_mode=binding_mode,
            workspace_filter=ws_path,
        )
        return {
            "content": [{"type": "text", "text": core.format_side_panel_text(payload)}],
            "structuredContent": payload,
            "isError": False,
        }

    return {
        "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
        "isError": True,
    }


def _record_host_capabilities(params: dict[str, Any], negotiated_proto: str) -> None:
    try:
        caps = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
        extensions = caps.get("extensions") if isinstance(caps.get("extensions"), dict) else {}
        record = {
            "recordedAtIso": core.iso_now(),
            "protocolVersion": negotiated_proto,
            "clientProtocolVersion": params.get("protocolVersion"),
            "clientInfo": params.get("clientInfo"),
            "capabilities": caps,
            "extensions": extensions,
        }
        core.atomic_write_json(core.DATA_DIR / "mcp_host_capabilities.json", record)
    except Exception as exc:
        core.append_error_log("record_host_capabilities", exc)


def _dispatch(req: dict[str, Any]) -> dict[str, Any] | None:
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    if req_id is None:
        return None

    if method == "initialize":
        client_ver = str(params.get("protocolVersion") or "")
        proto = client_ver if client_ver in SUPPORTED_PROTOCOL_VERSIONS else "2024-11-05"
        _record_host_capabilities(params, proto)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": RESOURCES}}

    if method == "resources/read":
        uri = str(params.get("uri") or "").strip() if isinstance(params, dict) else ""
        if uri != SIDE_PANEL_URI:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32002, "message": f"Resource not found: {uri}"},
            }
        try:
            html_text = _load_side_panel_html()
        except Exception as exc:
            core.append_error_log("resources/read:side_panel", exc)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Failed to read UI resource: {exc}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "contents": [
                    {
                        "uri": SIDE_PANEL_URI,
                        "mimeType": MCP_APP_MIME_TYPE,
                        "text": html_text,
                        "_meta": {
                            "ui": {
                                "prefersBorder": False,
                                "permissions": {"clipboardWrite": {}},
                            }
                        },
                    }
                ]
            },
        }

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            result = _handle_tool_call(tool_name, arguments)
        except Exception as exc:
            core.append_error_log(f"tools/call:{tool_name}", exc)
            result = {
                "content": [{"type": "text", "text": f"Next Step MCP error: {exc}"}],
                "isError": True,
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    core.ensure_data_dirs()
    try:
        next_step_floating.ensure_floating_hud_auto_started(
            data_dir=core.DATA_DIR,
            clear_dismiss=True,
            reason="mcp_start",
        )
    except Exception as exc:
        core.append_error_log("mcp_auto_start_floating_hud", exc)
    while True:
        try:
            req, framing = _read_message()
        except json.JSONDecodeError as exc:
            core.append_error_log("mcp_json_decode", exc)
            _write_message(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}},
                "jsonl",
            )
            continue
        except Exception as exc:
            core.append_error_log("mcp_read_loop", exc)
            break

        if req is None:
            break
        if not isinstance(req, dict):
            continue

        try:
            resp = _dispatch(req)
            if resp is not None:
                _write_message(resp, framing)
        except Exception as exc:
            core.append_error_log("mcp_dispatch", exc)
            if req.get("id") is not None:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": req.get("id"),
                        "error": {"code": -32603, "message": f"Internal error: {exc}"},
                    },
                    framing,
                )


if __name__ == "__main__":
    main()
