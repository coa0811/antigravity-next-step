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

interface QuestionPromptItem {
  question: string;
  options: string[];
  multiSelect?: boolean;
}

interface WorkspaceOption {
  key: string;
  label: string;
}

interface SessionPanelItem {
  conversationId: string;
  shortId: string;
  conversationName?: string | null;
  workspaceName: string;
  workspaceKey?: string;
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
  questions?: QuestionPromptItem[];
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
  workspaceFilter?: string | null;
  workspaceOptions?: WorkspaceOption[];
  workspaces?: string[];
  totalSessions?: number;
  waitingCount?: number;
}

type UiBindingMode = "hook_bound" | "user_selected" | "unbound";

const POLL_INTERVAL_MS = 3000;
const MAX_BACKOFF_MS = 12000;

const rootEl = document.getElementById("app") as HTMLElement;

const app = new App(
  { name: "NextStepHUD", version: "0.4.0" },
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
  backendWorkspaceFilter: string;
  workspaceFilter: string;
  knownWorkspaceOptions: WorkspaceOption[];
  showIdleSessions: boolean;
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
  backendWorkspaceFilter: "",
  workspaceFilter: "",
  knownWorkspaceOptions: [],
  showIdleSessions: false,
  payload: null,
};

function getSessionWorkspaceKey(s: SessionPanelItem): string {
  if (s.workspaceKey) return s.workspaceKey;
  if (s.workspacePaths && s.workspacePaths.length > 0 && s.workspacePaths[0]) {
    return s.workspacePaths[0];
  }
  return s.workspaceName || "__none__";
}

function sessionMatchesWorkspace(s: SessionPanelItem, filterKey: string): boolean {
  if (!filterKey) return true;
  const sessionKey = getSessionWorkspaceKey(s);
  if (filterKey === "__none__") {
    return sessionKey === "__none__";
  }
  if (s.workspaceKey) {
    return s.workspaceKey === filterKey;
  }
  return sessionKey === filterKey || s.workspaceName === filterKey;
}

function extractBaseWorkspaceName(key: string, label?: string): string {
  if (key === "__none__") return "未关联工作区";
  const trimmed = key.replace(/\/+$/, "");
  const slashIdx = trimmed.lastIndexOf("/");
  if (slashIdx >= 0 && slashIdx < trimmed.length - 1) {
    return trimmed.slice(slashIdx + 1);
  }
  if (label) {
    const suffix = ` (${key})`;
    if (label.endsWith(suffix) && label.length > suffix.length) {
      return label.slice(0, -suffix.length);
    }
    return label;
  }
  return trimmed || key;
}

function recomputeWorkspaceLabels(options: WorkspaceOption[]): WorkspaceOption[] {
  const bases = new Map<string, string>();
  const counts = new Map<string, number>();
  for (const opt of options) {
    const base = extractBaseWorkspaceName(opt.key, opt.label);
    bases.set(opt.key, base);
    if (opt.key !== "__none__") {
      counts.set(base, (counts.get(base) ?? 0) + 1);
    }
  }
  return options.map((opt) => {
    if (opt.key === "__none__") {
      return { key: opt.key, label: "未关联工作区" };
    }
    const base = bases.get(opt.key) || opt.label;
    const needPath = (counts.get(base) ?? 0) > 1 && opt.key !== base;
    return { key: opt.key, label: needPath ? `${base} (${opt.key})` : base };
  });
}

let pollTimer: number | null = null;

// --- Render throttling: avoid clobbering focus / text selection on background polls ---
let lastRenderSig: string | null = null;
let lastCopyGateSig: string | null = null;
let deferredRender = false;
let syncLabelEl: HTMLElement | null = null;
let liveRegionEl: HTMLElement | null = null;
let lastLiveMessage = "";

function clipLabel(text: string, maxLen: number): string {
  const chars = Array.from(text.trim());
  if (chars.length <= maxLen) return chars.join("");
  return `${chars.slice(0, Math.max(0, maxLen - 1)).join("")}…`;
}

function sessionSignatureView(
  s: SessionPanelItem | null | undefined,
): SessionPanelItem | null {
  if (!s) return null;
  if (!s.activeTool || s.activeTool.elapsedSec === undefined) return s;
  const { elapsedSec: _ignored, ...restTool } = s.activeTool;
  return { ...s, activeTool: restTool };
}

function resolveSelectedSessionName(
  matchedSelected: SessionPanelItem | null,
  allSessions: SessionPanelItem[],
  cid: string | null,
): string | null {
  const fromSelected = matchedSelected?.conversationName?.trim();
  if (fromSelected) return fromSelected;
  if (!cid) return null;
  const found = allSessions.find((s) => matchesCid(s.conversationId, cid));
  const fromList = found?.conversationName?.trim();
  return fromList || null;
}

/**
 * Anything that can change what is copyable (or whether copying is allowed) must render
 * immediately, never be deferred: errors, binding, and the full selected-session content
 * (prompt / options / questions / plan fingerprint / staleness).
 */
function copyGateSignature(): string {
  return JSON.stringify([
    state.lastError,
    state.bindingMode,
    state.selectedCid,
    sessionSignatureView(state.payload?.selectedSession),
  ]);
}

function renderSignature(): string {
  const p = state.payload;
  const payloadSig = p
    ? {
        ...p,
        generatedAtIso: undefined,
        selectedSession: sessionSignatureView(p.selectedSession),
      }
    : null;
  return JSON.stringify([
    payloadSig,
    state.connected,
    state.connectError,
    state.loading,
    state.inFlight,
    state.pendingRefreshTrigger,
    state.autoRefresh,
    state.pollDelayMs,
    state.lastError,
    state.bindingMode,
    state.hookBoundCid,
    state.selectedCid,
    state.backendWorkspaceFilter,
    state.workspaceFilter,
    state.knownWorkspaceOptions,
    state.showIdleSessions,
    // Coarse minute bucket so relative timestamps ("3 分钟前") stay roughly fresh
    Math.floor(Date.now() / 60000),
  ]);
}

function nodeInsideRoot(node: Node | null | undefined): boolean {
  if (!node || typeof rootEl.contains !== "function") return false;
  try {
    return rootEl.contains(node);
  } catch {
    return false;
  }
}

function isUserInteracting(): boolean {
  const active = document.activeElement as HTMLElement | null | undefined;
  if (active && active.tagName === "SELECT" && nodeInsideRoot(active)) return true;
  const sel = typeof window.getSelection === "function" ? window.getSelection() : null;
  if (sel && sel.isCollapsed === false && nodeInsideRoot(sel.anchorNode)) return true;
  return false;
}

function formatSyncTitle(): string {
  if (!state.lastSyncedIso) return "";
  return `${state.lastSyncedIso} · 面板可见时每 ${Math.round(state.pollDelayMs / 1000)}s 刷新`;
}

function updateSyncLabel(): void {
  if (!syncLabelEl) return;
  syncLabelEl.textContent = formatSyncLabel();
  if (state.lastSyncedIso) {
    syncLabelEl.title = formatSyncTitle();
  }
}

/**
 * Render path for background polls: skip the DOM rebuild when nothing visible changed,
 * and postpone it while the user has a <select> focused or text selected (manual-copy
 * fallback). Copy-gating changes (errors / staleness) always render immediately.
 */
function renderPassive(): void {
  if (copyGateSignature() !== lastCopyGateSig) {
    render();
    return;
  }
  if (renderSignature() === lastRenderSig) {
    updateSyncLabel();
    return;
  }
  if (isUserInteracting()) {
    deferredRender = true;
    updateSyncLabel();
    return;
  }
  render();
}

function flushDeferredRender(): void {
  if (deferredRender && !isUserInteracting()) {
    render();
  }
}

function announce(message: string): void {
  if (message === lastLiveMessage) return;
  lastLiveMessage = message;
  if (!liveRegionEl) {
    liveRegionEl = document.createElement("div");
    liveRegionEl.className = "ns-sr-only";
    liveRegionEl.setAttribute("role", "status");
    liveRegionEl.setAttribute("aria-live", "polite");
    document.body.appendChild(liveRegionEl);
  }
  liveRegionEl.textContent = message;
}

// --- Presentation helpers ---
type Tone = "waiting" | "running" | "stale" | "idle";

function sessionTone(s: SessionPanelItem): Tone {
  if (s.stale) return "stale";
  if (s.waitingOnUser) return "waiting";
  if (
    s.lifecycle === "WORKING" ||
    s.lifecycle === "TOOL_RUNNING" ||
    s.lifecycle === "BACKGROUND_RUNNING"
  ) {
    return "running";
  }
  return "idle";
}

function shortStatusLabel(s: SessionPanelItem): string {
  if (s.stale) return "可能已过期";
  if (s.waitingOnUser) {
    switch (s.lifecycle) {
      case "WAITING_PLAN_REVIEW":
        return "等你审阅计划";
      case "WAITING_USER_DECISION":
        return "等你回答 / 选择";
      case "WAITING_USER_APPROVAL":
        return "等你授权";
      case "ERROR":
      case "CANCELED":
        return "已中止，待介入";
      default:
        return "等你确认";
    }
  }
  switch (s.lifecycle) {
    case "WORKING":
    case "TOOL_RUNNING":
      return "Agent 运行中";
    case "BACKGROUND_RUNNING":
      return "后台任务运行中";
    default:
      return "空闲";
  }
}

function statusChip(s: SessionPanelItem): HTMLElement {
  const chip = el("span", `ns-chip ns-tone-${sessionTone(s)}`);
  const dot = el("span", "ns-dot");
  dot.setAttribute("aria-hidden", "true");
  chip.appendChild(dot);
  chip.appendChild(el("span", "", shortStatusLabel(s)));
  if (s.statusBadge) chip.title = s.statusBadge;
  return chip;
}

function parseIsoMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

function formatRelativeTime(iso: string | null | undefined): string {
  const ms = parseIsoMs(iso);
  if (ms === null) return iso || "时间未知";
  const diffSec = Math.round((Date.now() - ms) / 1000);
  if (diffSec < 0) return "刚刚";
  if (diffSec < 45) return "刚刚";
  if (diffSec < 3600) return `${Math.max(1, Math.round(diffSec / 60))} 分钟前`;
  if (diffSec < 86400) return `${Math.round(diffSec / 3600)} 小时前`;
  if (diffSec < 86400 * 7) return `${Math.round(diffSec / 86400)} 天前`;
  const d = new Date(ms);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

function timeEl(iso: string | null | undefined, className = "ns-time"): HTMLElement {
  const node = el("span", className, formatRelativeTime(iso));
  if (iso) node.title = iso;
  return node;
}

function formatSyncLabel(): string {
  const ms = parseIsoMs(state.lastSyncedIso);
  if (ms === null) return "尚未同步";
  const d = new Date(ms);
  const pad = (n: number): string => String(n).padStart(2, "0");
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return state.autoRefresh ? `已同步 ${t}` : `已同步 ${t} · 自动刷新已暂停`;
}

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

  const incomingBackendWs =
    typeof payload.workspaceFilter === "string" ? payload.workspaceFilter : "";
  state.backendWorkspaceFilter = incomingBackendWs;
  if (payload.workspaceOptions && payload.workspaceOptions.length > 0) {
    if (!incomingBackendWs) {
      state.knownWorkspaceOptions = recomputeWorkspaceLabels([...payload.workspaceOptions]);
    } else {
      const byKey = new Map<string, string>(
        state.knownWorkspaceOptions.map((o) => [o.key, o.label]),
      );
      for (const opt of payload.workspaceOptions) {
        if (!byKey.has(opt.key)) {
          byKey.set(opt.key, opt.label);
        }
      }
      state.knownWorkspaceOptions = recomputeWorkspaceLabels(
        Array.from(byKey.entries()).map(([key, label]) => ({ key, label })),
      );
    }
  }

  if (fromToolResult) {
    state.workspaceFilter = incomingBackendWs;
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
    if (state.bindingMode === "hook_bound") {
      if (
        payload.boundConversationId &&
        !matchesCid(payload.boundConversationId, state.hookBoundCid)
      ) {
        state.payload = {
          ...payload,
          boundConversationId: state.hookBoundCid,
          sessionNotFound: true,
          selectedSession: null,
        };
      }
    } else if (state.bindingMode === "user_selected") {
      if (payload.ambiguousPrefix) {
        state.bindingMode = "unbound";
        state.selectedCid = null;
      } else if (
        payload.boundConversationId &&
        !matchesCid(payload.boundConversationId, state.selectedCid)
      ) {
        state.payload = {
          ...payload,
          boundConversationId: state.selectedCid,
          sessionNotFound: true,
          selectedSession: null,
        };
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
  const reqWs = state.workspaceFilter;
  state.inFlight = true;
  if (trigger === "manual" || trigger === "select") {
    render();
  }

  const args: Record<string, unknown> = {};
  if (reqCid) {
    args.conversation_id = reqCid;
  }
  if (reqWs) {
    args.workspace_path = reqWs;
  }

  try {
    const res = await app.callServerTool({
      name: "next_step_panel_state",
      arguments: args,
    });
    const stillMatchesSelection =
      gen === state.refreshGeneration &&
      reqWs === state.workspaceFilter &&
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
      reqWs === state.workspaceFilter &&
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
      if (trigger === "poll") {
        renderPassive();
      } else {
        render();
      }
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

function renderSessionItemButton(
  s: SessionPanelItem,
  wsLabelMap?: Map<string, string>,
): HTMLButtonElement {
  const convName = (s.conversationName || "").trim();
  const baseCls = s.stale
    ? "ns-session-item stale"
    : s.waitingOnUser
      ? "ns-session-item waiting"
      : "ns-session-item";
  const cls = convName ? `${baseCls} has-name` : baseCls;
  const btn = el("button", cls);
  btn.type = "button";
  btn.onclick = () => selectSession(s.conversationId);

  const wsLabel =
    wsLabelMap?.get(getSessionWorkspaceKey(s)) || s.workspaceName;
  const top = el("div", "ns-session-top");
  if (convName) {
    const nameEl = el("span", "ns-session-name ns-ellipsis", convName);
    nameEl.title = `${convName} · ${wsLabel} (#${s.shortId})`;
    top.appendChild(nameEl);
  } else {
    const idEl = el("span", "ns-session-id ns-ellipsis", `#${s.shortId} · ${wsLabel}`);
    idEl.title = `#${s.shortId} · ${wsLabel}`;
    top.appendChild(idEl);
  }
  top.appendChild(statusChip(s));
  btn.appendChild(top);

  btn.appendChild(
    el(
      "div",
      s.nextAction ? "ns-session-action" : "ns-session-action ns-action-empty",
      s.nextAction || "当前无已声明的下一步",
    ),
  );

  const sub = el("div", "ns-session-sub");
  sub.appendChild(timeEl(s.updatedAtIso));
  if (convName) {
    const metaEl = el("span", "ns-session-meta ns-ellipsis", `${wsLabel} · #${s.shortId}`);
    metaEl.title = `${wsLabel} · #${s.shortId}`;
    sub.appendChild(metaEl);
  } else if (s.lastUserInput) {
    sub.appendChild(el("span", "ns-ellipsis", `输入：${s.lastUserInput}`));
  }
  btn.appendChild(sub);
  return btn;
}

function appendSessionGroup(
  wrap: HTMLElement,
  title: string,
  tone: Tone,
  items: SessionPanelItem[],
  wsLabelMap?: Map<string, string>,
): void {
  if (!items.length) return;
  const head = el("div", `ns-group-title ns-tone-${tone}`);
  const dot = el("span", "ns-dot");
  dot.setAttribute("aria-hidden", "true");
  head.appendChild(dot);
  head.appendChild(el("span", "", title));
  wrap.appendChild(head);
  for (const s of items) {
    wrap.appendChild(renderSessionItemButton(s, wsLabelMap));
  }
}

function renderSessionList(
  sessions: SessionPanelItem[],
  wsLabelMap?: Map<string, string>,
): HTMLElement {
  const wrap = el("div", "ns-session-list");
  if (!sessions.length) {
    if (
      (state.inFlight || state.pendingRefreshTrigger !== null) &&
      state.backendWorkspaceFilter !== state.workspaceFilter
    ) {
      wrap.appendChild(
        el("div", "ns-banner ns-banner-info", "正在加载所选工作区的会话状态..."),
      );
      return wrap;
    }
    const activeLabel =
      (state.workspaceFilter && wsLabelMap?.get(state.workspaceFilter)) ||
      (state.workspaceFilter === "__none__" ? "未关联工作区" : state.workspaceFilter);
    wrap.appendChild(
      el(
        "div",
        "ns-banner ns-banner-info",
        state.workspaceFilter
          ? `工作区「${activeLabel}」下暂无已记录的会话状态，可切换回「全部工作区」查看。`
          : "当前尚无已记录的会话状态。发送消息或调用 next_step_publish / next_step_side_panel 后会出现在这里。",
      ),
    );
    return wrap;
  }

  const waitingGroup: SessionPanelItem[] = [];
  const activeGroup: SessionPanelItem[] = [];
  const idleGroup: SessionPanelItem[] = [];
  for (const s of sessions) {
    if (s.waitingOnUser) {
      waitingGroup.push(s);
    } else if (
      s.stale ||
      s.lifecycle === "WORKING" ||
      s.lifecycle === "TOOL_RUNNING" ||
      s.lifecycle === "BACKGROUND_RUNNING"
    ) {
      activeGroup.push(s);
    } else {
      idleGroup.push(s);
    }
  }

  appendSessionGroup(wrap, `待我处理 (${waitingGroup.length})`, "waiting", waitingGroup, wsLabelMap);
  appendSessionGroup(wrap, `运行中 / 待核验 (${activeGroup.length})`, "running", activeGroup, wsLabelMap);

  if (idleGroup.length > 0) {
    const toggleBtn = el(
      "button",
      "ns-btn ns-idle-toggle",
      state.showIdleSessions
        ? `收起历史空闲会话 (${idleGroup.length})`
        : `展开历史空闲会话 (${idleGroup.length})`,
    );
    toggleBtn.type = "button";
    toggleBtn.onclick = () => {
      state.showIdleSessions = !state.showIdleSessions;
      render();
    };
    wrap.appendChild(toggleBtn);
    if (state.showIdleSessions) {
      appendSessionGroup(wrap, `历史空闲 (${idleGroup.length})`, "idle", idleGroup, wsLabelMap);
    }
  }
  return wrap;
}

function renderOptionRow(
  optText: string,
  copyBlocked: boolean,
  isStale: boolean,
  optHint: HTMLElement,
): HTMLElement {
  const row = el("div", "ns-opt-item");
  const span = el("span", "ns-opt-text", optText);
  const copyBtn = el("button", "ns-btn ns-btn-ghost", "复制选项");
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
  return row;
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

  const convName = (s.conversationName || "").trim();
  if (convName) {
    const head = el("div", "ns-card-head");
    const titleEl = el("div", "ns-card-title ns-ellipsis", convName);
    titleEl.title = `${convName} · ${s.workspaceName} (#${s.shortId})`;
    head.appendChild(titleEl);
    const subIdEl = el("span", "ns-card-sub ns-mono", `#${s.shortId}`);
    subIdEl.title = `会话 ID：${s.conversationId}`;
    head.appendChild(subIdEl);
    card.appendChild(head);
  }

  // Hero: answers "waiting on me? / who acts? / what next?" at a glance
  const hero = el("div", `ns-hero ns-tone-${isStale ? "stale" : sessionTone(s)}`);
  const heroTop = el("div", "ns-hero-top");
  heroTop.appendChild(statusChip(isStale ? { ...s, stale: true } : s));
  const actorText = s.actorLabel && s.actorLabel !== "—" ? s.actorLabel : "";
  if (actorText) {
    heroTop.appendChild(el("span", "ns-hero-actor", `执行者：${actorText}`));
  }
  hero.appendChild(heroTop);
  hero.appendChild(el("div", "ns-section-label", "下一步"));
  hero.appendChild(
    el(
      "p",
      s.nextAction ? "ns-action-text" : "ns-action-text ns-action-empty",
      s.nextAction || "当前无已声明的下一步（回合空闲或尚未发布，不作猜测）",
    ),
  );
  if (s.detail) {
    hero.appendChild(el("div", "ns-detail-text", `说明：${s.detail}`));
  }
  card.appendChild(hero);

  if (isStale) {
    card.appendChild(
      el(
        "div",
        "ns-banner ns-banner-warn",
        `过期警示：${s.staleReason || s.planArtifact?.staleReason || "关联计划文件已变更，旧续作指令已禁用复制"}`,
      ),
    );
  } else if (isUnverified) {
    card.appendChild(
      el(
        "div",
        "ns-banner ns-banner-warn",
        "新鲜度待核验：最近一次刷新失败，无法确认指令是否过期，已暂禁复制。请点击「刷新」恢复后再复制。",
      ),
    );
  }

  if (s.activeTool?.approvalHint) {
    card.appendChild(
      el("div", "ns-banner ns-banner-warn", `审批提示：${s.activeTool.approvalHint}`),
    );
  }

  const qList = s.questions && s.questions.length > 0 ? s.questions : [];
  if (qList.length > 1) {
    const optSection = el("div", "ns-options");
    optSection.appendChild(
      el(
        "div",
        "ns-section-label",
        `待你选择（共 ${qList.length} 个问题）`,
      ),
    );
    const optHint = el("div", "ns-copy-hint");
    for (let i = 0; i < qList.length; i++) {
      const qItem = qList[i];
      const qBlock = el("div", "ns-q-block");
      const qTitle = `Q${i + 1}. ${qItem.question}${qItem.multiSelect ? "（可多选）" : ""}`;
      qBlock.appendChild(el("div", "ns-q-title", qTitle));
      for (const optText of qItem.options || []) {
        qBlock.appendChild(renderOptionRow(optText, copyBlocked, isStale, optHint));
      }
      optSection.appendChild(qBlock);
    }
    optSection.appendChild(optHint);
    card.appendChild(optSection);
  } else if (s.options && s.options.length > 0) {
    const optSection = el("div", "ns-options");
    optSection.appendChild(
      el(
        "div",
        "ns-section-label",
        `待你选择（${s.options.length} 项）`,
      ),
    );
    const optHint = el("div", "ns-copy-hint");
    for (const optText of s.options) {
      optSection.appendChild(renderOptionRow(optText, copyBlocked, isStale, optHint));
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
            : "建议续作指令（复制后由你决定是否发送）",
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
  if (s.lastUserInput) {
    foot.appendChild(el("div", "ns-foot-input", `最近输入：${s.lastUserInput}`));
  }
  const footTop = el("div", "ns-foot-row");
  footTop.appendChild(el("span", "ns-ellipsis", `${s.workspaceName} · #${s.shortId}`));
  footTop.appendChild(timeEl(s.updatedAtIso));
  foot.appendChild(footTop);
  foot.appendChild(
    el(
      "div",
      "",
      `来源：${s.sourceLabel}（${s.confidence || "UNKNOWN"}）· 状态码 ${s.lifecycle}`,
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
  titleRow.appendChild(el("span", "ns-ver", state.payload?.version ? `v${state.payload.version}` : "v0.4.0"));
  const waitingCount = state.payload?.waitingCount ?? 0;
  if (waitingCount > 0) {
    const badge = el("span", "ns-count-badge", `${waitingCount} 待处理`);
    badge.title = `${waitingCount} 个会话在等你 / 共 ${state.payload?.totalSessions ?? state.payload?.sessions.length ?? 0} 个`;
    titleRow.appendChild(badge);
  }
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
    announce("MCP Bridge 连接失败");
    syncLabelEl = null;
    lastRenderSig = renderSignature();
    lastCopyGateSig = copyGateSignature();
    deferredRender = false;
    return;
  }

  if (state.lastError) {
    container.appendChild(el("div", "ns-banner ns-banner-warn", state.lastError));
  }

  const sessions = state.payload?.sessions ?? [];
  const wsOptions: WorkspaceOption[] = [];
  const seenWsKeys = new Set<string>();
  const addWsOpt = (key: string, label: string): void => {
    if (!key || seenWsKeys.has(key)) return;
    seenWsKeys.add(key);
    wsOptions.push({ key, label });
  };
  if (state.knownWorkspaceOptions.length > 0 || (state.payload?.workspaceOptions && state.payload.workspaceOptions.length > 0)) {
    for (const opt of state.knownWorkspaceOptions) {
      addWsOpt(opt.key, opt.label);
    }
    for (const opt of state.payload?.workspaceOptions ?? []) {
      addWsOpt(opt.key, opt.label);
    }
  } else if (state.payload?.workspaces && state.payload.workspaces.length > 0) {
    for (const w of state.payload.workspaces) {
      addWsOpt(w, w);
    }
  } else {
    const keyCounts = new Map<string, number>();
    const rawKeys = Array.from(new Set(sessions.map((s) => getSessionWorkspaceKey(s))));
    for (const k of rawKeys) {
      if (k === "__none__") continue;
      const nm = sessions.find((s) => getSessionWorkspaceKey(s) === k)?.workspaceName || k;
      keyCounts.set(nm, (keyCounts.get(nm) ?? 0) + 1);
    }
    for (const k of rawKeys.sort()) {
      if (k === "__none__") continue;
      const nm = sessions.find((s) => getSessionWorkspaceKey(s) === k)?.workspaceName || k;
      addWsOpt(k, (keyCounts.get(nm) ?? 0) > 1 ? `${nm} (${k})` : nm);
    }
    if (rawKeys.includes("__none__")) {
      addWsOpt("__none__", "未关联工作区");
    }
  }
  if (state.workspaceFilter && !seenWsKeys.has(state.workspaceFilter)) {
    addWsOpt(
      state.workspaceFilter,
      state.workspaceFilter === "__none__" ? "未关联工作区" : state.workspaceFilter,
    );
  }
  const hasCanonicalWsOpts =
    state.knownWorkspaceOptions.length > 0 ||
    Boolean(state.payload?.workspaceOptions && state.payload.workspaceOptions.length > 0);
  if (hasCanonicalWsOpts) {
    const disambiguated = recomputeWorkspaceLabels(wsOptions);
    wsOptions.splice(0, wsOptions.length, ...disambiguated);
  }
  const wsLabelMap = new Map(wsOptions.map((o) => [o.key, o.label]));
  const visibleSessions = state.workspaceFilter
    ? sessions.filter((s) => sessionMatchesWorkspace(s, state.workspaceFilter))
    : sessions;

  const curPayload = state.payload;
  const matchedNotFound = Boolean(
    curPayload?.sessionNotFound && matchesCid(curPayload.boundConversationId, state.selectedCid),
  );
  const matchedSelectedSession =
    curPayload?.selectedSession && matchesCid(curPayload.selectedSession.conversationId, state.selectedCid)
      ? curPayload.selectedSession
      : null;
  const activeName = resolveSelectedSessionName(
    matchedSelectedSession,
    sessions,
    state.selectedCid,
  );

  const bindingBar = el("div", "ns-binding-bar");
  const bindingTop = el("div", "ns-binding-top");

  if (state.bindingMode === "hook_bound" && state.selectedCid) {
    const shortCid = state.selectedCid.slice(0, 8);
    if (activeName) {
      const pill = el("span", "ns-bind-pill bound ns-ellipsis", `已绑定：${clipLabel(activeName, 20)} · #${shortCid}`);
      pill.title = `已绑定当前会话：${activeName} (#${shortCid})`;
      bindingTop.appendChild(pill);
    } else {
      bindingTop.appendChild(
        el("span", "ns-bind-pill bound", `已绑定当前会话 #${shortCid}`),
      );
    }
  } else if (state.bindingMode === "user_selected" && state.selectedCid) {
    const shortCid = state.selectedCid.slice(0, 8);
    if (activeName) {
      const pill = el("span", "ns-bind-pill selected ns-ellipsis", `已选定：${clipLabel(activeName, 20)} · #${shortCid}`);
      pill.title = `已手动选定：${activeName} (#${shortCid})`;
      bindingTop.appendChild(pill);
    } else {
      bindingTop.appendChild(
        el("span", "ns-bind-pill selected", `已手动选定 #${shortCid}`),
      );
    }
  } else {
    bindingTop.appendChild(
      el("span", "ns-bind-pill unbound", "未绑定会话"),
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
        ? `选择要查看的会话（共 ${visibleSessions.length} 个）`
        : `切换到其他会话（共 ${visibleSessions.length} 个）`,
    );
    placeholder.value = "";
    select.appendChild(placeholder);

    for (const s of visibleSessions) {
      const opt = document.createElement("option");
      opt.value = s.conversationId;
      const waitTag = s.stale ? "🟡" : s.waitingOnUser ? "🔴" : "⚪";
      const wsLbl = wsLabelMap.get(getSessionWorkspaceKey(s)) || s.workspaceName;
      const convName = (s.conversationName || "").trim();
      opt.textContent = convName
        ? `${waitTag} ${clipLabel(convName, 44)} · ${wsLbl} (#${s.shortId})`
        : `${waitTag} #${s.shortId} (${wsLbl}) - ${s.nextAction || "空闲"}`;
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

  if (wsOptions.length > 1 || state.workspaceFilter || state.backendWorkspaceFilter) {
    const wsSelect = el("select", "ns-select ns-ws-select");
    wsSelect.setAttribute("aria-label", "按工作区筛选会话");
    const allOpt = el("option", "", `全部工作区 (${sessions.length})`);
    allOpt.value = "";
    if (!state.workspaceFilter) allOpt.selected = true;
    wsSelect.appendChild(allOpt);
    for (const wsOpt of wsOptions) {
      const count = sessions.filter((s) => sessionMatchesWorkspace(s, wsOpt.key)).length;
      const showCount = !state.backendWorkspaceFilter || wsOpt.key === state.backendWorkspaceFilter;
      const baseLabel = wsOpt.key === "__none__" ? "未关联工作区" : `工作区: ${wsOpt.label}`;
      const opt = el("option", "", showCount ? `${baseLabel} (${count})` : baseLabel);
      opt.value = wsOpt.key;
      if (state.workspaceFilter === wsOpt.key) opt.selected = true;
      wsSelect.appendChild(opt);
    }
    wsSelect.onchange = () => {
      const nextVal = wsSelect.value;
      const needsServerFetch =
        Boolean(state.backendWorkspaceFilter) && nextVal !== state.backendWorkspaceFilter;
      state.workspaceFilter = nextVal;
      if (needsServerFetch) {
        state.refreshGeneration++;
        void refreshFromServer("select");
      } else {
        render();
      }
    };
    bindingBar.appendChild(wsSelect);
  }
  container.appendChild(bindingBar);

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
        "未收到当前会话的绑定信息，请从下方选择要查看的会话。",
      ),
    );
    container.appendChild(renderSessionList(visibleSessions, wsLabelMap));
  } else if (matchedNotFound) {
    const missingCid = state.selectedCid || curPayload?.boundConversationId || "未知";
    container.appendChild(
      el(
        "div",
        "ns-banner ns-banner-warn",
        `已绑定会话 #${missingCid.slice(0, 8)}，但后端尚无该会话的状态记录（尚未触发 Hook 或发布下一步），未越权展示其他会话数据。`,
      ),
    );
    if (visibleSessions.length > 0) {
      container.appendChild(el("div", "ns-section-label", "其他已记录会话（需点击才会切换）："));
      container.appendChild(renderSessionList(visibleSessions, wsLabelMap));
    }
  } else if (matchedSelectedSession) {
    container.appendChild(renderSessionDetail(matchedSelectedSession));
  } else if (state.selectedCid) {
    const isLoadingCid = state.inFlight || state.pendingRefreshTrigger !== null;
    const shortCid = state.selectedCid.slice(0, 8);
    container.appendChild(
      el(
        "div",
        isLoadingCid ? "ns-banner ns-banner-info" : "ns-banner ns-banner-warn",
        isLoadingCid
          ? activeName
            ? `正在加载会话「${clipLabel(activeName, 30)}」(#${shortCid}) 的最新状态...`
            : `正在加载会话 #${shortCid} 的最新状态...`
          : activeName
            ? `尚未获取到会话「${clipLabel(activeName, 30)}」(#${shortCid}) 的状态（已隐藏其他会话内容以防跨会话混淆，请点击「刷新」重试）。`
            : `尚未获取到会话 #${shortCid} 的状态（已隐藏其他会话内容以防跨会话混淆，请点击「刷新」重试）。`,
      ),
    );
  } else {
    container.appendChild(renderSessionList(visibleSessions, wsLabelMap));
  }

  const statusLine = el("div", "ns-status-line");
  syncLabelEl = el("span", "", formatSyncLabel());
  if (state.lastSyncedIso) {
    syncLabelEl.title = formatSyncTitle();
  }
  statusLine.appendChild(syncLabelEl);
  container.appendChild(statusLine);

  rootEl.appendChild(container);
  lastRenderSig = renderSignature();
  lastCopyGateSig = copyGateSignature();
  deferredRender = false;

  announce(liveMessage(matchedSelectedSession, waitingCount));
}

function liveMessage(sel: SessionPanelItem | null, waitingCount: number): string {
  if (state.loading) return "";
  if (sel) {
    const base = `${shortStatusLabel(sel)}${sel.nextAction ? `：${sel.nextAction}` : ""}`;
    const staleBlocked = Boolean(sel.stale || sel.planArtifact?.stale || sel.copyBlocked);
    if (state.lastError) return `${base}。最近一次刷新失败，已暂禁复制`;
    if (staleBlocked) return `${base}。指令已过期，已禁止复制`;
    return base;
  }
  if (state.lastError) return `刷新失败：${state.lastError}`;
  return waitingCount > 0 ? `${waitingCount} 个会话在等你处理` : "当前没有待处理会话";
}

// Register handlers BEFORE app.connect() so initial tool input/result/context are never missed
app.ontoolinput = () => {
  cancelPendingPoll();
  invalidateHostBinding();
  state.backendWorkspaceFilter = "";
  state.workspaceFilter = "";
  state.knownWorkspaceOptions = [];
  state.showIdleSessions = false;
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

document.addEventListener("focusout", () => {
  // Let focus settle on the next element before deciding whether the user is done
  window.setTimeout(flushDeferredRender, 0);
});
document.addEventListener("selectionchange", flushDeferredRender);

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
