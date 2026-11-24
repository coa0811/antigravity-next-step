---
name: next-step
description: >-
  Inspect, publish, and open the read-only Next Step HUD for Antigravity sessions.
  Use when the user asks what the next step is, who acts next, whether a session is
  waiting on them, or wants to open the Next Step HUD dashboard.
---

# Next Step HUD Skill

Use the `next-step` MCP tools to inspect, open, or publish the current session's next action status:

1. **Open the MCP Apps Side Panel (`next_step_side_panel`, primary in Antigravity App)**:
   - Call `next_step_side_panel` (`{}` — `PreToolUse` binds the active `conversationId` automatically) to open/refresh the native Antigravity Side Panel (`aux_pane`, `ui://next-step/side-panel.html`) beside the conversation. Non-UI clients receive the same clean Markdown summary as plain text.
2. **Inspect status as Markdown (`next_step_get`)**:
   - Call `next_step_get` to query active sessions sorted by `waitingOnUser` and recency (or inspect a specific `conversation_id` / `workspace_path`) and check plan artifact SHA-256 staleness in Markdown.
3. **Publish an explicit next step (`next_step_publish`)**:
   - Call `next_step_publish` with `next_action`, `actor` (`"user" | "agent" | "agent_background"`), `waiting_on_user` (`true` or `false`), and optional `suggested_prompt`, `detail`, `plan_path`, and `conversation_name`.
4. **Read-only guarantee**:
   - Present the Side Panel, HUD link, status summary, and any `suggested_prompt` as read-only information. Never auto-execute or auto-submit `suggested_prompt` without the user's explicit command.
