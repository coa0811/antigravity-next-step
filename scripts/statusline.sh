#!/bin/sh
# Optional Antigravity CLI (/statusline) companion script that merges CLI live state with Next Step HUD session state.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/run_python.sh" -c '
import json, os, sys
from pathlib import Path

script_dir = Path(sys.argv[1]).resolve().parent
sys.path.insert(0, str(script_dir))
import next_step_core as core

raw = sys.stdin.read().strip()
cli = {}
if raw:
    try:
        cli = json.loads(raw)
    except Exception:
        cli = {}

cid = str(cli.get("conversation_id") or cli.get("session_id") or "").strip()
agent_state = str(cli.get("agent_state") or "idle").strip()
confirm_pending = bool(cli.get("tool_confirmation_pending", False))

if confirm_pending:
    print("🔴 [等我审批] 终端工具确认弹窗待处理 · 由谁做: 用户")
    sys.exit(0)

if cid and core.session_path(cid).is_file():
    st = core.evaluate_staleness(core.load_session(cid))
    badge = core.format_status_badge(st)
    actor = core.format_actor_label(st.get("actor"), bool(st.get("waitingOnUser")))
    action = st.get("nextAction") or "当前无已声明的下一步（等待新指令）"
    print(f"{badge} | 由谁做: {actor} | 下一步: {action}")
else:
    if agent_state in ("working", "tool_use", "initializing"):
        print(f"🟢 [Agent 运行中: {agent_state}] | 由谁做: Agent")
    elif agent_state in ("reviewing", "authenticating"):
        print(f"🔴 [等我处理: {agent_state}] | 由谁做: 用户")
    else:
        print("⚪ [会话空闲] | 当前无已声明的下一步")
' "$0"
