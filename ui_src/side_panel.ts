import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
  type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps/app-with-deps";

interface PlanArtifactInfo {
  path?: string;
  name?: string;
  sha256?: string;
  stale?: boolean;
  staleReason?: string;
}

interface ActiveToolInfo {
  name?: string;
  summary?: string;
  approvalHint?: string;
  elapsedSec?: number;
}

interface SessionPanelItem {
  conversationId: string;
  shortId: string;
  workspaceName: string;
  workspacePaths?: string[];
  modelName?: string;
  lastUserInput?: string;
  lifecycle: string;
  sourceType: string;
  confidence: string;
  waitingOnUser: boolean;
  actor: string;
  actorLabel: string;
  statusBadge: string;
  sourceLabel: string;
  nextAction: string | null;
  detail?: string | null;
  options?: string[];
  suggestedPrompt?: string | null;
  copyBlocked?: boolean;
  stale?: boolean;
  staleReason?: string | null;
  planArtifact?: PlanArtifactInfo | null;
  activeTool?: ActiveToolInfo | null;
  updatedAt?: number;
  updatedAtIso?: string;
}

interface SidePanelPayload {
  version?: string;
  generatedAtIso?: string;
  bindingMode: "hook_bound" | "explicit" | "unbound";
  boundConversationId: string | null;
  sessionNotFound?: boolean;
  ambiguousPrefix?: boolean;
  selectedSession: SessionPanelItem | null;
  sessions: SessionPanelItem[];
  totalSessions?: number;
  waitingCount?: number;
}

type UiBindingMode = "hook_bound" | "user_selected" | "unbound";

const POLL_INTERVAL_MS = 3000;
const MAX_BACKOFF_MS = 12000;

const rootEl = document.getElementById("app") as HTMLElement;

const app = new App(
  { name: "NextStepHUD", version: "0.3.0" },
  {},
  { autoResize: false },
);

type RefreshTrigger = "manual" | "poll" | "select";

const state: {
  connected: boolean;
  connectError: string | null;
  loading: boolean;
  inFlight: boolean;
  pendingRefreshTrigger: RefreshTrigger | null;
  autoRefresh: boolean;
  pollDelayMs: number;
  refreshGeneration: number;
  lastError: string | null;
  lastSyncedIso: string | null;
  bindingMode: UiBindingMode;
  hookBoundCid: string | null;
  selectedCid: string | null;
  payload: SidePanelPayload | null;
} = {
  connected: false,
  connectError: null,
  loading: true,
  inFlight: false,
  pendingRefreshTrigger: null,
  autoRefresh: true,
  pollDelayMs: POLL_INTERVAL_MS,
  refreshGeneration: 0,
  lastError: null,
  lastSyncedIso: null,
  bindingMode: "unbound",
  hookBoundCid: null,
  selectedCid: null,
  payload: null,
};

let pollTimer: number | null = null;

function matchesCid(
  candidateCid: string | null | undefined,
  targetCid: string | null | undefined,
): boolean {
  if (!candidateCid || !targetCid) return false;
  return candidateCid === targetCid;
}

function cancelPendingPoll(): void {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function applyHostContext(ctx: McpUiHostContext | undefined): void {
  if (!ctx) return;
  if (ctx.theme) {
    applyDocumentTheme(ctx.theme);
  }
  if (ctx.styles?.variables) {
    applyHostStyleVariables(ctx.styles.variables);
  }
  if (ctx.styles?.css?.fonts) {
    applyHostFonts(ctx.styles.css.fonts);
  }
}

function isSidePanelPayload(val: unknown): val is SidePanelPayload {
  if (!val || typeof val !== "object") return false;
  const obj = val as Record<string, unknown>;
  return Array.isArray(obj.sessions) && typeof obj.bindingMode === "string";
}

function clearSelectedPayloadCache(): void {
  if (state.payload) {
    state.payload = {
      ...state.payload,
      bindingMode: "unbound",
      boundConversationId: null,
      sessionNotFound: false,
      ambiguousPrefix: false,
      selectedSession: null,
    };
  }
}

function invalidateHostBinding(): void {
  state.refreshGeneration++;
  state.pendingRefreshTrigger = null;
  state.bindingMode = "unbound";
  state.hookBoundCid = null;
  state.selectedCid = null;
  clearSelectedPayloadCache();
}

function ingestPayload(
  payload: SidePanelPayload,
  fromToolResult: boolean,
): void {
  state.payload = payload;
  state.loading = false;
  state.lastError = null;
  state.pollDelayMs = POLL_INTERVAL_MS;
  state.lastSyncedIso = payload.generatedAtIso ?? new Date().toISOString();

  if (fromToolResult) {
    if (payload.bindingMode === "hook_bound" && payload.boundConversationId) {
      state.bindingMode = "hook_bound";
      state.hookBoundCid = payload.boundConversationId;
      state.selectedCid = payload.boundConversationId;
    } else if (payload.bindingMode === "explicit" && payload.boundConversationId) {
      state.bindingMode = "user_selected";
      state.selectedCid = payload.boundConversationId;
    } else if (state.bindingMode !== "user_selected") {
      state.bindingMode = "unbound";
      state.selectedCid = null;
    }
  } else {
    if (
      state.bindingMode === "hook_bound" &&
      payload.boundConversationId
    ) {
      state.hookBoundCid = payload.boundConversationId;
      state.selectedCid = payload.boundConversationId;
    } else if (state.bindingMode === "user_selected") {
      if (payload.ambiguousPrefix) {
        state.bindingMode = "unbound";
        state.selectedCid = null;
      } else if (payload.boundConversationId) {
        state.selectedCid = payload.boundConversationId;
      }
    }
  }
}

async function copyTextSafely(
  text: string,
  buttonEl: HTMLButtonElement,
  hintEl: HTMLElement | null,
  textSourceEl: HTMLElement | null,
): Promise<void> {
  if (!text) return;

  const setHint = (msg: string): void => {
    if (hintEl) hintEl.textContent = msg;
  };

  const origLabel = buttonEl.textContent || "复制";
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(text);
      buttonEl.textContent = "已复制";
      setHint("已写入剪贴板（只读操作，不会自动提交；由你决定是否粘贴）。");
      window.setTimeout(() => {
        buttonEl.textContent = origLabel;
      }, 1500);
      return;
    }
  } catch {
    // Fall through to execCommand fallback
  }

  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (ok) {
      buttonEl.textContent = "已复制";
      setHint("已写入剪贴板（只读操作，不会自动提交；由你决定是否粘贴）。");
      window.setTimeout(() => {
        buttonEl.textContent = origLabel;
      }, 1500);
      return;
    }
  } catch {
    // Fall through to manual text-selection fallback
  }

  if (textSourceEl) {
    try {
      const sel = window.getSelection();
      if (sel) {
        const range = document.createRange();
        range.selectNodeContents(textSourceEl);
        sel.removeAllRanges();
        sel.addRange(range);
      }
    } catch {
      // Ignore selection error; text remains user-selectable
    }
  }
  setHint("剪贴板受宿主 iframe 策略限制：已为你高亮选中指令，请按 Cmd+C / Ctrl+C 手动复制。");
}

function scheduleNextPoll(): void {
  cancelPendingPoll();
  if (!state.autoRefresh || document.visibilityState !== "visible") {
    return;
  }
  pollTimer = window.setTimeout(() => {
    pollTimer = null;
    if (document.visibilityState === "visible" && state.autoRefresh) {
      void refreshFromServer("poll");
    }
  }, state.pollDelayMs);
}

const TRIGGER_PRIORITY: Record<RefreshTrigger, number> = {
  poll: 1,
  manual: 2,
  select: 3,
};

async function refreshFromServer(trigger: RefreshTrigger): Promise<void> {
  if (!state.connected) return;
  const caps = app.getHostCapabilities();
  if (!caps?.serverTools) {
    state.loading = false;
    state.lastError = "当前宿主未启用 serverTools 能力，无法刷新后端状态。";
    render();
    return;
  }
  if (state.inFlight) {
    // True single-flight guard across poll/manual/select; coalesce at most one follow-up
    if (trigger !== "poll") {
      if (
        !state.pendingRefreshTrigger ||
        TRIGGER_PRIORITY[trigger] >= TRIGGER_PRIORITY[state.pendingRefreshTrigger]
      ) {
        state.pendingRefreshTrigger = trigger;
      }
    }
    return;
  }

  cancelPendingPoll();
  const gen = state.refreshGeneration;
  const reqMode = state.bindingMode;
  const reqCid = reqMode !== "unbound" ? state.selectedCid : null;
  state.inFlight = true;
  if (trigger === "manual" || trigger === "select") {
    render();
  }

  const args: Record<string, unknown> = {};
  if (reqCid) {
    args.conversation_id = reqCid;
  }

  try {
    const res = await app.callServerTool({
      name: "next_step_panel_state",
      arguments: args,
    });
    const stillMatchesSelection =
      gen === state.refreshGeneration &&
      (reqCid === null
        ? state.bindingMode === "unbound"
        : state.bindingMode !== "unbound" && reqCid === state.selectedCid);
    if (!stillMatchesSelection) {
      return;
    }
    if (res.isError) {
      const errText =
        res.content?.find((b) => b.type === "text")?.text || "工具返回错误";
      state.loading = false;
      state.lastError = `状态查询失败: ${errText}`;
      state.pollDelayMs = Math.min(MAX_BACKOFF_MS, state.pollDelayMs * 2);
    } else if (isSidePanelPayload(res.structuredContent)) {
      ingestPayload(res.structuredContent, false);
    } else {
      state.loading = false;
      state.lastError = "后端未返回结构化会话状态 (structuredContent)。";
      state.pollDelayMs = Math.min(MAX_BACKOFF_MS, state.pollDelayMs * 2);
    }
  } catch (err) {
    const stillMatchesSelection =
      gen === state.refreshGeneration &&
      (reqCid === null
        ? state.bindingMode === "unbound"
        : state.bindingMode !== "unbound" && reqCid === state.selectedCid);
    if (stillMatchesSelection) {
      state.loading = false;
      state.lastError = `刷新异常: ${err instanceof Error ? err.message : String(err)}`;
      state.pollDelayMs = Math.min(MAX_BACKOFF_MS, state.pollDelayMs * 2);
    }
  } finally {
    state.inFlight = false;
    const queued = state.pendingRefreshTrigger;
    state.pendingRefreshTrigger = null;
    if (queued && state.connected && (queued !== "poll" || document.visibilityState === "visible")) {
      void refreshFromServer(queued);
    } else {
      render();
      scheduleNextPoll();
    }
  }
}

function selectSession(cid: string): void {
  state.refreshGeneration++;
  state.loading = false;
  state.bindingMode = "user_selected";
  state.selectedCid = cid;
  clearSelectedPayloadCache();
  render();
  void refreshFromServer("select");
}

function unbindToOverview(): void {
  state.refreshGeneration++;
  state.loading = false;
  state.bindingMode = "unbound";
  state.selectedCid = null;
  clearSelectedPayloadCache();
  render();
  void refreshFromServer("select");
}

function restoreHookBound(): void {
  if (!state.hookBoundCid) return;
  state.refreshGeneration++;
  state.loading = false;
  state.bindingMode = "hook_bound";
  state.selectedCid = state.hookBoundCid;
  clearSelectedPayloadCache();
  render();
  void refreshFromServer("select");
}

function renderSessionList(sessions: SessionPanelItem[]): HTMLElement {
  const wrap = el("div", "ns-session-list");
  if (!sessions.length) {
    wrap.appendChild(
      el(
        "div",
        "ns-banner ns-banner-info",
        "当前尚无已记录的会话状态。发送消息或调用 next_step_publish / next_step_side_panel 后会出现在这里。",
      ),
    );
    return wrap;
  }

  for (const s of sessions) {
    const cls = s.stale
      ? "ns-session-item stale"
      : s.waitingOnUser
        ? "ns-session-item waiting"
        : "ns-session-item";
    const btn = el("button", cls);
    btn.type = "button";
    btn.onclick = () => selectSession(s.conversationId);

    const top = el("div", "ns-session-top");
    top.appendChild(
      el("span", "ns-mono", `#${s.shortId} · ${s.workspaceName}`),
    );
    top.appendChild(el("span", "", s.statusBadge));
    btn.appendChild(top);

    const actionPreview =
      s.nextAction || "当前无已声明的下一步（空闲）";
    btn.appendChild(
      el("div", "ns-session-sub", `下一步: ${actionPreview}`),
    );

    const metaParts: string[] = [];
    if (s.lastUserInput) metaParts.push(`输入: ${s.lastUserInput}`);
    if (s.updatedAtIso) metaParts.push(s.updatedAtIso);
    if (metaParts.length) {
      btn.appendChild(el("div", "ns-session-sub", metaParts.join(" · ")));
    }
    wrap.appendChild(btn);
  }
  return wrap;
}

function renderSessionDetail(s: SessionPanelItem): HTMLElement {
  const isStale = Boolean(s.stale || s.planArtifact?.stale || s.copyBlocked);
  const isUnverified = Boolean(state.lastError);
  const copyBlocked = isStale || isUnverified;
  const cardCls = copyBlocked
    ? "ns-card stale"
    : s.waitingOnUser
      ? "ns-card waiting"
      : "ns-card";
  const card = el("div", cardCls);

  const metaRow = el("div", "ns-meta-row");
  metaRow.appendChild(
    el("span", "ns-mono", `#${s.shortId} · ${s.workspaceName}`),
  );
  metaRow.appendChild(el("span", "ns-mono", s.updatedAtIso || "时间未知"));
  card.appendChild(metaRow);

  if (s.lastUserInput) {
    card.appendChild(
      el("div", "ns-detail-text", `最近输入：${s.lastUserInput}`),
    );
  }

  if (isStale) {
    card.appendChild(
      el(
        "div",
        "ns-banner ns-banner-warn",
        `🟡 过期警示：${s.staleReason || s.planArtifact?.staleReason || "关联计划文件已变更，旧续作指令已禁用复制"}`,
      ),
    );
  } else if (isUnverified) {
    card.appendChild(
      el(
        "div",
        "ns-banner ns-banner-warn",
        "🟡 新鲜度待核验：最近一次状态刷新失败，无法确认指令是否过期，已暂禁复制。请点击「刷新」恢复后再复制。",
      ),
    );
  }

  if (s.activeTool?.approvalHint) {
    card.appendChild(
      el(
        "div",
        "ns-banner ns-banner-warn",
        `🟡 审批提示：${s.activeTool.approvalHint}`,
      ),
    );
  }

  const grid = el("div", "ns-grid");

  const mStatus = el("div", "ns-metric");
  mStatus.appendChild(el("div", "ns-metric-label", "Agent 状态"));
  const valCls = isStale
    ? "ns-metric-val ns-val-stale"
    : s.waitingOnUser
      ? "ns-metric-val ns-val-waiting"
      : s.lifecycle === "WORKING" ||
          s.lifecycle === "TOOL_RUNNING" ||
          s.lifecycle === "BACKGROUND_RUNNING"
        ? "ns-metric-val ns-val-running"
        : "ns-metric-val";
  mStatus.appendChild(
    el("div", valCls, `${s.statusBadge} [${s.lifecycle}]`),
  );
  grid.appendChild(mStatus);

  const mWait = el("div", "ns-metric");
  mWait.appendChild(el("div", "ns-metric-label", "Waiting on User"));
  mWait.appendChild(
    el(
      "div",
      s.waitingOnUser ? "ns-metric-val ns-val-waiting" : "ns-metric-val",
      s.waitingOnUser ? "是 · 等待用户确认/操作" : "否 · 无需用户介入",
    ),
  );
  grid.appendChild(mWait);

  const mActor = el("div", "ns-metric");
  mActor.appendChild(el("div", "ns-metric-label", "下一步执行者"));
  mActor.appendChild(el("div", "ns-metric-val", s.actorLabel || "未知"));
  grid.appendChild(mActor);

  card.appendChild(grid);

  const actionBox = el("div", "ns-action-box");
  actionBox.appendChild(el("div", "ns-section-label", "接下来要做什么 (Next Action)"));
  actionBox.appendChild(
    el(
      "p",
      "ns-action-text",
      s.nextAction ||
        "当前无已声明的下一步（回合空闲或尚未发布，不作猜测）",
    ),
  );
  if (s.detail) {
    actionBox.appendChild(el("div", "ns-detail-text", `说明：${s.detail}`));
  }
  card.appendChild(actionBox);

  if (s.options && s.options.length > 0) {
    const optSection = el("div", "ns-options");
    optSection.appendChild(
      el(
        "div",
        "ns-section-label",
        `待选确认项（系统拦截 · ${s.options.length} 项）`,
      ),
    );
    const optHint = el("div", "ns-copy-hint");
    for (const optText of s.options) {
      const row = el("div", "ns-opt-item");
      const span = el("span", "ns-opt-text", optText);
      const copyBtn = el("button", "ns-btn", "复制选项");
      copyBtn.type = "button";
      if (copyBlocked) {
        copyBtn.disabled = true;
        copyBtn.title = isStale
          ? "计划或状态已过期，已阻止直接复制"
          : "最近一次状态刷新失败，无法核验新鲜度";
      } else {
        copyBtn.onclick = () => void copyTextSafely(optText, copyBtn, optHint, span);
      }
      row.appendChild(span);
      row.appendChild(copyBtn);
      optSection.appendChild(row);
    }
    optSection.appendChild(optHint);
    card.appendChild(optSection);
  }

  if (s.suggestedPrompt) {
    const blocked = copyBlocked;
    const pBox = el("div", blocked ? "ns-prompt-box blocked" : "ns-prompt-box");
    const pHeader = el("div", "ns-prompt-header");
    pHeader.appendChild(
      el(
        "span",
        "ns-section-label",
        isStale
          ? "建议续作指令（已过期，已阻止复制）"
          : isUnverified
            ? "建议续作指令（刷新失败，暂禁复制）"
            : "Suggested Prompt（只读复制，不自动提交）",
      ),
    );
    const pre = el("pre", "ns-prompt-pre", s.suggestedPrompt);
    const hint = el(
      "div",
      "ns-copy-hint",
      isStale
        ? "关联计划已变更，请等待 Agent 重新发布或自行确认最新计划。"
        : isUnverified
          ? "最近一次后端刷新失败，无法核验计划与指令是否过期，请点击「刷新」恢复后再复制。"
          : "",
    );

    const copyBtn = el(
      "button",
      blocked ? "ns-btn" : "ns-btn ns-btn-primary",
      isStale
        ? "已过期 · 禁止复制"
        : isUnverified
          ? "待核验 · 暂禁复制"
          : "复制指令",
    );
    copyBtn.type = "button";
    if (blocked) {
      copyBtn.disabled = true;
      copyBtn.title = isStale
        ? "计划或状态已过期，已阻止直接复制旧指令"
        : "最近一次状态刷新失败，无法核验指令是否过期";
    } else {
      copyBtn.onclick = () =>
        void copyTextSafely(s.suggestedPrompt || "", copyBtn, hint, pre);
    }

    pHeader.appendChild(copyBtn);
    pBox.appendChild(pHeader);
    pBox.appendChild(pre);
    pBox.appendChild(hint);
    card.appendChild(pBox);
  }

  const foot = el("div", "ns-foot-meta");
  foot.appendChild(
    el(
      "div",
      "",
      `状态来源与可信度：${s.sourceLabel}（${s.confidence || "UNKNOWN"}）`,
    ),
  );
  if (s.planArtifact?.path) {
    const planLabel = s.planArtifact.name || s.planArtifact.path;
    const planStatus = s.planArtifact.stale
      ? `🟡 已过期（${s.planArtifact.staleReason || "计划已改写"}）`
      : "✓ 未过期";
    const shaPrefix = s.planArtifact.sha256
      ? ` · sha256:${s.planArtifact.sha256.slice(0, 8)}`
      : "";
    foot.appendChild(
      el("div", "ns-mono", `关联计划：${planLabel} [${planStatus}${shaPrefix}]`),
    );
  }
  foot.appendChild(el("div", "ns-mono", `会话 ID：${s.conversationId}`));
  card.appendChild(foot);

  return card;
}

function render(): void {
  rootEl.textContent = "";
  const container = el("div", "ns-root");

  const header = el("div", "ns-header");
  const titleRow = el("div", "ns-title-row");
  titleRow.appendChild(el("h1", "ns-title", "Next Step HUD"));
  titleRow.appendChild(el("span", "ns-ver", state.payload?.version ? `v${state.payload.version}` : "v0.3.0"));
  header.appendChild(titleRow);

  const toolbar = el("div", "ns-toolbar");
  const refreshBtn = el("button", "ns-btn", state.inFlight ? "刷新中..." : "刷新");
  refreshBtn.type = "button";
  refreshBtn.disabled = !state.connected || state.inFlight;
  refreshBtn.onclick = () => void refreshFromServer("manual");
  toolbar.appendChild(refreshBtn);

  const autoBtn = el(
    "button",
    state.autoRefresh ? "ns-btn ns-btn-active" : "ns-btn",
    state.autoRefresh ? "自动刷新: 开" : "自动刷新: 关",
  );
  autoBtn.type = "button";
  autoBtn.onclick = () => {
    state.autoRefresh = !state.autoRefresh;
    render();
    scheduleNextPoll();
  };
  toolbar.appendChild(autoBtn);
  header.appendChild(toolbar);
  container.appendChild(header);

  if (state.connectError) {
    container.appendChild(
      el("div", "ns-banner ns-banner-danger", `MCP Bridge 连接失败: ${state.connectError}`),
    );
    rootEl.appendChild(container);
    return;
  }

  if (state.lastError) {
    container.appendChild(el("div", "ns-banner ns-banner-warn", state.lastError));
  }

  const sessions = state.payload?.sessions ?? [];
  const bindingBar = el("div", "ns-binding-bar");
  const bindingTop = el("div", "ns-binding-top");

  if (state.bindingMode === "hook_bound" && state.selectedCid) {
    bindingTop.appendChild(
      el("span", "ns-bind-pill bound", `🔗 已绑定当前会话 #${state.selectedCid.slice(0, 8)}`),
    );
  } else if (state.bindingMode === "user_selected" && state.selectedCid) {
    bindingTop.appendChild(
      el("span", "ns-bind-pill selected", `👤 已手动选定 #${state.selectedCid.slice(0, 8)}`),
    );
  } else {
    bindingTop.appendChild(
      el("span", "ns-bind-pill unbound", "⚠️ 未绑定会话（需手动选择，绝不猜测）"),
    );
  }

  const bindBtns = el("div", "ns-toolbar");
  if (state.bindingMode !== "unbound") {
    const unbindBtn = el("button", "ns-btn", "查看全部 / 解绑");
    unbindBtn.type = "button";
    unbindBtn.onclick = unbindToOverview;
    bindBtns.appendChild(unbindBtn);
  }
  if (
    state.hookBoundCid &&
    (state.bindingMode !== "hook_bound" || state.selectedCid !== state.hookBoundCid)
  ) {
    const backBtn = el("button", "ns-btn ns-btn-primary", "回到原绑定会话");
    backBtn.type = "button";
    backBtn.onclick = restoreHookBound;
    bindBtns.appendChild(backBtn);
  }
  bindingTop.appendChild(bindBtns);
  bindingBar.appendChild(bindingTop);

  if (sessions.length > 0) {
    const select = el("select", "ns-select");
    select.setAttribute("aria-label", "选择 Antigravity 会话");
    const placeholder = el(
      "option",
      "",
      state.bindingMode === "unbound"
        ? `-- 请显式选择会话（共 ${sessions.length} 个，不自动猜测） --`
        : `-- 切换查看其他会话（共 ${sessions.length} 个） --`,
    );
    placeholder.value = "";
    select.appendChild(placeholder);

    for (const s of sessions) {
      const opt = document.createElement("option");
      opt.value = s.conversationId;
      const waitTag = s.stale ? "🟡" : s.waitingOnUser ? "🔴" : "⚪";
      opt.textContent = `${waitTag} #${s.shortId} (${s.workspaceName}) - ${s.nextAction || "空闲"}`;
      if (state.bindingMode !== "unbound" && s.conversationId === state.selectedCid) {
        opt.selected = true;
      }
      select.appendChild(opt);
    }
    select.onchange = () => {
      if (select.value) {
        selectSession(select.value);
      } else {
        unbindToOverview();
      }
    };
    bindingBar.appendChild(select);
  }
  container.appendChild(bindingBar);

  const curPayload = state.payload;
  const matchedNotFound = Boolean(
    curPayload?.sessionNotFound && matchesCid(curPayload.boundConversationId, state.selectedCid),
  );
  const matchedSelectedSession =
    curPayload?.selectedSession && matchesCid(curPayload.selectedSession.conversationId, state.selectedCid)
      ? curPayload.selectedSession
      : null;

  if (state.loading) {
    container.appendChild(
      el(
        "div",
        "ns-banner ns-banner-info",
        curPayload
          ? "正在同步当前会话绑定状态..."
          : "正在连接 Antigravity Side Panel 并加载后端状态...",
      ),
    );
  } else if (state.bindingMode === "unbound") {
    container.appendChild(
      el(
        "div",
        "ns-banner ns-banner-info",
        "当前未收到会话绑定上下文（例如从面板菜单手动打开，或存在同名冲突）。请从下方列表手动点击会话以查看，系统不会擅自猜测最近会话。",
      ),
    );
    container.appendChild(renderSessionList(sessions));
  } else if (matchedNotFound) {
    const missingCid = state.selectedCid || curPayload?.boundConversationId || "未知";
    container.appendChild(
      el(
        "div",
        "ns-banner ns-banner-warn",
        `已绑定会话 #${missingCid.slice(0, 8)}，但后端尚无该会话的状态记录（尚未触发 Hook 或发布下一步），未越权展示其他会话数据。`,
      ),
    );
    if (sessions.length > 0) {
      container.appendChild(el("div", "ns-section-label", "其他已记录会话（需点击才会切换）："));
      container.appendChild(renderSessionList(sessions));
    }
  } else if (matchedSelectedSession) {
    container.appendChild(renderSessionDetail(matchedSelectedSession));
  } else if (state.selectedCid) {
    const isLoadingCid = state.inFlight || state.pendingRefreshTrigger !== null;
    container.appendChild(
      el(
        "div",
        isLoadingCid ? "ns-banner ns-banner-info" : "ns-banner ns-banner-warn",
        isLoadingCid
          ? `正在加载会话 #${state.selectedCid.slice(0, 8)} 的最新状态...`
          : `尚未获取到会话 #${state.selectedCid.slice(0, 8)} 的状态（已隐藏其他会话内容以防跨会话混淆，请点击「刷新」重试）。`,
      ),
    );
  } else {
    container.appendChild(renderSessionList(sessions));
  }

  const statusLine = el("div", "ns-status-line");
  statusLine.appendChild(
    el("span", "", `同步: ${state.lastSyncedIso || "尚未同步"} · 仅活动面板轮询 (${Math.round(state.pollDelayMs / 1000)}s)`),
  );
  statusLine.appendChild(
    el("span", "", `等待用户会话: ${state.payload?.waitingCount ?? 0} / ${state.payload?.totalSessions ?? sessions.length}`),
  );
  container.appendChild(statusLine);

  rootEl.appendChild(container);
}

// Register handlers BEFORE app.connect() so initial tool input/result/context are never missed
app.ontoolinput = () => {
  cancelPendingPoll();
  invalidateHostBinding();
  state.loading = true;
  state.lastError = null;
  render();
};

app.ontoolresult = (result) => {
  state.refreshGeneration++;
  state.pendingRefreshTrigger = null;
  if (result.isError) {
    const errText =
      result.content?.find((b) => b.type === "text")?.text || "工具调用返回错误";
    invalidateHostBinding();
    state.loading = false;
    state.lastError = errText;
    render();
    return;
  }
  if (isSidePanelPayload(result.structuredContent)) {
    ingestPayload(result.structuredContent, true);
    render();
    scheduleNextPoll();
  } else {
    invalidateHostBinding();
    state.loading = false;
    state.lastError = "工具调用未返回结构化会话状态，已重置为未绑定状态。";
    render();
  }
};

app.ontoolcancelled = (params) => {
  invalidateHostBinding();
  state.loading = false;
  state.lastError = `工具调用已取消: ${params?.reason || "未知原因"}`;
  render();
};

app.onhostcontextchanged = (ctx) => {
  applyHostContext(ctx);
};

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    if (state.connected && state.autoRefresh) {
      void refreshFromServer("poll");
    }
  } else {
    cancelPendingPoll();
  }
});

render();

async function bootstrap(): Promise<void> {
  try {
    await app.connect();
    state.connected = true;
    applyHostContext(app.getHostContext());
    if (!state.payload) {
      await refreshFromServer("manual");
    } else {
      render();
      scheduleNextPoll();
    }
  } catch (err) {
    state.connectError = err instanceof Error ? err.message : String(err);
    state.loading = false;
    render();
  }
}

void bootstrap();
