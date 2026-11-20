---
name: next-step
description: >-
  Inspect, publish, and open the read-only Next Step HUD for Antigravity sessions.
  Use when the user asks what the next step is, who acts next, whether a session is
  waiting on them, or wants to open the Next Step HUD dashboard.
---

# Next Step HUD Skill

Use the `next-step` MCP tools to inspect or publish the current session's next action status:

1. **Inspect status and open HUD (`next_step_get`)**:
   - Call `next_step_get` (with `ensure_hud: true`, and `open_window: true` on macOS when the user runs `/next-step` or asks to open/float the HUD window) to query active sessions sorted by `waitingOnUser` and recency, evaluate plan artifact SHA-256 staleness, obtain the local read-only SSE HUD URL (`http://127.0.0.1:<port>/`), and open/surface the native always-on-top non-activating floating HUD panel.
2. **Publish an explicit next step (`next_step_publish`)**:
   - Call `next_step_publish` with `next_action`, `actor` (`"user" | "agent" | "agent_background"`), `waiting_on_user` (`true` or `false`), and optional `suggested_prompt`, `detail`, and `plan_path`.
3. **Read-only guarantee**:
   - Present the HUD link, status summary, and any `suggested_prompt` as read-only information. Never auto-execute or auto-submit `suggested_prompt` without the user's explicit command.
