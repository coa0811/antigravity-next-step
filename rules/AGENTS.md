# Next Step HUD Publishing Rule (`next-step`)

- **显式发布下一步指令（`next_step_publish`）**：
  - 当本轮任务结束前仍存在**明确的后续动作、待验证步骤、待确认分支或阶段交接**时，在收尾回复前调用 `next_step_publish`（传入一句话 `next_action`、`actor: "user" | "agent" | "agent_background"`、`waiting_on_user`，以及可选的 `suggested_prompt`、`detail`、`plan_path`）。
  - 若当前步骤关联了计划或任务工件（如 `implementation_plan.md`），传入 `plan_path` 以便 HUD 记录 SHA-256 指纹并在计划被改写时自动标记过期。
- **不强行编造下一步**：
  - 当任务已彻底完结、无需任何后续动作，或本轮仅为一次性问答时，**不要调用** `next_step_publish`（由 `Stop` Hook 自动落盘为确定性的 `NONE / 当前无已声明的下一步`）；
  - 当你已调用 `ask_question`、`ask_permission` 或写入带 `ArtifactMetadata.RequestFeedback: true` 的计划工件时，`PreToolUse` / `PostToolUse` Hook 会自动捕获 `APPROVAL_REQUIRED` 状态，无需重复调用 `next_step_publish`。
- **只读边界**：插件仅展示下一步提示与复制按钮，严禁代用户自动提交或自动执行 `suggested_prompt`。
