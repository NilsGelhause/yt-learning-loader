/* core.js — shared state, helpers, api wrapper, toasts, markdown, mermaid */
"use strict";

const App = {
  settings: null,
  state: null,
  videos: null,          // cache of /api/videos
  learningsCache: {},    // id -> learnings object | null
  skillsIndex: null,     // cache of /api/skills
  tabs: {},              // id -> {render(root, screen), built}
  currentTab: null,
  mermaidTheme: "dark",
  teachPreset: null,     // {video_id, focus_skill?} set by other tabs, consumed by Teach
};
window.App = App;

/* ---------- formatting helpers ---------- */
function fmtDate(yyyymmdd) {
  if (!yyyymmdd || typeof yyyymmdd !== "string" || yyyymmdd.length < 8) return yyyymmdd || "";
  const y = +yyyymmdd.slice(0, 4), m = +yyyymmdd.slice(4, 6), d = +yyyymmdd.slice(6, 8);
  const date = new Date(y, m - 1, d);
  if (isNaN(date)) return yyyymmdd;
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
function fmtDur(seconds) {
  const s = Math.max(0, Math.round(+seconds || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

/* ---------- DOM helpers ---------- */
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return node;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

/* ---------- shared video sorting (used by every tab that lists videos) ----
   key: "date" | "title" | "channel" | "dur"; dir: 1 asc, -1 desc.
   Videos missing the sort field sink to the end in both directions. */
const VIDEO_SORT_OPTIONS = [
  ["date_desc", "Newest first"], ["date_asc", "Oldest first"],
  ["title_asc", "Title A→Z"], ["title_desc", "Title Z→A"],
  ["channel_asc", "Channel A→Z"],
  ["dur_desc", "Longest first"], ["dur_asc", "Shortest first"],
];
function videoSort(rows, key, dir) {
  const val = {
    date: v => v.published || "", title: v => (v.title || "").toLowerCase(),
    channel: v => (v.channel || "").toLowerCase(), dur: v => +v.duration || 0,
  }[key];
  const has = {
    date: v => !!v.published, title: v => !!v.title,
    channel: v => !!v.channel, dur: v => !!+v.duration,
  }[key];
  const present = rows.filter(has);
  const missing = rows.filter(v => !has(v));
  present.sort((a, b) => {
    const x = val(a), y = val(b);
    const c = typeof x === "number" ? x - y : (x < y ? -1 : x > y ? 1 : 0);
    return c * dir;
  });
  return [...present, ...missing];
}
/* identical sort dropdown for every video list */
function videoSortSelect(value, onchange) {
  return el("select", {
    title: "Sort videos", onchange: (e) => onchange(e.target.value),
  }, VIDEO_SORT_OPTIONS.map(([k, label]) =>
    el("option", { value: k, selected: value === k || null }, label)));
}
/* shared Shorts/long-form filter: "" | "shorts" | "long" */
function lengthFilterSelect(value, onchange) {
  return el("select", {
    title: "Filter by length", onchange: (e) => onchange(e.target.value),
  },
    el("option", { value: "", selected: value === "" || null }, "All lengths"),
    el("option", { value: "shorts", selected: value === "shorts" || null }, "Shorts"),
    el("option", { value: "long", selected: value === "long" || null }, "Long-form"));
}
function lengthMatch(v, mode) {
  return mode === "shorts" ? !!v.is_short : mode === "long" ? !v.is_short : true;
}

const THUMB_PLACEHOLDER =
  "data:image/svg+xml;utf8," + encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">` +
    `<rect width="320" height="180" fill="#1a1e29"/>` +
    `<circle cx="160" cy="90" r="30" fill="none" stroke="#3a4157" stroke-width="4"/>` +
    `<path d="M152 74l26 16-26 16z" fill="#3a4157"/>` +
    `<text x="160" y="150" font-family="sans-serif" font-size="12" fill="#5c6478" text-anchor="middle">no thumbnail</text>` +
    `</svg>`);
function thumbImg(video, cls) {
  const img = el("img", { class: cls || "thumb", src: `/api/thumb/${video.id}`, alt: "", loading: "lazy" });
  img.addEventListener("error", () => { img.src = THUMB_PLACEHOLDER; });
  return img;
}

/* ---------- toasts ---------- */
function toast(message, type) {
  type = type || "info";
  const box = document.getElementById("toasts");
  const node = el("div", { class: `toast ${type}` }, el("span", null, message));
  box.appendChild(node);
  setTimeout(() => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 220);
  }, 4200);
  node.addEventListener("click", () => node.remove());
}

/* ---------- api ---------- */
let tokenPromptShown = false;  // at most one token prompt per page load

function promptForToken() {
  /* Overlay asking for the LAN token; resolves true when one was saved. */
  return new Promise((resolve) => {
    if (tokenPromptShown) return resolve(false);
    tokenPromptShown = true;
    const input = el("input", {
      type: "password", placeholder: "LAN token", style: "flex:1",
      onkeydown: (e) => { if (e.key === "Enter") saveBtn.click(); },
    });
    const close = (ok) => { overlay.remove(); resolve(ok); };
    const saveBtn = el("button", {
      class: "primary",
      onclick: () => {
        const value = input.value.trim();
        if (!value) return;
        sessionStorage.setItem("ytl_token", value);
        close(true);
      },
    }, "Save");
    const overlay = el("div", { class: "modal-overlay", id: "token-overlay" },
      el("div", { class: "modal-box", style: "width:380px;height:auto;padding:16px;gap:10px" },
        el("div", { class: "modal-head", style: "padding:0 0 4px;border-bottom:none" },
          el("strong", null, "LAN token required"),
          el("button", { onclick: () => close(false) }, "✕")),
        el("div", { class: "muted small" },
          "This server requires a token. Enter it once — it is kept for this tab only."),
        el("div", { class: "row" }, input, saveBtn)));
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(false); });
    document.body.appendChild(overlay);
    input.focus();
  });
}

async function api(path, options) {
  options = options || {};
  const init = { method: options.method || "GET", headers: {} };
  const token = sessionStorage.getItem("ytl_token");
  if (token) init.headers["x-ytl-token"] = token;
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = typeof options.body === "string" ? options.body : JSON.stringify(options.body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (!options.quiet) toast(`Network error: ${path}`, "error");
    throw err;
  }
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = null; } }
  if (!res.ok) {
    if (options.quiet) {
      const e = new Error((data && data.error) || res.statusText);
      e.status = res.status;
      throw e;
    }
    if (res.status === 401 && data && data.error === "token_required") {
      const saved = await promptForToken();
      if (saved) return api(path, options);  // retry once with the new token
    }
    const msg = (data && data.error) || `${res.status} ${res.statusText}`;
    toast(String(msg), "error");
    const err = new Error(String(msg));
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ---------- data accessors with caching ---------- */
async function getVideos(force) {
  if (!App.videos || force) {
    const data = await api("/api/videos");
    App.videos = data.videos || [];
  }
  return App.videos;
}
function invalidateVideos() { App.videos = null; }

async function getLearnings(id, force) {
  if (!(id in App.learningsCache) || force) {
    try {
      App.learningsCache[id] = await api(`/api/learnings/${id}`);
    } catch (err) {
      if (err.status === 404) App.learningsCache[id] = null;
      else throw err;
    }
  }
  return App.learningsCache[id];
}
function invalidateLearnings(id) { delete App.learningsCache[id]; }

async function getSkillsIndex(force) {
  if (!App.skillsIndex || force) {
    const data = await api("/api/skills");
    App.skillsIndex = data.skills || [];
  }
  return App.skillsIndex;
}

async function getSettings(force) {
  if (!App.settings || force) App.settings = await api("/api/settings");
  return App.settings;
}

/* ---------- skeletons / empty states ---------- */
function skeletonLines(n) {
  const wrap = el("div", { style: "display:flex;flex-direction:column;gap:10px;padding:16px" });
  for (let i = 0; i < n; i++) wrap.appendChild(el("div", { class: "skeleton", style: `width:${88 - (i % 3) * 14}%` }));
  return wrap;
}
function emptyState(text, icon) {
  return el("div", { class: "empty-state" }, el("span", { class: "icon" }, icon || "◌"), text);
}

/* ---------- copy helper ---------- */
async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard", "success");
  } catch {
    const ta = el("textarea", null, text);
    document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
    toast("Copied to clipboard", "success");
  }
  if (btn) { const old = btn.textContent; btn.textContent = "Copied!"; setTimeout(() => { btn.textContent = old; }, 1500); }
}

/* ---------- mermaid ---------- */
let mermaidSeq = 0;
let mermaidReady = false;
function initMermaid() {
  if (typeof mermaid === "undefined") return;
  try {
    mermaid.initialize({ startOnLoad: false, theme: App.mermaidTheme, securityLevel: "loose" });
    mermaidReady = true;
  } catch (err) {
    console.warn("mermaid init failed", err);
  }
}
async function renderMermaid(source) {
  /* mermaid-box must grow to fill the canvas — as a plain flex item it would
     shrink to the svg's intrinsic (small) size and width:100% would be 100%
     of that small box */
  const container = el("div", { class: "mermaid-box", style: "overflow:auto" });
  if (typeof mermaid === "undefined" || !mermaidReady) {
    container.appendChild(el("pre", { class: "mermaid-fallback" }, source));
    return container;
  }
  try {
    const id = `mmd-${Date.now()}-${mermaidSeq++}`;
    const result = await mermaid.render(id, source);
    container.innerHTML = result.svg;
  } catch (err) {
    console.warn("mermaid render failed", err);
    clear(container);
    container.appendChild(el("pre", { class: "mermaid-fallback" }, source));
  }
  return container;
}

/* ---------- tiny markdown renderer ---------- */
function inlineMarkdown(text) {
  return String(text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}
function renderMarkdown(md) {
  const root = el("div", { class: "markdown" });
  const lines = String(md || "").split(/\r?\n/);
  let list = null, para = [], blockquote = [];
  const flushPara = () => { if (para.length) { root.appendChild(el("p", { html: inlineMarkdown(para.join(" ")) })); para = []; } };
  const flushList = () => { if (list) { root.appendChild(list); list = null; } };
  const flushQuote = () => {
    if (blockquote.length) {
      root.appendChild(el("blockquote", { html: inlineMarkdown(blockquote.join(" ")) }));
      blockquote = [];
    }
  };
  for (const line of lines) {
    const t = line.trim();
    const h = t.match(/^(#{1,3})\s+(.*)$/);
    if (h) {
      flushPara(); flushList(); flushQuote();
      root.appendChild(el("h" + h[1].length, { html: inlineMarkdown(h[2]) }));
    } else if (t === "---") {
      flushPara(); flushList(); flushQuote();
      root.appendChild(el("hr"));
    } else if (t.startsWith(">")) {
      flushPara(); flushList();
      blockquote.push(t.replace(/^>\s?/, ""));
    } else if (t.startsWith("- ")) {
      flushPara(); flushQuote();
      if (!list) { list = el("ul"); }
      list.appendChild(el("li", { html: inlineMarkdown(t.slice(2)) }));
    } else if (t === "") {
      flushPara(); flushList(); flushQuote();
    } else {
      flushList(); flushQuote();
      para.push(t);
    }
  }
  flushPara(); flushList(); flushQuote();
  return root;
}
