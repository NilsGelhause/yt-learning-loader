/* tabs-chrome.js — browser-style tab bar, hash routing, tab settings persistence */
"use strict";

const TAB_COLORS = ["#7c5cff", "#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#ec4899", "#14b8a6", "#a1a1aa"];
const ALWAYS_VISIBLE = "settings";

function sortedVisibleTabs() {
  return App.settings.tabs
    .filter(t => t.visible || t.id === ALWAYS_VISIBLE)
    .slice()
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
}

function applyAppearance() {
  const s = App.settings || {};
  document.documentElement.style.setProperty("--accent", s.accent || "#7c5cff");
  document.documentElement.dataset.theme = s.theme === "light" ? "light" : "dark";
  App.mermaidTheme = s.theme === "light" ? "neutral" : "dark";
  if (typeof mermaid !== "undefined" && mermaidReady) {
    try { mermaid.initialize({ startOnLoad: false, theme: App.mermaidTheme, securityLevel: "loose" }); } catch {}
  }
}

/* persist tab config wholesale (order / colors / labels / visibility).
   Server response becomes the new source of truth in App.settings. */
async function persistTabs() {
  const tabs = App.settings.tabs
    .slice()
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
    .map((t, i) => ({ ...t, order: i }));
  let res;
  try {
    res = await api("/api/settings", { method: "POST", body: { tabs } });
  } catch (err) {
    toast("Failed to save tab settings", "error");
    return;
  }
  App.settings = res;
  applyAppearance();
  renderTabbar();
  /* if the Settings tab is open, re-render it so the tab manager shows fresh state */
  if (App.currentTab === "settings" && App.tabs.settings) {
    showTab("settings");
  }
}

let dragTabId = null;

function renderTabbar() {
  const bar = document.getElementById("tabbar");
  clear(bar);
  for (const tab of sortedVisibleTabs()) {
    const isActive = App.currentTab === tab.id;
    const tabEl = el("div", {
      class: `tab${isActive ? " active" : ""}`,
      role: "tab", "aria-selected": isActive, dataset: { tabId: tab.id }, draggable: "true",
      style: `--tab-color:${tab.color || "#7c5cff"}`,
    },
      el("span", { class: "tab-dot", style: `background:${tab.color || "#7c5cff"}` }),
      el("span", null, tab.label || tab.id),
      el("button", { class: "tab-caret", title: "Tab options", "aria-label": "Tab options" }, "▾"),
    );
    tabEl.addEventListener("click", (e) => {
      if (e.target.closest(".tab-caret")) return;
      switchTab(tab.id);
    });
    tabEl.querySelector(".tab-caret").addEventListener("click", (e) => {
      e.stopPropagation();
      openTabPopover(tab, e.currentTarget);
    });
    /* drag & drop reorder */
    tabEl.addEventListener("dragstart", (e) => {
      dragTabId = tab.id;
      tabEl.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", tab.id); } catch {}
    });
    tabEl.addEventListener("dragend", () => {
      dragTabId = null;
      tabEl.classList.remove("dragging");
      bar.querySelectorAll(".tab.drag-over").forEach(n => n.classList.remove("drag-over"));
    });
    tabEl.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (dragTabId && dragTabId !== tab.id) tabEl.classList.add("drag-over");
    });
    tabEl.addEventListener("dragleave", () => tabEl.classList.remove("drag-over"));
    tabEl.addEventListener("drop", async (e) => {
      e.preventDefault();
      tabEl.classList.remove("drag-over");
      const fromId = dragTabId || e.dataTransfer.getData("text/plain");
      if (!fromId || fromId === tab.id) return;
      const visible = sortedVisibleTabs();
      const fromIdx = visible.findIndex(t => t.id === fromId);
      const toIdx = visible.findIndex(t => t.id === tab.id);
      if (fromIdx < 0 || toIdx < 0) return;
      const [moved] = visible.splice(fromIdx, 1);
      visible.splice(toIdx, 0, moved);
      visible.forEach((t, i) => { const full = App.settings.tabs.find(x => x.id === t.id); if (full) full.order = i; });
      await persistTabs();
    });
    bar.appendChild(tabEl);
  }
}

/* ---------- caret popover ---------- */
let openPopover = null;
function closePopover() { if (openPopover) { openPopover.remove(); openPopover = null; } }
document.addEventListener("click", (e) => {
  if (openPopover && !openPopover.contains(e.target) && !e.target.closest(".tab-caret")) closePopover();
});

function openTabPopover(tab, anchor) {
  closePopover();
  const pop = el("div", { class: "tab-popover" },
    el("h4", null, "Tab color"),
    el("div", { class: "swatch-row" },
      TAB_COLORS.map(c => el("button", {
        class: `swatch${c === tab.color ? " selected" : ""}`,
        style: `background:${c}`, title: c,
        onclick: async () => { tab.color = c; await persistTabs(); closePopover(); },
      })),
    ),
    el("button", {
      class: "full ghost", disabled: tab.id === ALWAYS_VISIBLE,
      onclick: async () => {
        if (tab.id === ALWAYS_VISIBLE) return;
        if (App.currentTab === tab.id) switchTab(ALWAYS_VISIBLE);
        tab.visible = false;
        await persistTabs();
        closePopover();
      },
    }, "Hide tab"),
  );
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.top = `${r.bottom + 6}px`;
  pop.style.left = `${Math.min(r.left, window.innerWidth - pop.offsetWidth - 12)}px`;
  openPopover = pop;
}

/* ---------- routing ---------- */
function switchTab(id) {
  if (location.hash !== "#" + id) location.hash = "#" + id;
  else showTab(id);
}
function showTab(id) {
  closePopover();
  App.currentTab = id;
  renderTabbar();
  const screen = document.getElementById("screen");
  clear(screen);
  const def = App.tabs[id];
  if (!def) {
    screen.appendChild(emptyState(`Unknown tab: ${id}`, "?"));
    return;
  }
  def.built = false;
  def.render(screen);
  def.built = true;
}
window.addEventListener("hashchange", () => {
  const id = (location.hash || "#overview").slice(1);
  if (id !== App.currentTab) showTab(id);
});
