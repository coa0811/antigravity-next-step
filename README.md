# Next Step HUD (`next-step`)

面向 Google Antigravity 的「下一步指令」状态感知与只读 HUD 插件（路线 A 实现）。切回任意会话时，一眼确认：**1. 是否在等我、2. 由谁做、3. 接下来要做什么**。

## 核心架构（路线 A）

1. **双层状态源与可信度分级**
   - **`APPROVAL_REQUIRED`（系统待办/审批，确定性事实，最高优先级）**：由 `PreToolUse` / `PostToolUse` Hook 自动拦截 `ask_question`（提取题干与候选项）、`ask_permission`（提取 `Action(Target)` 与原因）以及 `ArtifactMetadata.RequestFeedback: true` 的计划/工件写入（提示点击 `Proceed` 或提交评论），无需模型额外配合。
   - **`AGENT_DECLARED`（Agent 显式声明，高可信）**：由 `rules/AGENTS.md` 引导 Agent 在存在后续动作时调用本地 Stdio MCP 工具 `next_step_publish`（或解析 `<!-- NEXT_STEP: {...} -->` 回退标记），记录 `next_action`、`actor`、`waiting_on_user`、只读 `suggested_prompt` 及关联计划工件 `plan_path` 的 SHA-256 指纹。
   - **`LIFECYCLE` / `NONE`（生命周期运行态与无信号空状态）**：`PreInvocation` 标记 `WORKING`、`PreToolUse` 标记 `TOOL_RUNNING`（敏感工具超 8 秒提示可能存在宿主权限确认框）、`Stop(fullyIdle=false)` 标记 `BACKGROUND_RUNNING`；当回合正常结束且既无待批交互也无显式声明时，`Stop` Hook 确定性置为 `NONE`（*当前无已声明的下一步*），绝不从自由格式 Markdown 猜测。

2. **会话强制归属与过期检测（防串用）**
   - **物理隔离与 `PreToolUse.overwrite` 绑定**：每个会话独立落盘至 `$PLUGIN_DATA/sessions/<conversationId>.json`（原子写 `tempfile + os.replace`）。`PreToolUse` 拦截 `next_step_publish` 时自动将宿主 `stdin.conversationId` 注入并锁定到当前会话，同时由 `PostToolUse` 二次核对，杜绝跨会话或 `/fork` 分叉串写。
   - **4 重过期失效规则**：
     1. 新用户轮次开始（`PreInvocation` `invocationNum <= 1` 或新 `USER_EXPLICIT` 步骤）立即清空旧待办；同一轮次内调工具后的续跑（`invocationNum > 1`）保留 `pendingDeclaration` 直至 `Stop`。
     2. 关联计划文件（`planArtifact.path`）在磁盘上的 SHA-256 发生变化时，`PostToolUse` 与 `evaluate_staleness` 立即标记 `🟡 已过期（计划已改写）` 并禁用复制按钮。
     3. `WORKING` / `TOOL_RUNNING` 超过 `120s` 无事件更新时自动标记超时提示。
     4. `Stop` 收到 `USER_CANCELED` 或 `ERROR` 时立即作废本轮暂存声明。

3. **只读展示层（macOS 原生置顶悬浮窗 + 本地 SSE HUD + 静态快照 + CLI `statusLine` 伴随脚本）**
   - **macOS 原生非激活置顶悬浮窗（Always-on-Top Non-Activating `NSPanel`）**：通过 `scripts/floating_hud.swift` + `scripts/next_step_floating.py`（或一键脚本 `./scripts/open_floating_hud.sh`，也可通过 `next_step_get(open_window=True)` 拉起）将精简版 `?mode=compact` 置顶悬浮在 Antigravity 聊天框上方并记住位置/尺寸；点击 **`[复制指令]`** 或待选选项通过 `WKScriptMessageHandler` 直接写入系统剪贴板，**不抢占聊天框焦点**，点完直接 `Cmd+V` 粘贴。MCP 服务启动（`next_step_mcp_server.py`）与 `SessionStart`/`PreInvocation` Hook 会非阻塞自动拉起悬浮窗；手动点 `×` 关闭会写入 `$PLUGIN_DATA/floating_hud_dismissed.json` 防重复弹出，直到 MCP 重启、执行 `/next-step` 或运行 `./scripts/open_floating_hud.sh` 才清除恢复；设置 `NEXT_STEP_NO_AUTO_FLOATING_HUD=1` 可关闭自动拉起。
   - **本地实时 SSE HUD**：`scripts/next_step_hud_server.py` 仅绑定 `127.0.0.1`（默认端口 `18765`，冲突自动分配空闲端口并记录于 `$PLUGIN_DATA/hud_server.json`），支持完整/精简双模式切换、多会话 Tab 切换（`waitingOnUser: true` 自动置顶）与只读 **`[复制指令]`** 按钮。
   - **静态快照兜底**：每次状态变更同步生成 `$PLUGIN_DATA/hud_snapshot.html` 与 `$PLUGIN_DATA/latest_next_step.md`。
   - **CLI 状态栏兼容**：`scripts/statusline.sh` 可直接用于 Antigravity CLI `/statusline`，合并 `tool_confirmation_pending` 与当前会话下一步指令。

## 目录结构

```text
plugins/next-step/
├── plugin.json
├── mcp_config.json
├── hooks.json
├── LICENSE
├── README.md
├── rules/
│   └── AGENTS.md
├── skills/
│   └── next-step/
│       └── SKILL.md
├── scripts/
│   ├── floating_hud.swift
│   ├── next_step_core.py
│   ├── next_step_floating.py
│   ├── next_step_hook.py
│   ├── next_step_hud_server.py
│   ├── next_step_mcp_server.py
│   ├── open_floating_hud.sh
│   ├── run_python.sh
│   └── statusline.sh
└── tests/
    └── test_next_step.py
```

## 安装、验证与启用方式

```bash
# 0. 获取仓库（或进入插件根目录 <path-to-repo>）
git clone https://github.com/coa0811/antigravity-next-step.git
cd antigravity-next-step

# 1. 运行回归测试（22 项）
./scripts/run_python.sh -m unittest discover -s ./tests -v

# 2. 校验插件清单与目录结构
agy plugin validate .

# 3. 安装或软链到 Antigravity 全局插件目录
agy plugin install .
# 或开发模式软链：
ln -s "$(pwd)" ~/.gemini/config/plugins/next-step
```
