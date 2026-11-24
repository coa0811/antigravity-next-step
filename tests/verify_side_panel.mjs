import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const htmlPath = new URL("../ui/side-panel.html", import.meta.url);
const html = readFileSync(htmlPath, "utf8");
const match = html.match(/<script>\n([\s\S]*?)\n<\/script>\n<\/body>/);
assert.ok(match, "Could not extract inline <script> from ui/side-panel.html");
const scriptCode = match[1];

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.id = "";
    this.type = "";
    this.value = "";
    this.title = "";
    this.disabled = false;
    this.selected = false;
    this.attributes = new Map();
    this.dataset = {};
    this.style = {
      _props: new Map(),
      setProperty(k, v) {
        this._props.set(k, String(v));
      },
    };
    this.onclick = null;
    this.onchange = null;
    this._text = "";
  }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this._text + this.children.map((c) => c.textContent).join("\n");
  }

  set textContent(val) {
    this._text = val == null ? "" : String(val);
    this.children = [];
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) {
      this.children.splice(idx, 1);
      child.parentNode = null;
    }
    return child;
  }

  setAttribute(name, val) {
    this.attributes.set(name, String(val));
  }

  select() {}

  contains(node) {
    for (let cur = node; cur; cur = cur.parentNode) {
      if (cur === this) return true;
    }
    return false;
  }

  findAll(pred) {
    const out = [];
    const visit = (node) => {
      if (pred(node)) out.push(node);
      for (const c of node.children) visit(c);
    };
    visit(this);
    return out;
  }
}

const appRoot = new FakeElement("div");
appRoot.id = "app";
const docElement = new FakeElement("html");
const docHead = new FakeElement("head");
const docBody = new FakeElement("body");
docBody.appendChild(appRoot);

const windowListeners = new Map();
const docListeners = new Map();
const visibilityState = "visible";

function addListener(map, type, fn) {
  const list = map.get(type) || [];
  list.push(fn);
  map.set(type, list);
}

function removeListener(map, type, fn) {
  const list = map.get(type) || [];
  const idx = list.indexOf(fn);
  if (idx >= 0) list.splice(idx, 1);
}

function dispatchTo(map, type, eventObj = {}) {
  const list = [...(map.get(type) || [])];
  for (const fn of list) {
    fn(eventObj);
  }
}

let inFlightRpc = 0;
let maxConcurrentRpc = 0;
const pendingToolCalls = [];
const recordedToolCalls = [];

const parentWindow = {
  postMessage(msg, _targetOrigin) {
    if (!msg || msg.jsonrpc !== "2.0" || msg.id === undefined) {
      return;
    }
    if (msg.method === "ui/initialize") {
      queueMicrotask(() => {
        dispatchTo(windowListeners, "message", {
          data: {
            jsonrpc: "2.0",
            id: msg.id,
            result: {
              protocolVersion: "2026-01-26",
              hostInfo: { name: "antigravity-test-host", version: "2.34.0" },
              hostCapabilities: { serverTools: {} },
              hostContext: { theme: "light", styles: { variables: {} } },
            },
          },
          source: parentWindow,
          origin: "null",
        });
      });
      return;
    }
    if (msg.method === "tools/call") {
      inFlightRpc += 1;
      if (inFlightRpc > maxConcurrentRpc) maxConcurrentRpc = inFlightRpc;
      const callName = msg.params?.name;
      const callArgs = msg.params?.arguments || {};
      recordedToolCalls.push({
        id: msg.id,
        name: callName,
        arguments: { ...callArgs },
      });
      pendingToolCalls.push({
        id: msg.id,
        name: callName,
        arguments: { ...callArgs },
        resolve(result) {
          inFlightRpc -= 1;
          dispatchTo(windowListeners, "message", {
            data: { jsonrpc: "2.0", id: msg.id, result },
            source: parentWindow,
            origin: "null",
          });
        },
        reject(message) {
          inFlightRpc -= 1;
          dispatchTo(windowListeners, "message", {
            data: {
              jsonrpc: "2.0",
              id: msg.id,
              error: { code: -32603, message: message || "transport error" },
            },
            source: parentWindow,
            origin: "null",
          });
        },
      });
      return;
    }
    queueMicrotask(() => {
      dispatchTo(windowListeners, "message", {
        data: { jsonrpc: "2.0", id: msg.id, result: {} },
        source: parentWindow,
        origin: "null",
      });
    });
  },
};

const fakeDocument = {
  documentElement: docElement,
  head: docHead,
  body: docBody,
  get visibilityState() {
    return visibilityState;
  },
  getElementById(id) {
    return id === "app" ? appRoot : null;
  },
  createElement(tag) {
    return new FakeElement(tag);
  },
  createRange() {
    return { selectNodeContents() {} };
  },
  execCommand() {
    return false;
  },
  addEventListener(type, fn) {
    addListener(docListeners, type, fn);
  },
  removeEventListener(type, fn) {
    removeListener(docListeners, type, fn);
  },
};

// Controllable selection so tests can simulate "user has text selected inside the panel"
const fakeSelection = {
  isCollapsed: true,
  anchorNode: null,
  removeAllRanges() {},
  addRange() {},
};

let nextTimerId = 1;
const activeTimers = new Map();
function fakeSetTimeout(fn, delay) {
  const id = nextTimerId++;
  activeTimers.set(id, { fn, delay });
  return id;
}
function fakeClearTimeout(id) {
  activeTimers.delete(id);
}

const fakeWindow = {
  parent: parentWindow,
  top: parentWindow,
  document: fakeDocument,
  location: { href: "about:blank", origin: "null" },
  addEventListener(type, fn) {
    addListener(windowListeners, type, fn);
  },
  removeEventListener(type, fn) {
    removeListener(windowListeners, type, fn);
  },
  postMessage(msg, _origin) {
    dispatchTo(windowListeners, "message", {
      data: msg,
      source: parentWindow,
      origin: "null",
    });
  },
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  getSelection() {
    return fakeSelection;
  },
  matchMedia() {
    return { matches: false, addEventListener() {}, removeEventListener() {} };
  },
};
fakeWindow.window = fakeWindow;
fakeWindow.self = fakeWindow;

const quietConsole = {
  log() {},
  debug() {},
  info() {},
  warn: console.warn,
  error: console.error,
};

const fakeNavigator = {};
const context = vm.createContext({
  window: fakeWindow,
  self: fakeWindow,
  document: fakeDocument,
  navigator: fakeNavigator,
  console: quietConsole,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  URL,
  URLSearchParams,
  Map,
  Set,
  Promise,
  Error,
  TypeError,
  Object,
  Array,
  String,
  Number,
  Boolean,
  Math,
  Date,
  JSON,
  encodeURIComponent,
  decodeURIComponent,
});

function makeSessionItem(cid, nextAction, prompt, opts = {}) {
  return {
    conversationId: cid,
    shortId: cid.slice(0, 8),
    ...(opts.conversationName !== undefined ? { conversationName: opts.conversationName } : {}),
    workspaceName: opts.workspaceName || "repo-test",
    lifecycle: opts.stale ? "IDLE" : "WAITING_USER_DECISION",
    sourceType: opts.stale ? "STALE" : "AGENT_DECLARED",
    confidence: "HIGH",
    waitingOnUser: Boolean(!opts.stale),
    actor: opts.stale ? "none" : "user",
    actorLabel: opts.stale ? "—" : "用户",
    statusBadge: opts.stale ? "🟡 状态可能已过期 (Stale)" : "🔴 等我确认",
    sourceLabel: "Agent 显式声明（高可信）",
    nextAction,
    detail: opts.detail || `Detail for ${cid}`,
    options: opts.options || [],
    suggestedPrompt: prompt,
    copyBlocked: Boolean(opts.copyBlocked),
    stale: Boolean(opts.stale),
    staleReason: opts.staleReason || null,
    planArtifact: opts.planArtifact || null,
    activeTool: null,
    updatedAt: 1700000000,
    updatedAtIso: "2026-11-22T00:00:00Z",
  };
}

function makePayload({
  bindingMode = "unbound",
  boundConversationId = null,
  selectedSession = null,
  sessions = [],
  sessionNotFound = false,
  ambiguousPrefix = false,
}) {
  return {
    version: "0.3.0",
    generatedAtIso: "2026-11-22T00:00:01Z",
    bindingMode,
    boundConversationId,
    sessionNotFound,
    ambiguousPrefix,
    selectedSession,
    sessions,
    totalSessions: sessions.length,
    waitingCount: sessions.filter((s) => s.waitingOnUser).length,
  };
}

async function flushMicrotasks(rounds = 8) {
  for (let i = 0; i < rounds; i++) {
    await new Promise((r) => setImmediate(r));
  }
}

function sendHostNotification(method, params) {
  dispatchTo(windowListeners, "message", {
    data: { jsonrpc: "2.0", method, params },
    source: parentWindow,
    origin: "null",
  });
}

function renderedText() {
  return appRoot.textContent;
}

function findCards() {
  return appRoot.findAll(
    (n) => typeof n.className === "string" && n.className.split(/\s+/).includes("ns-card"),
  );
}

function findSessionListEl() {
  return appRoot.findAll(
    (n) => typeof n.className === "string" && n.className.split(/\s+/).includes("ns-session-list"),
  )[0];
}

function findButtonsByText(label) {
  return appRoot.findAll((n) => n.tagName === "BUTTON" && n._text === label);
}

const sessionA = makeSessionItem("cid-aaaa-1111", "Action A", "Prompt A", {
  options: ["Option A1"],
});
const sessionB = makeSessionItem("cid-bbbb-2222", "Action B", "Prompt B");
const sessionC = makeSessionItem("cid-cccc-3333", "Action C", "Prompt C", {
  options: ["Option C1"],
});
const sessionD = makeSessionItem("cid-dddd-4444", "Action D", "Old prompt", {
  stale: true,
  copyBlocked: true,
  staleReason: "计划文件 implementation_plan.md 内容已变更",
  planArtifact: {
    path: "/tmp/implementation_plan.md",
    name: "implementation_plan.md",
    sha256: "abcdef0123456789",
    stale: true,
    staleReason: "计划文件 implementation_plan.md 内容已变更",
  },
});
const allSessions = [sessionA, sessionB, sessionC, sessionD];

// Run compiled bundle inside VM
vm.runInContext(scriptCode, context);
await flushMicrotasks();

// 1. Startup {} remains strictly unbound and never leaks session details
assert.equal(pendingToolCalls.length, 1, "Expected initial bootstrap tools/call");
assert.equal(pendingToolCalls[0].name, "next_step_panel_state");
assert.deepEqual(pendingToolCalls[0].arguments, {});

pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "unbound" }],
  structuredContent: makePayload({
    bindingMode: "unbound",
    boundConversationId: null,
    selectedSession: null,
    sessions: allSessions,
  }),
});
await flushMicrotasks();

assert.equal(findCards().length, 0, "Unbound state rendered detail card");
assert.ok(renderedText().includes("未绑定会话"), "Unbound state pill missing");
assert.ok(!renderedText().includes("Prompt A"), "Unbound state leaked Prompt A");
assert.equal(findButtonsByText("复制指令").length, 0, "Unbound state rendered copy button");
console.log("[PASS] 1. Unbound {} state does not guess or leak session detail");

// Deliver host tool-result binding Session A
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "Session A" }],
  structuredContent: makePayload({
    bindingMode: "hook_bound",
    boundConversationId: "cid-aaaa-1111",
    selectedSession: sessionA,
    sessions: allSessions,
  }),
});
await flushMicrotasks();

assert.equal(findCards().length, 1, "Session A detail card missing");
assert.ok(findCards()[0].textContent.includes("Action A"), "Session A Action A missing in card");
assert.ok(findCards()[0].textContent.includes("Prompt A"), "Session A Prompt A missing in card");
assert.equal(findButtonsByText("复制指令").length, 1, "Session A copy button missing");

// 2. P1 Cross-session isolation on switch pending and error
const selectEl = appRoot.findAll((n) => n.tagName === "SELECT")[0];
assert.ok(selectEl, "Session <select> element missing");
selectEl.value = "cid-bbbb-2222";
selectEl.onchange();
await flushMicrotasks();

assert.equal(pendingToolCalls.length, 1, "Selecting Session B did not invoke next_step_panel_state");
assert.deepEqual(pendingToolCalls[0].arguments, { conversation_id: "cid-bbbb-2222" });

const loadingText = renderedText();
assert.ok(loadingText.includes("正在加载会话 #cid-bbbb"), `Expected loading banner for B, got: ${loadingText}`);
assert.equal(findCards().length, 0, "P1 FAIL: Detail card for A remained rendered while switching to B");
assert.ok(!loadingText.includes("Prompt A"), "P1 FAIL: Prompt A leaked while switching to B");
assert.equal(findButtonsByText("复制指令").length, 0, "P1 FAIL: Copy button remained while switching to B");

pendingToolCalls.shift().resolve({
  isError: true,
  content: [{ type: "text", text: "simulated RPC error for B" }],
});
await flushMicrotasks();

const errText = renderedText();
assert.ok(errText.includes("状态查询失败"), "Expected tool failure banner for B");
assert.ok(errText.includes("已隐藏其他会话内容以防跨会话混淆"), "Expected isolation notice when B fails to load");
assert.equal(findCards().length, 0, "P1 FAIL: Detail card for A remained rendered after B error");
assert.ok(!errText.includes("Prompt A"), "P1 FAIL: Prompt A leaked after B error");
assert.equal(findButtonsByText("复制指令").length, 0, "P1 FAIL: Copy button remained after B error");
console.log("[PASS] 2. P1 Cross-session isolation on switch-pending and switch-error");

// 3. P2 Single-flight & coalescing across visibilitychange + rapid B -> C switch
const selectB = appRoot.findAll((n) => n.tagName === "SELECT")[0];
selectB.value = "cid-bbbb-2222";
selectB.onchange();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
const slowCallB = pendingToolCalls.shift();

maxConcurrentRpc = inFlightRpc;
dispatchTo(docListeners, "visibilitychange");
dispatchTo(docListeners, "visibilitychange");

const curSelect = appRoot.findAll((n) => n.tagName === "SELECT")[0];
curSelect.value = "cid-cccc-3333";
curSelect.onchange();
await flushMicrotasks();

assert.equal(pendingToolCalls.length, 0, "P2 FAIL: Concurrent tools/call started while B was in-flight");
assert.equal(maxConcurrentRpc, 1, `P2 FAIL: peak concurrency=${maxConcurrentRpc}`);

slowCallB.resolve({
  content: [{ type: "text", text: "Session B" }],
  structuredContent: makePayload({
    bindingMode: "explicit",
    boundConversationId: "cid-bbbb-2222",
    selectedSession: sessionB,
    sessions: allSessions,
  }),
});
await flushMicrotasks();

assert.equal(pendingToolCalls.length, 1, "P2 FAIL: Expected coalesced follow-up call for C");
assert.deepEqual(pendingToolCalls[0].arguments, { conversation_id: "cid-cccc-3333" });
assert.equal(findCards().length, 0, "Stale response for B must not render card over C");

pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "Session C" }],
  structuredContent: makePayload({
    bindingMode: "explicit",
    boundConversationId: "cid-cccc-3333",
    selectedSession: sessionC,
    sessions: allSessions,
  }),
});
await flushMicrotasks();

assert.equal(maxConcurrentRpc, 1);
assert.equal(findCards().length, 1);
assert.ok(findCards()[0].textContent.includes("Action C"));
assert.ok(findCards()[0].textContent.includes("Prompt C"));
console.log("[PASS] 3. P2 Single-flight concurrency == 1 and coalesced final refresh renders Session C");

// 4. Refresh error MUST block copying unverified cached prompt/options until next successful verify
const refreshBtn1 = findButtonsByText("刷新")[0];
assert.ok(refreshBtn1, "Refresh button missing");
refreshBtn1.onclick();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
pendingToolCalls.shift().reject("simulated network disconnect");
await flushMicrotasks();

assert.ok(renderedText().includes("刷新异常"), "Expected refresh error banner");
assert.ok(renderedText().includes("新鲜度待核验"), "Expected unverified freshness banner inside detail card");
assert.equal(findButtonsByText("复制指令").length, 0, "Refresh-error FAIL: Enabled copy button remained after failed refresh");
const unverifiedBtns = findButtonsByText("待核验 · 暂禁复制");
assert.equal(unverifiedBtns.length, 1, "Expected '待核验 · 暂禁复制' button after failed refresh");
assert.equal(unverifiedBtns[0].disabled, true, "Unverified copy button must be disabled");
const optBtnsOnErr = findButtonsByText("复制选项");
assert.equal(optBtnsOnErr.length, 1);
assert.equal(optBtnsOnErr[0].disabled, true, "Option copy button must be disabled while freshness is unverified");

// Also test recovery via successful refresh, and check if recovered state is stale it remains blocked
const refreshBtn2 = findButtonsByText("刷新")[0];
refreshBtn2.onclick();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "Session C fresh" }],
  structuredContent: makePayload({
    bindingMode: "explicit",
    boundConversationId: "cid-cccc-3333",
    selectedSession: sessionC,
    sessions: allSessions,
  }),
});
await flushMicrotasks();
assert.equal(findButtonsByText("复制指令").length, 1, "Copy button did not recover after fresh refresh");
assert.equal(findButtonsByText("复制选项")[0].disabled, false);

// Now refresh returns isError: true again (e.g., server tool error) -> must disable copy again
findButtonsByText("刷新")[0].onclick();
await flushMicrotasks();
pendingToolCalls.shift().resolve({
  isError: true,
  content: [{ type: "text", text: "server read error" }],
});
await flushMicrotasks();
assert.equal(findButtonsByText("复制指令").length, 0);
assert.equal(findButtonsByText("待核验 · 暂禁复制")[0].disabled, true);

// 4b: Codex counter-example — bind A -> refresh fails -> unbind -> while unbind RPC is in-flight,
// re-select A via "回到原绑定会话" or <select>; old prompt & copy button MUST stay absent until fresh A returns.
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "Session A" }],
  structuredContent: makePayload({
    bindingMode: "hook_bound",
    boundConversationId: "cid-aaaa-1111",
    selectedSession: sessionA,
    sessions: allSessions,
  }),
});
await flushMicrotasks();
assert.equal(findButtonsByText("复制指令").length, 1);

findButtonsByText("刷新")[0].onclick();
await flushMicrotasks();
pendingToolCalls.shift().reject("network error before unbind");
await flushMicrotasks();
assert.equal(findButtonsByText("待核验 · 暂禁复制")[0].disabled, true);

const unbindBtn = findButtonsByText("查看全部 / 解绑")[0];
assert.ok(unbindBtn, "Unbind button missing");
unbindBtn.onclick();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1, "Expected in-flight unbind RPC");
assert.equal(inFlightRpc, 1);

// Click "回到原绑定会话" while unbind RPC is still pending
const restoreBtn = findButtonsByText("回到原绑定会话")[0];
assert.ok(restoreBtn, "Restore hook-bound button missing");
restoreBtn.onclick();
await flushMicrotasks();

assert.equal(inFlightRpc, 1, "4b FAIL: Concurrent RPC launched during unbind");
assert.equal(findCards().length, 0, "4b FAIL: Cached Session A detail card reappeared before fresh response");
assert.equal(findButtonsByText("复制指令").length, 0, "4b FAIL: Copy button re-enabled before fresh response");
assert.ok(!renderedText().includes("Prompt A"), "4b FAIL: Old Prompt A leaked across unbind -> reselect");
assert.ok(renderedText().includes("刷新异常"), "4b FAIL: Refresh error notice prematurely cleared before recovery");
assert.ok(renderedText().includes("正在加载会话 #cid-aaaa"), "4b FAIL: Expected loading state for coalesced A select");

// Also verify manual <select> re-selection while still waiting keeps cache clear
const selRe = appRoot.findAll((n) => n.tagName === "SELECT")[0];
selRe.value = "cid-aaaa-1111";
selRe.onchange();
await flushMicrotasks();
assert.equal(findCards().length, 0, "4b FAIL: <select> re-select leaked cached Session A card");
assert.equal(findButtonsByText("复制指令").length, 0, "4b FAIL: <select> re-select enabled copy button");

// Resolve stale unbind RPC -> coalesced fetch for A fires; still no card until A responds
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "unbound" }],
  structuredContent: makePayload({
    bindingMode: "unbound",
    boundConversationId: null,
    selectedSession: null,
    sessions: allSessions,
  }),
});
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1, "4b FAIL: Expected coalesced follow-up fetch for Session A");
assert.deepEqual(pendingToolCalls[0].arguments, { conversation_id: "cid-aaaa-1111" });
assert.equal(findCards().length, 0, "4b FAIL: Session A card appeared before Session A RPC resolved");

// Now resolve Session A fetch cleanly -> fresh A renders and copy re-enables
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "Session A recovered" }],
  structuredContent: makePayload({
    bindingMode: "explicit",
    boundConversationId: "cid-aaaa-1111",
    selectedSession: sessionA,
    sessions: allSessions,
  }),
});
await flushMicrotasks();
assert.equal(findCards().length, 1);
assert.equal(findButtonsByText("复制指令").length, 1);
console.log("[PASS] 4. Refresh failure & unbind/re-select blocks stale copy until fresh backend verification");

// 5. New host tool-input / error / cancel must invalidate prior hook_bound & copyable prompt
function bindSessionAFromHost() {
  sendHostNotification("ui/notifications/tool-result", {
    content: [{ type: "text", text: "Session A" }],
    structuredContent: makePayload({
      bindingMode: "hook_bound",
      boundConversationId: "cid-aaaa-1111",
      selectedSession: sessionA,
      sessions: allSessions,
    }),
  });
}

// 5a: Re-bind A -> send new tool-input {} -> old binding & copy button immediately invalidated
bindSessionAFromHost();
await flushMicrotasks();
assert.ok(renderedText().includes("已绑定当前会话 #cid-aaaa"));
assert.equal(findButtonsByText("复制指令").length, 1);

sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
assert.ok(!renderedText().includes("已绑定当前会话"), "FAIL 5a: Old '已绑定当前会话' survived new tool-input");
assert.equal(findCards().length, 0, "FAIL 5a: Old detail card survived new tool-input");
assert.equal(findButtonsByText("复制指令").length, 0, "FAIL 5a: Old copy button survived new tool-input");

// Deliver unbound {} tool-result -> lands cleanly in unbound
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "unbound" }],
  structuredContent: makePayload({
    bindingMode: "unbound",
    boundConversationId: null,
    selectedSession: null,
    sessions: allSessions,
  }),
});
await flushMicrotasks();
assert.ok(renderedText().includes("未绑定会话"));
assert.equal(findCards().length, 0);

// 5b: Re-bind A -> new tool-input {} -> tool-result isError -> must remain unbound and not copyable
bindSessionAFromHost();
await flushMicrotasks();
assert.ok(renderedText().includes("已绑定当前会话 #cid-aaaa"));
sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  isError: true,
  content: [{ type: "text", text: "new tool invocation failed" }],
});
await flushMicrotasks();
assert.ok(renderedText().includes("new tool invocation failed"));
assert.ok(!renderedText().includes("已绑定当前会话"), "FAIL 5b: Old binding remained after tool-result error");
assert.ok(renderedText().includes("未绑定会话"), "FAIL 5b: Expected unbound fallback after tool-result error");
assert.equal(findCards().length, 0, "FAIL 5b: Detail card remained after tool-result error");
assert.equal(findButtonsByText("复制指令").length, 0);

// 5c: Re-bind A -> new tool-cancelled -> must invalidate old binding & copyable prompt
bindSessionAFromHost();
await flushMicrotasks();
assert.ok(renderedText().includes("已绑定当前会话 #cid-aaaa"));
sendHostNotification("ui/notifications/tool-cancelled", { reason: "user cancelled" });
await flushMicrotasks();
assert.ok(renderedText().includes("工具调用已取消"));
assert.ok(!renderedText().includes("已绑定当前会话"), "FAIL 5c: Old binding remained after tool-cancelled");
assert.ok(renderedText().includes("未绑定会话"), "FAIL 5c: Expected unbound fallback after tool-cancelled");
assert.equal(findCards().length, 0, "FAIL 5c: Detail card remained after tool-cancelled");
assert.equal(findButtonsByText("复制指令").length, 0);
console.log("[PASS] 5. New host tool-input / tool-result(error) / tool-cancelled invalidates prior session binding");

// 6. Stale plan disables prompt copy cleanly
const selectD = appRoot.findAll((n) => n.tagName === "SELECT")[0];
selectD.value = "cid-dddd-4444";
selectD.onchange();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "Session D" }],
  structuredContent: makePayload({
    bindingMode: "explicit",
    boundConversationId: "cid-dddd-4444",
    selectedSession: sessionD,
    sessions: allSessions,
  }),
});
await flushMicrotasks();

const blockedBtns = findButtonsByText("已过期 · 禁止复制");
assert.equal(blockedBtns.length, 1);
assert.equal(blockedBtns[0].disabled, true);
assert.equal(findButtonsByText("复制指令").length, 0);
console.log("[PASS] 6. Stale plan disables prompt copying ('已过期 · 禁止复制', disabled=true)");

// 7. Grouped overview (collapsing idle sessions), workspace filtering without auto-binding, & multi-question rendering
const waitAlpha = makeSessionItem("cid-wait-alpha", "Answer Alpha", "Prompt Alpha", {
  workspaceName: "repo-alpha",
});
const workAlpha = {
  ...makeSessionItem("cid-work-alpha", "Working Alpha", null, { workspaceName: "repo-alpha" }),
  lifecycle: "WORKING",
  waitingOnUser: false,
  statusBadge: "🟢 Agent 运行中 (WORKING)",
  actor: "agent",
  actorLabel: "Agent（主会话）",
};
const idleAlpha = {
  ...makeSessionItem("cid-idle-alpha", null, null, { workspaceName: "repo-alpha" }),
  lifecycle: "IDLE",
  sourceType: "NONE",
  waitingOnUser: false,
  statusBadge: "⚪ 会话空闲（无挂起待办）",
  actor: "none",
  actorLabel: "—",
};
const waitBeta = makeSessionItem("cid-wait-beta", "Answer Beta", "Prompt Beta", {
  workspaceName: "repo-beta",
});
const multiQSession = {
  ...makeSessionItem("cid-multi-q", "在聊天框回答选择题：选择环境（共 2 个问题）", null, {
    workspaceName: "repo-alpha",
  }),
  sourceType: "APPROVAL_REQUIRED",
  lifecycle: "WAITING_USER_DECISION",
  options: ["[Q1] Dev", "[Q1] Prod", "[Q2] Skip tests", "[Q2] Full tests"],
  questions: [
    { question: "选择目标环境", options: ["Dev", "Prod"], multiSelect: false },
    { question: "选择测试档位", options: ["Skip tests", "Full tests"], multiSelect: true },
  ],
};
const mixedSessions = [waitAlpha, workAlpha, idleAlpha, waitBeta, multiQSession];

// Reset to unbound with mixed multi-workspace sessions
sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "unbound mixed" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: mixedSessions,
    }),
    workspaces: ["repo-alpha", "repo-beta"],
  },
});
await flushMicrotasks();

// Idle session should be collapsed initially inside .ns-session-list behind "展开历史空闲会话 (1)"
const listCollapsed = findSessionListEl();
assert.ok(listCollapsed, "Session list container missing");
assert.ok(listCollapsed.textContent.includes("待我处理 ("), "Missing waiting group title");
assert.ok(listCollapsed.textContent.includes("运行中 / 待核验 ("), "Missing active group title");
assert.ok(!listCollapsed.textContent.includes("#cid-idle"), "Idle session should be hidden while collapsed");
const toggleIdleBtn = findButtonsByText("展开历史空闲会话 (1)")[0];
assert.ok(toggleIdleBtn, "Expected idle session collapse toggle button");
toggleIdleBtn.onclick();
await flushMicrotasks();
assert.ok(findSessionListEl().textContent.includes("#cid-idle"), "Idle session should appear after expanding toggle");

// Filtering by workspace "repo-beta" must narrow list WITHOUT binding or showing copy buttons
const wsSelect = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
assert.ok(wsSelect, "Workspace filter <select> missing");
wsSelect.value = "repo-beta";
wsSelect.onchange();
await flushMicrotasks();

assert.ok(renderedText().includes("未绑定会话"), "Workspace filter must NEVER auto-bind");
assert.equal(findCards().length, 0, "Workspace filter must NOT render detail card");
const filteredList = findSessionListEl();
assert.ok(filteredList.textContent.includes("Answer Beta"), "Filtered repo-beta session missing");
assert.ok(!filteredList.textContent.includes("Working Alpha"), "repo-alpha session should be filtered out");

// Clear filter back to all workspaces before checking multi-question detail
const wsSelect2 = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
wsSelect2.value = "";
wsSelect2.onchange();
await flushMicrotasks();

// Select multiQSession and confirm both Q1 & Q2 + all option copy buttons render and lock on refresh failure
const sessSelect7 = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "选择 Antigravity 会话",
)[0];
sessSelect7.value = "cid-multi-q";
sessSelect7.onchange();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "multi-q" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "explicit",
      boundConversationId: "cid-multi-q",
      selectedSession: multiQSession,
      sessions: mixedSessions,
    }),
    workspaces: ["repo-alpha", "repo-beta"],
  },
});
await flushMicrotasks();

assert.ok(renderedText().includes("Q1. 选择目标环境"), "Q1 title missing");
assert.ok(renderedText().includes("Q2. 选择测试档位（可多选）"), "Q2 title missing");
const multiOptBtns = findButtonsByText("复制选项");
assert.equal(multiOptBtns.length, 4, "Expected all 4 option copy buttons across Q1 and Q2");
assert.ok(multiOptBtns.every((b) => !b.disabled), "Option copy buttons should be enabled when verified");

findButtonsByText("刷新")[0].onclick();
await flushMicrotasks();
pendingToolCalls.shift().reject("refresh error on multi-q");
await flushMicrotasks();
assert.ok(
  findButtonsByText("复制选项").every((b) => b.disabled),
  "All multi-question option copy buttons must disable after refresh failure",
);
console.log("[PASS] 7. Grouped/collapsible idle list, workspace filter (no auto-bind), & multi-question copy controls");

// 8. Disambiguated workspace keys, caller workspace_path preserved across poll/manual refresh & cleared on All, idle-only collapse, & untruncated Q10 copy
const idleFooProj = {
  ...makeSessionItem("cid-fp01-idle", null, null, { workspaceName: "foo" }),
  workspaceKey: "/projects/foo",
  workspacePaths: ["/projects/foo"],
  lifecycle: "IDLE",
  sourceType: "NONE",
  waitingOnUser: false,
  statusBadge: "⚪ 会话空闲（无挂起待办）",
  actor: "none",
  actorLabel: "—",
};
const idleFooWork = {
  ...makeSessionItem("cid-fw02-idle", null, null, { workspaceName: "foo" }),
  workspaceKey: "/work/foo",
  workspacePaths: ["/work/foo"],
  lifecycle: "IDLE",
  sourceType: "NONE",
  waitingOnUser: false,
  statusBadge: "⚪ 会话空闲（无挂起待办）",
  actor: "none",
  actorLabel: "—",
};
const idleNoWs = {
  ...makeSessionItem("cid-nw03-idle", null, null, { workspaceName: "未关联工作区" }),
  workspaceKey: "__none__",
  workspacePaths: [],
  lifecycle: "IDLE",
  sourceType: "NONE",
  waitingOnUser: false,
  statusBadge: "⚪ 会话空闲（无挂起待办）",
  actor: "none",
  actorLabel: "—",
};
const disambigOpts = [
  { key: "/projects/foo", label: "foo (/projects/foo)" },
  { key: "/work/foo", label: "foo (/work/foo)" },
  { key: "__none__", label: "未关联工作区" },
];

// 8a. All idle sessions + caller workspace_path filter starts collapsed and retains workspace_path across poll & manual refresh
sendHostNotification("ui/notifications/tool-input", { arguments: { workspace_path: "/projects/foo" } });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "filtered idle" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj],
    }),
    workspaceFilter: "/projects/foo",
    workspaceOptions: [{ key: "/projects/foo", label: "foo (/projects/foo)" }],
  },
});
await flushMicrotasks();

assert.ok(renderedText().includes("未绑定会话"), "Idle-only view must stay unbound");
assert.ok(!findSessionListEl().textContent.includes("#cid-fp01"), "Idle-only list must be collapsed by default");
const idleOnlyBtn = findButtonsByText("展开历史空闲会话 (1)")[0];
assert.ok(idleOnlyBtn, "Idle-only view must show collapse toggle");
idleOnlyBtn.onclick();
await flushMicrotasks();
assert.ok(findSessionListEl().textContent.includes("#cid-fp01"), "Idle-only list should expand on click");
assert.ok(renderedText().includes("未绑定会话"), "Expanding idle group must not change binding");
const recollapseBtn = findButtonsByText("收起历史空闲会话 (1)")[0];
assert.ok(recollapseBtn, "Expected collapse button after expand");
recollapseBtn.onclick();
await flushMicrotasks();
assert.ok(!findSessionListEl().textContent.includes("#cid-fp01"), "Idle-only list should re-collapse cleanly");

// Auto-poll retains workspace_path
const pollTimer8 = Array.from(activeTimers.values()).find((t) => t.delay >= 3000);
assert.ok(pollTimer8, "Expected active poll timer for workspace retention test");
pollTimer8.fn();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
assert.deepEqual(pendingToolCalls[0].arguments, { workspace_path: "/projects/foo" });
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "poll filtered" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj, idleFooWork, idleNoWs],
    }),
    workspaceFilter: "/projects/foo",
    workspaceOptions: disambigOpts,
  },
});
await flushMicrotasks();

// Manual refresh also retains workspace_path
findButtonsByText("刷新")[0].onclick();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
assert.deepEqual(pendingToolCalls[0].arguments, { workspace_path: "/projects/foo" });
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "manual filtered" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj, idleFooWork, idleNoWs],
    }),
    workspaceFilter: "/projects/foo",
    workspaceOptions: disambigOpts,
  },
});
await flushMicrotasks();

// Check disambiguated workspace dropdown + distinct __none__ option vs ""
const wsSel8 = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
const wsVals8 = wsSel8.children.map((o) => o.value);
assert.ok(wsVals8.includes(""), "All-workspaces empty option missing");
assert.ok(wsVals8.includes("/projects/foo"), "Canonical /projects/foo option missing");
assert.ok(wsVals8.includes("/work/foo"), "Canonical /work/foo option missing");
assert.ok(wsVals8.includes("__none__"), "Distinct __none__ option missing");
assert.ok(wsSel8.children.some((o) => o._text.includes("foo (/projects/foo)")));
assert.ok(wsSel8.children.some((o) => o._text.includes("foo (/work/foo)")));

// Switching back to "" (全部工作区) clears backend workspace constraint and refreshes without workspace_path
wsSel8.value = "";
wsSel8.onchange();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1, "Clearing backend workspace filter should trigger refresh");
assert.deepEqual(pendingToolCalls[0].arguments, {});
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "all cleared" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj, idleFooWork, idleNoWs],
    }),
    workspaceFilter: null,
    workspaceOptions: disambigOpts,
  },
});
await flushMicrotasks();

// 8a-2. Selecting /work/foo from "All Workspaces" must NOT match parent /work and must retain /work/foo on manual refresh
const idleWorkParent = {
  ...makeSessionItem("cid-wk01-work", null, null, { workspaceName: "work" }),
  workspaceKey: "/work",
  workspacePaths: ["/work"],
  nextAction: "Parent Work Session",
  lifecycle: "IDLE",
  sourceType: "NONE",
  waitingOnUser: false,
  statusBadge: "⚪ 会话空闲（无挂起待办）",
  actor: "none",
  actorLabel: "—",
};
const disambigWithParent = [
  ...disambigOpts,
  { key: "/work", label: "work" },
];
sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "all with /work parent" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj, idleFooWork, idleWorkParent, idleNoWs],
    }),
    workspaceFilter: null,
    workspaceOptions: disambigWithParent,
  },
});
await flushMicrotasks();

const wsSelFromAll = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
wsSelFromAll.value = "/work/foo";
wsSelFromAll.onchange();
await flushMicrotasks();

const sessOptsWorkFoo = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "选择 Antigravity 会话",
)[0].children.map((o) => o.value);
assert.ok(sessOptsWorkFoo.includes("cid-fw02-idle"), "/work/foo session must appear under /work/foo filter");
assert.ok(!sessOptsWorkFoo.includes("cid-wk01-work"), "Parent /work session must NOT leak into /work/foo filter");

findButtonsByText("展开历史空闲会话 (1)")[0].onclick();
await flushMicrotasks();
const listWorkFoo = findSessionListEl().textContent;
assert.ok(listWorkFoo.includes("#cid-fw02"), "/work/foo session must appear in list");
assert.ok(!listWorkFoo.includes("#cid-wk01"), "Parent /work session must NOT appear in /work/foo list");
findButtonsByText("收起历史空闲会话 (1)")[0].onclick();
await flushMicrotasks();

findButtonsByText("刷新")[0].onclick();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1, "Manual refresh after dropdown selection must query server");
assert.deepEqual(pendingToolCalls[0].arguments, { workspace_path: "/work/foo" });
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "manual /work/foo" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooWork],
    }),
    workspaceFilter: "/work/foo",
    workspaceOptions: [{ key: "/work/foo", label: "foo (/work/foo)" }],
  },
});
await flushMicrotasks();

const wsSelAfterRefresh = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
assert.ok(wsSelAfterRefresh.children.some((o) => o.value === "/work"), "Sibling /work option must persist after filtered refresh");
wsSelAfterRefresh.value = "";
wsSelAfterRefresh.onchange();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1, "Switching back to All must trigger unfiltered refresh");
assert.deepEqual(pendingToolCalls[0].arguments, {});
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "restored all" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj, idleFooWork, idleWorkParent, idleNoWs],
    }),
    workspaceFilter: null,
    workspaceOptions: disambigWithParent,
  },
});
await flushMicrotasks();

// Filter to __none__ shows only unassociated session without touching binding
const wsSelNone = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
wsSelNone.value = "__none__";
wsSelNone.onchange();
await flushMicrotasks();
findButtonsByText("展开历史空闲会话 (1)")[0].onclick();
await flushMicrotasks();
assert.ok(findSessionListEl().textContent.includes("#cid-nw03"));
assert.ok(!findSessionListEl().textContent.includes("#cid-fp01"));

// 8b. Copying untruncated >340-char Q10/option #14 preserves tail caveat marker verbatim
const clipboardWrites = [];
fakeNavigator.clipboard = {
  async writeText(txt) {
    clipboardWrites.push(String(txt));
  },
};
const tailOptMarker = "OPT_TAIL_GUARD_99";
const longOpt14 = "L".repeat(345) + tailOptMarker;
const largeMultiQSession = {
  ...makeSessionItem("cid-large-mq", "在聊天框回答选择题：Q1 prompt？（共 10 个问题）", null, {
    workspaceName: "foo",
  }),
  workspaceKey: "/projects/foo",
  workspacePaths: ["/projects/foo"],
  sourceType: "APPROVAL_REQUIRED",
  lifecycle: "WAITING_USER_DECISION",
  questions: [
    ...Array.from({ length: 9 }, (_, i) => ({
      question: `Q${i + 1} prompt?`,
      options: [`Q${i + 1}-A`],
      multiSelect: false,
    })),
    {
      question: "Q10 final prompt?",
      options: [...Array.from({ length: 13 }, (_, j) => `Choice-${j + 1}`), longOpt14],
      multiSelect: false,
    },
  ],
};

sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "large multi-q" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "hook_bound",
      boundConversationId: "cid-large-mq",
      selectedSession: largeMultiQSession,
      sessions: [idleFooProj],
    }),
    workspaceOptions: disambigOpts,
  },
});
await flushMicrotasks();

assert.ok(renderedText().includes("Q10. Q10 final prompt?"), "10th question must render");
const optButtons8 = findButtonsByText("复制选项");
assert.equal(optButtons8.length, 23, "Expected 9 + 14 = 23 option copy buttons");
optButtons8[optButtons8.length - 1].onclick();
await flushMicrotasks();
assert.equal(clipboardWrites.length, 1);
assert.equal(clipboardWrites[0], longOpt14, "Copied option text must preserve full >340 char suffix");
assert.ok(clipboardWrites[0].endsWith(tailOptMarker));
console.log("[PASS] 8. Workspace disambiguation & filter retention, idle-only collapse, & untruncated Q10 copy");

// 9. Hook-bound refresh ID guard, direct filtered A->B switch & stale discard, & disambiguated label persistence across single-ws refreshes
const teamForkItem = makeSessionItem("team-fork", "Forked Action", "Forked Prompt");
sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "team not found" }],
  structuredContent: makePayload({
    bindingMode: "hook_bound",
    boundConversationId: "team",
    sessionNotFound: true,
    selectedSession: null,
    sessions: [teamForkItem],
  }),
});
await flushMicrotasks();
assert.ok(renderedText().includes("已绑定当前会话 #team"));

findButtonsByText("刷新")[0].onclick();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
assert.deepEqual(pendingToolCalls[0].arguments, { conversation_id: "team" });
// Even if a misbehaving server replied with a prefixed sibling, panel keeps exact hook_bound ID and hides sibling card
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "hijack attempt" }],
  structuredContent: makePayload({
    bindingMode: "explicit",
    boundConversationId: "team-fork",
    selectedSession: teamForkItem,
    sessions: [teamForkItem],
  }),
});
await flushMicrotasks();
assert.ok(renderedText().includes("已绑定当前会话 #team"), "Hook-bound ID must not be rewritten to team-fork");
assert.equal(findCards().length, 0, "Prefixed sibling detail card must never render for hook_bound session");

// 9b. Auto-refresh off + filtered /projects/foo -> direct switch to /work/foo keeps disambiguated labels and ignores stale response
sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "all disambig" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj, idleFooWork, idleNoWs],
    }),
    workspaceFilter: null,
    workspaceOptions: disambigOpts,
  },
});
await flushMicrotasks();

findButtonsByText("自动刷新: 开")[0].onclick();
await flushMicrotasks();
assert.ok(findButtonsByText("自动刷新: 关").length === 1);

const wsSel9 = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
wsSel9.value = "/projects/foo";
wsSel9.onchange();
await flushMicrotasks();
findButtonsByText("刷新")[0].onclick();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
assert.deepEqual(pendingToolCalls[0].arguments, { workspace_path: "/projects/foo" });
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "only /projects/foo" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj],
    }),
    workspaceFilter: "/projects/foo",
    workspaceOptions: [{ key: "/projects/foo", label: "foo" }],
  },
});
await flushMicrotasks();

// Switch directly from filtered /projects/foo -> /work/foo; must issue immediate fetch & NOT show false empty-state
const wsSel9Switch = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
wsSel9Switch.value = "/work/foo";
wsSel9Switch.onchange();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1, "Switching filtered workspace A -> B must query server immediately");
assert.deepEqual(pendingToolCalls[0].arguments, { workspace_path: "/work/foo" });

// Resolve /work/foo with bare "foo" label; disambiguation must persist and idle session must load
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "only /work/foo" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooWork],
    }),
    workspaceFilter: "/work/foo",
    workspaceOptions: [{ key: "/work/foo", label: "foo" }],
  },
});
await flushMicrotasks();

assert.ok(!renderedText().includes("暂无已记录的会话状态"), "Workspace B must render session list instead of empty error");
findButtonsByText("展开历史空闲会话 (1)")[0].onclick();
await flushMicrotasks();
assert.ok(findSessionListEl().textContent.includes("#cid-fw02"));
const wsSel9Labels = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0].children.map((o) => o._text);
assert.ok(wsSel9Labels.some((t) => t.includes("foo (/projects/foo)")), "Single-ws refreshes must preserve /projects/foo disambiguation");
assert.ok(wsSel9Labels.some((t) => t.includes("foo (/work/foo)")), "Single-ws refreshes must preserve /work/foo disambiguation");

// Trigger manual refresh on /work/foo, switch to /projects/foo before it resolves, ensure stale /work/foo response is dropped
findButtonsByText("刷新")[0].onclick();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
assert.deepEqual(pendingToolCalls[0].arguments, { workspace_path: "/work/foo" });
const wsSel9Race = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
wsSel9Race.value = "/projects/foo";
wsSel9Race.onchange();
await flushMicrotasks();
// Stale /work/foo call resolves after user switched to /projects/foo -> ignored and coalesced /projects/foo runs cleanly
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "late /work/foo" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooWork],
    }),
    workspaceFilter: "/work/foo",
    workspaceOptions: [{ key: "/work/foo", label: "foo" }],
  },
});
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1, "Expected coalesced follow-up refresh for /projects/foo");
assert.deepEqual(pendingToolCalls[0].arguments, { workspace_path: "/projects/foo" });
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "fresh /projects/foo" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj],
    }),
    workspaceFilter: "/projects/foo",
    workspaceOptions: [{ key: "/projects/foo", label: "foo" }],
  },
});
await flushMicrotasks();
assert.ok(findSessionListEl().textContent.includes("#cid-fp01"));
assert.ok(!findSessionListEl().textContent.includes("#cid-fw02"));

// 9c. Switching A (/projects/foo) -> All ("") -> B (/work/foo) before All settles must still enqueue & fetch B
const wsSel9cAll = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
wsSel9cAll.value = "";
wsSel9cAll.onchange();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
assert.deepEqual(pendingToolCalls[0].arguments, {});

const wsSel9cB = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
wsSel9cB.value = "/work/foo";
wsSel9cB.onchange();
await flushMicrotasks();

// Resolve the superseded {} response -> ignored, and coalesced follow-up query for /work/foo fires automatically
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "superseded all" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooProj, idleFooWork, idleNoWs],
    }),
    workspaceFilter: null,
    workspaceOptions: disambigOpts,
  },
});
await flushMicrotasks();

assert.equal(pendingToolCalls.length, 1, "A->All->B before All settles must enqueue follow-up fetch for B");
assert.deepEqual(pendingToolCalls[0].arguments, { workspace_path: "/work/foo" });
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "settled B" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "unbound",
      boundConversationId: null,
      selectedSession: null,
      sessions: [idleFooWork],
    }),
    workspaceFilter: "/work/foo",
    workspaceOptions: [{ key: "/work/foo", label: "foo" }],
  },
});
await flushMicrotasks();
assert.ok(findSessionListEl().textContent.includes("#cid-fw02"), "Workspace B session must render after coalesced fetch");
console.log("[PASS] 9. Hook-bound refresh guard, filtered A->B / A->All->B switch & stale discard, & disambiguated label persistence");

// 10. Background polls must not rebuild the DOM when nothing visible changed
//     (keeps <select> focus / manual-copy text selection intact), but errors render at once.
function latestPollTimer() {
  const timers = Array.from(activeTimers.values()).filter((t) => t.delay >= 3000);
  return timers[timers.length - 1];
}
if (findButtonsByText("自动刷新: 关").length) {
  findButtonsByText("自动刷新: 关")[0].onclick();
  await flushMicrotasks();
}
const containerBefore10 = appRoot.children[0];
const settledPayload10 = {
  ...makePayload({
    bindingMode: "unbound",
    boundConversationId: null,
    selectedSession: null,
    sessions: [idleFooWork],
  }),
  workspaceFilter: "/work/foo",
  workspaceOptions: [{ key: "/work/foo", label: "foo" }],
};
const poll10a = latestPollTimer();
assert.ok(poll10a, "Expected poll timer for passive-render test");
poll10a.fn();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "unchanged" }],
  structuredContent: { ...settledPayload10, generatedAtIso: "2026-11-22T00:09:59Z" },
});
await flushMicrotasks();
assert.equal(appRoot.children[0], containerBefore10, "10 FAIL: unchanged poll rebuilt the DOM");

const poll10b = latestPollTimer();
assert.ok(poll10b, "Expected follow-up poll timer");
poll10b.fn();
await flushMicrotasks();
pendingToolCalls.shift().resolve({ isError: true, content: [{ type: "text", text: "backend down" }] });
await flushMicrotasks();
assert.notEqual(appRoot.children[0], containerBefore10, "10 FAIL: poll error did not re-render");
assert.ok(renderedText().includes("状态查询失败"), "10 FAIL: poll error banner missing");
console.log("[PASS] 10. Unchanged polls skip DOM rebuild; poll errors render immediately");

// 11. While text is selected inside the panel, unrelated list changes are deferred,
//     but a same-session prompt withdrawal (new turn: prompt cleared, stale=false) renders at once.
function liveRegionText() {
  const live = docBody.findAll((n) => n.attributes?.get?.("aria-live") === "polite")[0];
  return live ? live.textContent : "";
}
assert.ok(liveRegionText().includes("刷新失败"), "11 FAIL: live region did not announce refresh failure");

const sessE = makeSessionItem("cid-eeee-5555", "Action E", "OLD PLAN PROMPT", { options: ["Option E1"] });
const otherF = makeSessionItem("cid-ffff-6666", "Action F", "Prompt F");
const selE = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "按工作区筛选会话",
)[0];
if (selE) {
  selE.value = "";
  selE.onchange();
  await flushMicrotasks();
  while (pendingToolCalls.length) {
    pendingToolCalls.shift().resolve({ structuredContent: makePayload({ sessions: [sessE] }) });
    await flushMicrotasks();
  }
}
// Select session E explicitly
const sessSelE = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "选择 Antigravity 会话",
)[0];
sessSelE.value = sessE.conversationId;
sessSelE.onchange();
await flushMicrotasks();
pendingToolCalls.shift().resolve({
  structuredContent: makePayload({
    bindingMode: "explicit",
    boundConversationId: sessE.conversationId,
    selectedSession: sessE,
    sessions: [sessE],
  }),
});
await flushMicrotasks();
assert.equal(findButtonsByText("复制指令").length, 1, "11 setup: expected enabled copy for E");
assert.ok(liveRegionText().includes("Action E"), "11 FAIL: live region did not announce selected session");

// Simulate the user selecting the prompt text (manual-copy fallback)
const promptPre = appRoot.findAll((n) => n.tagName === "PRE")[0];
fakeSelection.isCollapsed = false;
fakeSelection.anchorNode = promptPre;

// 11a. Unrelated overview change (another session appears) is deferred while selecting
const containerBefore11 = appRoot.children[0];
latestPollTimer().fn();
await flushMicrotasks();
pendingToolCalls.shift().resolve({
  structuredContent: makePayload({
    bindingMode: "explicit",
    boundConversationId: sessE.conversationId,
    selectedSession: sessE,
    sessions: [sessE, otherF],
  }),
});
await flushMicrotasks();
assert.equal(appRoot.children[0], containerBefore11, "11a FAIL: list-only change rebuilt DOM during text selection");

// 11a'. Clearing the selection flushes the deferred render
fakeSelection.isCollapsed = true;
fakeSelection.anchorNode = null;
dispatchTo(docListeners, "selectionchange");
await flushMicrotasks();
assert.notEqual(appRoot.children[0], containerBefore11, "11a' FAIL: deferred render not flushed on selectionchange");
const optValsAfterFlush = appRoot
  .findAll((n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "选择 Antigravity 会话")[0]
  .children.map((o) => o.value);
assert.ok(optValsAfterFlush.includes(otherF.conversationId), "11a' FAIL: flushed render missing new session F");

// Re-select the (re-rendered) prompt text before the withdrawal poll
fakeSelection.isCollapsed = false;
fakeSelection.anchorNode = appRoot.findAll((n) => n.tagName === "PRE")[0];
const containerBefore11b = appRoot.children[0];

// 11b. Same session, new turn: prompt + options withdrawn, stale stays false -> must render now
const sessEWorking = {
  ...sessE,
  lifecycle: "WORKING",
  waitingOnUser: false,
  nextAction: null,
  suggestedPrompt: null,
  options: [],
  statusBadge: "🟢 Agent 运行中 (WORKING)",
};
latestPollTimer().fn();
await flushMicrotasks();
pendingToolCalls.shift().resolve({
  structuredContent: makePayload({
    bindingMode: "explicit",
    boundConversationId: sessE.conversationId,
    selectedSession: sessEWorking,
    sessions: [sessEWorking, otherF],
  }),
});
await flushMicrotasks();
assert.notEqual(appRoot.children[0], containerBefore11b, "11b FAIL: prompt withdrawal deferred during selection");
assert.equal(findButtonsByText("复制指令").length, 0, "11b FAIL: withdrawn prompt still copyable");
assert.equal(findButtonsByText("复制选项").length, 0, "11b FAIL: withdrawn options still copyable");
assert.ok(!renderedText().includes("OLD PLAN PROMPT"), "11b FAIL: withdrawn prompt still shown");

// 11c. Live region follows the new status once selection is cleared
fakeSelection.isCollapsed = true;
fakeSelection.anchorNode = null;
dispatchTo(docListeners, "selectionchange");
await flushMicrotasks();
assert.ok(liveRegionText().includes("Agent 运行中"), "11c FAIL: live region not updated to new status");
console.log("[PASS] 11. Selection defers list-only changes; prompt withdrawal and live-region updates render immediately");

// 12. Conversation name displays ahead of #shortId across list, <select>, pill, card header, & loading banner;
//     activeTool.elapsedSec-only ticks skip DOM rebuild while updating syncLabel.title, and approvalHint rebuilds at once.
const namedWait = {
  ...makeSessionItem("cid-name-1111", "审阅并合入主分支", "确认后请合入 main 并运行全部测试", {
    workspaceName: "antigravity-next-step",
    conversationName: "UI 样式对比与合入",
  }),
  lastUserInput: "这不应该替代对话名",
};
const fallbackWait = {
  ...makeSessionItem("cid-anon-2222", "处理匿名会话", null, {
    workspaceName: "repo-anon",
    conversationName: null,
  }),
  lastUserInput: "匿名会话原始输入",
};
const namedTool = {
  ...makeSessionItem("cid-tool-3333", "正在执行工具：run_command · npm test", null, {
    workspaceName: "antigravity-next-step",
    conversationName: "长时间构建与验证",
  }),
  lifecycle: "TOOL_RUNNING",
  waitingOnUser: false,
  actor: "agent",
  actorLabel: "Agent（主会话）",
  statusBadge: "🟢 Agent 运行中 (TOOL_RUNNING)",
  sourceType: "LIFECYCLE",
  activeTool: {
    name: "run_command",
    summary: "npm test",
    elapsedSec: 2.1,
  },
};
const namedSidebar37 = makeSessionItem("cid-side-3737", "审核配置状态", null, {
  workspaceName: "antigravity-next-step",
  conversationName: "Application Configuration File Checks",
});
const namedSessions = [namedWait, fallbackWait, namedTool, namedSidebar37];

sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "named list" }],
  structuredContent: makePayload({
    bindingMode: "unbound",
    boundConversationId: null,
    selectedSession: null,
    sessions: namedSessions,
  }),
});
await flushMicrotasks();

// 12a. Unbound list cards & <select> options put conversationName BEFORE #shortId; null falls back cleanly
const listButtons12 = findSessionListEl().findAll((n) => n.tagName === "BUTTON" && n.className.includes("ns-session-item"));
const namedBtn12 = listButtons12.find((b) => b.textContent.includes("UI 样式对比与合入"));
assert.ok(namedBtn12, "12a FAIL: Named session list button missing");
assert.ok(namedBtn12.className.includes("has-name"), "12a FAIL: Named session card must include .has-name class");
const namedTitleNodes12 = namedBtn12.findAll((n) => typeof n.className === "string" && n.className.includes("ns-session-name"));
assert.equal(namedTitleNodes12.length, 1, "12a FAIL: Expected .ns-session-name node");
assert.equal(namedTitleNodes12[0].textContent, "UI 样式对比与合入");
const namedBtnText12 = namedBtn12.textContent;
assert.ok(
  namedBtnText12.indexOf("UI 样式对比与合入") < namedBtnText12.indexOf("#cid-name"),
  "12a FAIL: conversationName must appear BEFORE #shortId inside session card",
);

const fallbackBtn12 = listButtons12.find((b) => b.textContent.includes("#cid-anon"));
assert.ok(fallbackBtn12, "12a FAIL: Fallback anonymous session card missing");
assert.ok(!fallbackBtn12.className.includes("has-name"), "12a FAIL: Unnamed fallback card must NOT have .has-name class");
assert.ok(fallbackBtn12.textContent.includes("匿名会话原始输入"), "12a FAIL: Fallback card missing lastUserInput");

const selectOpts12 = appRoot
  .findAll((n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "选择 Antigravity 会话")[0]
  .children.map((o) => o._text);
const optNamed12 = selectOpts12.find((t) => t.includes("#cid-name"));
assert.ok(optNamed12, "12a FAIL: <select> option missing for named session");
assert.ok(
  optNamed12.indexOf("UI 样式对比与合入") >= 0 &&
    optNamed12.indexOf("UI 样式对比与合入") < optNamed12.indexOf("#cid-name"),
  `12a FAIL: <select> option must place conversationName before #shortId, got: ${optNamed12}`,
);
const opt37 = selectOpts12.find((t) => t.includes("#cid-side"));
assert.ok(
  opt37 && opt37.includes("Application Configuration File Checks"),
  `12a FAIL: 37-char sidebar title should render untruncated in <select>, got: ${opt37}`,
);

// 12b. Hook-bound pill & detail card header show conversationName in front
sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "hook named" }],
  structuredContent: makePayload({
    bindingMode: "hook_bound",
    boundConversationId: namedWait.conversationId,
    selectedSession: namedWait,
    sessions: namedSessions,
  }),
});
await flushMicrotasks();
assert.ok(renderedText().includes("已绑定：UI 样式对比与合入"), "12b FAIL: Bound pill missing conversationName");
const boundPill12 = appRoot.findAll(
  (n) => typeof n.className === "string" && n.className.includes("ns-bind-pill"),
)[0];
assert.ok(boundPill12, "12b FAIL: Bound pill missing");
assert.ok(
  boundPill12.textContent.includes("UI 样式对比与合入") &&
    boundPill12.textContent.includes("#cid-name") &&
    boundPill12.textContent.indexOf("UI 样式对比与合入") < boundPill12.textContent.indexOf("#cid-name"),
  `12b FAIL: Bound pill must place conversationName before #shortId, got: ${boundPill12.textContent}`,
);
const card12 = findCards()[0];
assert.ok(card12, "12b FAIL: Detail card missing");
const cardTitle12 = card12.findAll((n) => typeof n.className === "string" && n.className.includes("ns-card-title"))[0];
assert.ok(cardTitle12 && cardTitle12.textContent === "UI 样式对比与合入", "12b FAIL: Detail card header missing title");
assert.ok(
  card12.textContent.indexOf("UI 样式对比与合入") < card12.textContent.indexOf("#cid-name"),
  "12b FAIL: conversationName must appear BEFORE #shortId inside detail card",
);

// 12c. Loading banner names the session & TOOL_RUNNING elapsedSec-only poll skips DOM rebuild while updating sync title
const sessSel12 = appRoot.findAll(
  (n) => n.tagName === "SELECT" && n.attributes.get("aria-label") === "选择 Antigravity 会话",
)[0];
sessSel12.value = namedTool.conversationId;
sessSel12.onchange();
await flushMicrotasks();
assert.ok(renderedText().includes("已选定：长时间构建与验证"), "12c FAIL: Selected pill missing conversationName");
const selPill12 = appRoot.findAll(
  (n) => typeof n.className === "string" && n.className.includes("ns-bind-pill"),
)[0];
assert.ok(
  selPill12 &&
    selPill12.textContent.includes("长时间构建与验证") &&
    selPill12.textContent.includes("#cid-tool") &&
    selPill12.textContent.indexOf("长时间构建与验证") < selPill12.textContent.indexOf("#cid-tool"),
  `12c FAIL: Selected pill must place conversationName before #shortId, got: ${selPill12?.textContent}`,
);
assert.ok(
  renderedText().includes("正在加载会话「长时间构建与验证」(#cid-tool)"),
  "12c FAIL: Loading banner must show conversationName ahead of #shortId",
);

pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "tool running" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "explicit",
      boundConversationId: namedTool.conversationId,
      selectedSession: namedTool,
      sessions: namedSessions,
    }),
    generatedAtIso: "2026-11-22T00:01:00Z",
  },
});
await flushMicrotasks();

const containerBefore12 = appRoot.children[0];
const syncEl12Before = appRoot.findAll((n) => typeof n.className === "string" && n.className.includes("ns-status-line"))[0].children[0];
assert.ok(syncEl12Before.title.startsWith("2026-11-22T00:01:00Z"), "12c FAIL: Initial sync title missing");

// Background poll updates only elapsedSec & generatedAtIso -> must NOT rebuild DOM, MUST update syncEl.title
latestPollTimer().fn();
await flushMicrotasks();
assert.equal(pendingToolCalls.length, 1);
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "tool tick" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "explicit",
      boundConversationId: namedTool.conversationId,
      selectedSession: {
        ...namedTool,
        activeTool: { ...namedTool.activeTool, elapsedSec: 5.4 },
      },
      sessions: namedSessions,
    }),
    generatedAtIso: "2026-11-22T00:05:10Z",
  },
});
await flushMicrotasks();
assert.equal(appRoot.children[0], containerBefore12, "12c FAIL: elapsedSec-only tick rebuilt DOM");
assert.ok(
  syncEl12Before.title.startsWith("2026-11-22T00:05:10Z"),
  `12c FAIL: updateSyncLabel did not update title on skipped rebuild, got: ${syncEl12Before.title}`,
);

// Next poll adds approvalHint -> must rebuild immediately to surface approval banner
latestPollTimer().fn();
await flushMicrotasks();
pendingToolCalls.shift().resolve({
  content: [{ type: "text", text: "tool approval hint" }],
  structuredContent: {
    ...makePayload({
      bindingMode: "explicit",
      boundConversationId: namedTool.conversationId,
      selectedSession: {
        ...namedTool,
        activeTool: {
          ...namedTool.activeTool,
          elapsedSec: 9.2,
          approvalHint: "工具耗时超 8 秒，若宿主弹出了权限确认框请先处理",
        },
      },
      sessions: namedSessions,
    }),
    generatedAtIso: "2026-11-22T00:05:15Z",
  },
});
await flushMicrotasks();
assert.notEqual(appRoot.children[0], containerBefore12, "12c FAIL: approvalHint appearing did not rebuild DOM");
assert.ok(renderedText().includes("审批提示：工具耗时超 8 秒"), "12c FAIL: approvalHint banner missing");

// 12d. Code-point safe truncation avoids splitting surrogate-pair emojis
const emojiSess = makeSessionItem("cid-emj0-4444", "Check emoji clipping", null, {
  workspaceName: "repo-emj",
  conversationName: "🚀".repeat(25),
});
sendHostNotification("ui/notifications/tool-input", { arguments: {} });
await flushMicrotasks();
sendHostNotification("ui/notifications/tool-result", {
  content: [{ type: "text", text: "emoji bind" }],
  structuredContent: makePayload({
    bindingMode: "hook_bound",
    boundConversationId: emojiSess.conversationId,
    selectedSession: emojiSess,
    sessions: [emojiSess],
  }),
});
await flushMicrotasks();
const emojiPillText = appRoot.findAll(
  (n) => typeof n.className === "string" && n.className.includes("ns-bind-pill"),
)[0].textContent;
assert.ok(!/[\uD800-\uDFFF]/.test(emojiPillText) || [...emojiPillText].every((ch) => (ch.codePointAt(0) ?? 0) > 0xffff || !/[\uD800-\uDFFF]/.test(ch)), "12d FAIL: Lone surrogate emitted");
assert.ok(!emojiPillText.includes("\uFFFD"), "12d FAIL: Replacement char emitted in truncated emoji label");
assert.ok(emojiPillText.includes("#cid-emj0"), "12d FAIL: #shortId missing on emoji pill");
console.log("[PASS] 12. Conversation name displays in front across UI, fallback intact, & TOOL_RUNNING elapsedSec skips rebuild cleanly");

console.log(`ALL SIDE PANEL BUNDLE CHECKS PASSED (total RPC calls=${recordedToolCalls.length}, maxConcurrent=${maxConcurrentRpc})`);
