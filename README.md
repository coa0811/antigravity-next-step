# Next Step HUD (`next-step`)

面向 Google Antigravity 的「下一步指令」状态感知与只读 HUD 插件（路线 A 实现）。切回任意会话时，一眼确认：**1. 是否在等我、2. 由谁做、3. 接下来要做什么**。

## 核心架构（路线 A）

1. **双层状态源与可信度分级**
   - **`APPROVAL_REQUIRED`（系统待办/审批，确定性事实，最高优先级）**：由 `PreToolUse` / `PostToolUse` Hook 自动拦截 `ask_question`（完整提取多问题题干与各自候选项）、`ask_permission`（提取 `Action(Target)` 与原因）以及 `ArtifactMetadata.RequestFeedback: true` 的计划/工件写入（提示点击 `Proceed` 或提交评论），无需模型额外配合。
   - **`AGENT_DECLARED`（Agent 显式声明，高可信）**：由 `rules/AGENTS.md` 引导 Agent 在存在后续动作时调用本地 Stdio MCP 工具 `next_step_publish`（或解析 `<!-- NEXT_STEP: {...} -->` 回退标记），记录 `next_action`、`actor`、`waiting_on_user`、完整只读 `suggested_prompt` 及关联计划工件 `plan_path` 的 SHA-256 指纹。
   - **`LIFECYCLE` / `NONE`（生命周期运行态与无信号空状态）**：`PreInvocation` 标记 `WORKING`、`PreToolUse` 标记 `TOOL_RUNNING`（敏感工具超 8 秒提示可能存在宿主权限确认框）、`Stop(fullyIdle=false)` 标记 `BACKGROUND_RUNNING`；当回合正常结束且既无待批交互也无显式声明时，`Stop` Hook 确定性置为 `NONE`（*当前无已声明的下一步*），绝不从自由格式 Markdown 猜测。

2. **会话强制归属、性能护航与过期检测（防串用 / 零常驻进程）**
   - **物理隔离与 `PreToolUse.overwrite` 绑定**：每个会话独立落盘至 `$PLUGIN_DATA/sessions/<conversationId>.json`。`PreToolUse` 拦截 `next_step_publish` / `next_step_side_panel` 时自动将宿主 `stdin.conversationId` 注入并锁定到当前会话，同时由 `PostToolUse` 二次核对，杜绝跨会话或 `/fork` 分叉串写。
   - **轻量 I/O 路径**：计划工件使用 `(mtimeNs, sizeBytes, inode)` 静态稳态快路径跳过未变更文件的重复 SHA-256 哈希；长对话 `transcript.jsonl` 仅从文件尾部逆向分块定位最新用户输入；列表扫描默认限制最近 60 个活跃会话；**无后台守护进程、不占本地 HTTP 端口**。
   - **4 重过期失效规则**：
     1. 新用户轮次开始（`PreInvocation` `invocationNum <= 1` 或新 `USER_EXPLICIT` 步骤）立即清空旧待办；同一轮次内调工具后的续跑（`invocationNum > 1`）保留 `pendingDeclaration` 直至 `Stop`。
     2. 关联计划文件（`planArtifact.path`）在磁盘上的 SHA-256 发生变化时，`PostToolUse` 与 `evaluate_staleness` 立即标记 `🟡 已过期（计划已改写）` 并禁用复制按钮。
     3. `WORKING` / `TOOL_RUNNING` 超过 `120s` 无事件更新时自动标记超时提示。
     4. `Stop` 收到 `USER_CANCELED` 或 `ERROR` 时立即作废本轮暂存声明。

3. **只读展示层（MCP Apps 原生侧边面板 + 纯文本 Markdown 降级 + CLI `statusLine` 伴随脚本）**
   - **MCP Apps 原生 Side Panel（`aux_pane`）**：由 [next_step_mcp_server.py](scripts/next_step_mcp_server.py) 提供单文件自包含 `ui://next-step/side-panel.html` 资源（`text/html;profile=mcp-app`，基于官方 `@modelcontextprotocol/ext-apps` Bridge，支持宿主明暗主题、CSS 变量与 CSP 默认拒绝策略）；通过 `next_step_side_panel` 工具声明 `com.google.antigravity/ui.entrypoints: [{"type": "aux_pane", "title": "Next Step HUD"}]`，并在面板侧调用 `visibility: ["app"]` 只读查询工具 `next_step_panel_state` 按活动可见状态轻量轮询刷新。未绑定列表支持按「🔴 待我处理 / 🟢 运行中 / ⚪ 历史空闲（默认折叠）」分组以及工作区筛选（继续严格绝不自动猜测当前会话）；在对话中由 Agent 调用时经 `PreToolUse` 自动绑定当前 `conversationId`；计划过期或刷新失败时强制阻断旧指令复制并提供受限剪贴板降级。
   - **纯文本 / 非 UI 兼容**：不支持 MCP Apps 的客户端调用 `next_step_side_panel` 或 `next_step_get` 时直接获得结构化只读 Markdown 状态。
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
├── ui/
│   └── side-panel.html
├── ui_src/
│   ├── build.mjs
│   ├── package.json
│   ├── package-lock.json
│   ├── side_panel.css
│   └── side_panel.ts
├── scripts/
│   ├── next_step_core.py
│   ├── next_step_hook.py
│   ├── next_step_mcp_server.py
│   ├── run_python.sh
│   └── statusline.sh
└── tests/
    ├── test_next_step.py
    └── verify_side_panel.mjs
```

## 安装、验证与启用方式

```bash
# 0. 获取仓库（或进入插件根目录 <path-to-repo>）
git clone https://github.com/coa0811/antigravity-next-step.git
cd antigravity-next-step

# 0.5 若修改了 ui_src/，重新构建单文件 MCP App UI（仓库已包含预构建的 ui/side-panel.html）
# npm --prefix ./ui_src install && npm --prefix ./ui_src run build

# 1. 运行回归测试（22 项，含构建产物 Node VM 行为测试）
./scripts/run_python.sh -m unittest discover -s ./tests -v

# 2. 校验插件清单与目录结构
agy plugin validate . --strict

# 3. 安装或软链到 Antigravity 全局插件目录
agy plugin install .
# 或开发模式软链：
ln -s "$(pwd)" ~/.gemini/config/plugins/next-step
```
