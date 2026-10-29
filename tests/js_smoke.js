/* JS smoke test — load core.js + every tab script with a stubbed DOM and
   render each tab once. Fails on any synchronous error (TDZ ReferenceErrors,
   typos, missing helpers) that `node --check` — parse-only — cannot see.
   Run: node tests/js_smoke.js  (exit 0 = every tab rendered clean) */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const JS_DIR = path.join(__dirname, "..", "static", "js");

/* ---- minimal DOM stubs ---- */
const byId = new Map();

function makeNode(tag) {
  const node = {
    tagName: tag,
    children: [],
    attrs: {},
    style: {},
    dataset: {},
    parentNode: null,
    _listeners: {},
    classList: {
      _s: new Set(),
      add(...cs) { cs.forEach(c => this._s.add(c)); },
      remove(...cs) { cs.forEach(c => this._s.delete(c)); },
      toggle(c, force) {
        const on = force === undefined ? !this._s.has(c) : !!force;
        on ? this._s.add(c) : this._s.delete(c);
      },
      contains(c) { return this._s.has(c); },
    },
    get firstChild() { return this.children[0] || null; },
    get isConnected() { return true; },
    appendChild(c) { this.children.push(c); if (c && typeof c === "object") c.parentNode = this; return c; },
    append(...cs) { cs.forEach(c => this.appendChild(c)); },
    prepend(...cs) { cs.reverse().forEach(c => this.children.unshift(c)); },
    replaceChildren(...cs) { this.children = []; this.append(...cs); },
    insertBefore(c, ref) { const i = this.children.indexOf(ref); if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    cloneNode() { return makeNode(this.tagName); },
    removeChild(c) { this.children = this.children.filter(x => x !== c); return c; },
    setAttribute(k, v) { this.attrs[k] = String(v); if (k === "id") byId.set(this.attrs.id, this); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener() {},
    querySelector(sel) { return typeof sel === "string" && sel.startsWith("#") ? byId.get(sel.slice(1)) || null : null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    scrollIntoView() {},
    focus() {},
    click() {},
  };
  Object.defineProperty(node, "className", {
    get() { return [...node.classList._s].join(" "); },
    set(v) { node.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  Object.defineProperty(node, "innerHTML", {
    get() { return node._html || ""; }, set(v) { node._html = String(v); },
  });
  Object.defineProperty(node, "textContent", {
    get() { return node._text || ""; }, set(v) { node._text = String(v); },
  });
  Object.defineProperty(node, "value", {
    get() { return node._value || ""; }, set(v) { node._value = String(v); },
  });
  return node;
}

/* canned API bodies per route — schema-valid empties so async tails that
   destructure (library.known, videos, ...) behave like a fresh real server */
function cannedBody(url) {
  if (url.includes("/skills/library")) return { known: [], learnable: [] };
  if (url.includes("/api/skills")) return { skills: [] };
  if (url.includes("/videos")) return { videos: [] };
  if (url.includes("/workspaces")) return { workspaces: [] };
  if (url.includes("/api/network")) return { nodes: [], hidden_count: 0 };
  if (url.includes("/profile")) return { markdown: "" };
  if (url.includes("/learnings/")) return {};
  /* cua installed with one running sandbox — exercises the Sandboxes table,
     tutorial panes and devices pane, not just the not-installed state */
  if (url.includes("/api/cua/status")) {
    return { installed: true, version: "0.0.0", sandboxes: [{ name: "ytl-vm-1", status: "running", image: "ubuntu:latest" }] };
  }
  return {};
}

const documentStub = {
  createElement: t => makeNode(t),
  createTextNode: t => ({ text: String(t) }),
  /* auto-vivify unknown ids — static index.html elements exist in the real app */
  getElementById(id) {
    if (!byId.has(id)) byId.set(id, makeNode("div"));
    return byId.get(id);
  },
  querySelector(sel) { return typeof sel === "string" && sel.startsWith("#") ? documentStub.getElementById(sel.slice(1)) : null; },
  querySelectorAll: () => [],
  addEventListener() {},
  body: makeNode("body"),
  documentElement: makeNode("html"),
  head: makeNode("head"),
};

const sandbox = {
  console,
  document: documentStub,
  location: { hash: "#overview", href: "http://localhost/", reload() {} },
  history: { replaceState() {} },
  window: null, /* set below — same object as the global */
  fetch: (url) => Promise.resolve({
    ok: true, status: 200,
    text: () => Promise.resolve(JSON.stringify(cannedBody(String(url)))),
    json: () => Promise.resolve(cannedBody(String(url))),
  }),
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  confirm: () => true,
  prompt: () => null,
  alert() {},
  URLSearchParams,
  FormData: class {},
  navigator: { userAgent: "smoke" },
};
sandbox.window = sandbox;
sandbox.window.addEventListener = () => {};
vm.createContext(sandbox);

/* ---- load scripts in index.html order (app.js excluded: it is boot wiring
   that requires a live server; tabs register themselves via App.tabs) ---- */
const files = ["core.js", "tabs-chrome.js"]
  .concat(fs.readdirSync(JS_DIR).filter(f => /^tab-.*\.js$/.test(f)).sort());

for (const f of files) {
  const src = fs.readFileSync(path.join(JS_DIR, f), "utf-8");
  try {
    vm.runInContext(src, sandbox, { filename: f });
  } catch (e) {
    console.error(`LOAD FAIL ${f}: ${e.stack || e}`);
    process.exit(1);
  }
}

const App = sandbox.App;
if (!App || !App.tabs) {
  console.error("App.tabs not registered — core.js/tabs failed to load");
  process.exit(1);
}
/* boot normally loads settings before any tab renders — seed the shape the
   tabs read synchronously */
App.settings = { theme: "dark", accent: "", font_size: "", tabs: [] };

/* async render tails (picker, rail, lists) run after render() returns — flush
   microtasks/macrotasks per tab and attribute floating rejections to it */
const floatingRejections = [];
process.on("unhandledRejection", (e) => floatingRejections.push(e));

(async () => {
  let failures = 0;
  for (const [id, def] of Object.entries(App.tabs)) {
    if (!def || typeof def.render !== "function") continue;
    byId.clear();
    const screen = makeNode("screen");
    byId.set("screen", screen);
    floatingRejections.length = 0;
    try {
      def.render(screen);
    } catch (e) {
      failures++;
      console.error(`RENDER FAIL ${id}: ${e.stack || e}`);
      continue;
    }
    for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r));
    if (floatingRejections.length) {
      failures++;
      console.error(`RENDER FAIL ${id} (async): ${floatingRejections.map(e => (e && (e.stack || e)) || String(e)).join("\n---\n")}`);
      floatingRejections.length = 0;
    } else {
      console.log(`RENDER OK   ${id}`);
    }
  }

  if (failures) {
    console.error(`\n${failures} tab(s) failed to render`);
    process.exit(1);
  }
  console.log("\nall tabs rendered clean");
})();
