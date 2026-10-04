"use strict";

/* 撤回末条前端回归测试（纯 Node，无第三方依赖）。
 *
 * 通过 vm 加载真实的 app/static/app.js，用迷你 DOM 模拟页面元素与事件，
 * 用可控 fetch 代替网络（记录请求与请求头），confirm 可替换以覆盖
 * "二次确认"的通过与取消路径。每个用例独立加载一次脚本，模拟"新开标签页"。
 *
 * 覆盖：按钮禁用态（无已发 / 只读 / 在途，暂停时仍可用）；二次确认通过才发
 * 请求且目标为最后一条已发、携带页面版本号；取消确认不发请求；成功应用最新
 * 状态并提示；409 刷新为最新状态；403 清除凭据并只读。
 *
 * 运行：node tests/withdraw.test.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

/* ------------------------------------------------------------ 迷你 DOM */

class ClassList {
  constructor() {
    this.set = new Set();
  }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const want = force === undefined ? !this.set.has(c) : !!force;
    if (want) this.set.add(c);
    else this.set.delete(c);
    return want;
  }
}

class El {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.classList = new ClassList();
    this.listeners = {};
    this.children = [];
    this.dataset = {};
    this.value = "";
    this.disabled = false;
    this.textContent = "";
    this.innerHTML = "";
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
  closest() { return null; }
  querySelector() { return null; }
  focus() {}
  appendChild(child) { this.children.push(child); return child; }
  remove() {}
}

class Doc {
  constructor() {
    this.byId = new Map();
    this.listeners = {};
  }
  el(id) {
    if (!this.byId.has(id)) {
      const el = new El("div", id);
      if (id === "archives-modal") el.classList.add("hidden");
      this.byId.set(id, el);
    }
    return this.byId.get(id);
  }
  querySelector(sel) {
    if (sel.startsWith("#")) return this.el(sel.slice(1));
    return null;
  }
  querySelectorAll() { return []; }
  createElement(tag) { return new El(tag); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
}

/* -------------------------------------------------------- 加载被测脚本 */

const appCode = fs.readFileSync(
  path.join(__dirname, "..", "app", "static", "app.js"),
  "utf8"
);

/* 每个用例独立加载一次脚本（相当于新开标签页），可预置 sessionStorage
 * 与 confirm 行为（默认二次确认一律通过）。 */
function loadApp(storageData, confirmImpl) {
  const document = new Doc();
  const pending = []; // 尚未应答的请求：{ path, options, resolve, reject }
  const confirmCalls = []; // 二次确认框文案
  const storage = {
    _m: new Map(Object.entries(storageData || {})),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
  };
  const sandbox = {
    document,
    fetch: (path, options) =>
      new Promise((resolve, reject) => pending.push({ path, options, resolve, reject })),
    sessionStorage: storage,
    setTimeout,
    clearTimeout,
    confirm: (msg) => {
      confirmCalls.push(msg);
      return confirmImpl ? confirmImpl(msg) : true;
    },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(appCode, sandbox, { filename: "app.js" });
  return { document, pending, storage, confirmCalls };
}

const flush = () => new Promise((r) => setImmediate(r));
const lastRequest = (ctx) => ctx.pending[ctx.pending.length - 1];

async function respond(req, data, status = 200) {
  req.resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
  await flush();
}

/* ------------------------------------------------------------ 测试数据 */

const ISSUED_A1 = { id: 1, number: "A1", content: "灯光渐亮", issued_at: "2026-10-04T10:00:00+00:00" };
const ISSUED_A2 = { id: 2, number: "A2", content: "音乐起", issued_at: "2026-10-04T10:01:00+00:00" };

function liveState(over) {
  return Object.assign({
    version: 5,
    status: "live",
    paused: false,
    control_enabled: false,
    control_granted: true,
    current: { id: 3, number: "A3", content: "幕落" },
    next: null,
    pending: [{ id: 3, number: "A3", content: "幕落" }],
    issued: [ISSUED_A1, ISSUED_A2],
    issues: [],
    total: 3,
    remaining: 1,
    min_cues: 2,
    max_cues: 12,
  }, over || {});
}

/* ------------------------------------------------------------ 断言 */

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.error("  ✗ " + msg); }
}

/* ------------------------------------------------------------ 用例 */

async function t1_buttonDisabledStates() {
  console.log("T1 按钮禁用态：无已发禁用、有已发可用、暂停时仍可用、只读禁用");
  let ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ issued: [] }));
  check(ctx.document.el("withdraw-btn").disabled === true, "无已发提示时按钮禁用");

  ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  check(ctx.document.el("withdraw-btn").disabled === false, "有已发提示时按钮可用");

  ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ paused: true }));
  check(ctx.document.el("withdraw-btn").disabled === false, "暂停时按钮仍可用");

  ctx = loadApp();
  await respond(lastRequest(ctx), liveState({ control_enabled: true, control_granted: false }));
  check(ctx.document.el("withdraw-btn").disabled === true, "非控制席（只读）按钮禁用");
}

async function t2_clickSendsWithdrawForLastIssued() {
  console.log("T2 二次确认通过：提交最后一条已发的 cue_id 与页面版本号");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());

  ctx.document.el("withdraw-btn").dispatch("click");
  check(ctx.confirmCalls.length === 1, "弹出二次确认");
  check(ctx.confirmCalls[0].includes("A2") && ctx.confirmCalls[0].includes("音乐起"),
    "确认框展示末条编号与内容快照");
  const req = lastRequest(ctx);
  check(req.path === "/api/withdraw", "请求撤回接口");
  const body = JSON.parse(req.options.body);
  check(body.version === 5, "携带页面所见版本号");
  check(body.cue_id === 2, "目标为最后一条已发提示");

  await respond(req, liveState({
    version: 6,
    current: { id: 2, number: "A2", content: "音乐起" },
    next: { id: 3, number: "A3", content: "幕落" },
    pending: [
      { id: 2, number: "A2", content: "音乐起" },
      { id: 3, number: "A3", content: "幕落" },
    ],
    issued: [ISSUED_A1],
    remaining: 2,
  }));
  check(ctx.document.el("version-badge").textContent === "v6", "成功后应用最新状态");
  check(ctx.document.el("toast").textContent.includes("已撤回"), "成功后提示已撤回");
}

async function t3_cancelConfirmNoRequest() {
  console.log("T3 二次确认取消：不发请求");
  const ctx = loadApp(null, () => false);
  await respond(lastRequest(ctx), liveState());
  const before = ctx.pending.length;
  ctx.document.el("withdraw-btn").dispatch("click");
  check(ctx.confirmCalls.length === 1, "弹出二次确认");
  check(ctx.pending.length === before, "取消后不发送请求");
}

async function t4_conflictRefreshes() {
  console.log("T4 409：页面刷新为最新状态");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  ctx.document.el("withdraw-btn").dispatch("click");
  const req = lastRequest(ctx);
  await respond(req, {
    error: "conflict",
    message: "操作已过期",
    state: liveState({ version: 9, issued: [ISSUED_A1] }),
  }, 409);
  check(ctx.document.el("version-badge").textContent === "v9", "409 后刷新为最新版本");
  check(ctx.document.el("toast").textContent.includes("操作未生效"), "409 后提示操作未生效");
}

async function t5_forbiddenClearsToken() {
  console.log("T5 403：凭据清除、标记只读、按钮禁用");
  const ctx = loadApp({ cueControlToken: "tok-old" });
  await respond(lastRequest(ctx), liveState({ control_enabled: true, control_granted: true }));
  ctx.document.el("withdraw-btn").dispatch("click");
  const req = lastRequest(ctx);
  check(req.options.headers["X-Control-Token"] === "tok-old", "请求携带已保存凭据");
  await respond(req, {
    error: "forbidden",
    message: "本页面不是当前控制席：写操作未生效，可凭最新版本号接管",
    state: liveState({ version: 8, control_enabled: true, control_granted: false }),
  }, 403);
  check(ctx.storage.getItem("cueControlToken") === null, "403 后凭据已清除");
  check(ctx.document.el("control-badge").textContent === "控制席：他人 · 本页只读",
    "403 后标记更新为只读");
  check(ctx.document.el("withdraw-btn").disabled === true, "403 后撤回按钮禁用");
}

async function t6_busyDisablesButton() {
  console.log("T6 请求在途时按钮禁用，响应后恢复");
  const ctx = loadApp();
  await respond(lastRequest(ctx), liveState());
  ctx.document.el("withdraw-btn").dispatch("click");
  check(ctx.document.el("withdraw-btn").disabled === true, "在途时按钮禁用（防重复点击）");
  await respond(lastRequest(ctx), liveState({ version: 6 }));
  check(ctx.document.el("withdraw-btn").disabled === false, "响应后恢复可用");
}

/* ------------------------------------------------------------ 主流程 */

(async function main() {
  const cases = [
    t1_buttonDisabledStates,
    t2_clickSendsWithdrawForLastIssued,
    t3_cancelConfirmNoRequest,
    t4_conflictRefreshes,
    t5_forbiddenClearsToken,
    t6_busyDisablesButton,
  ];
  for (const run of cases) await run();

  console.log(
    failures === 0
      ? `\n全部通过（${cases.length} 个用例）`
      : `\n失败 ${failures} 处断言`
  );
  process.exit(failures === 0 ? 0 : 1);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
