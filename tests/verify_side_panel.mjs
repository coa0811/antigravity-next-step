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
    return { removeAllRanges() {}, addRange() {} };
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

const context = vm.createContext({
  window: fakeWindow,
  self: fakeWindow,
  document: fakeDocument,
  navigator: {},
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

console.log(`ALL SIDE PANEL BUNDLE CHECKS PASSED (total RPC calls=${recordedToolCalls.length}, maxConcurrent=${maxConcurrentRpc})`);
