/* tab-network.js — agent network view: one node per machine (this one, plus
   cua sandboxes as VM nodes), its durable-agent sessions listed beneath,
   LAN-ready shape */
"use strict";

(function () {
  const TYPE_META = {
    teach:    { label: "teach",    icon: "🎓" },
    console:  { label: "console",  icon: "⌨" },
    distill:  { label: "distill",  icon: "⚗" },
    analysis: { label: "analysis", icon: "🔍" },
  };

  function render(screen) {
    const wrap = el("div", { class: "screen-scroll scroll", style: "display:flex;flex-direction:column;gap:14px;max-width:960px;width:100%;margin:0 auto" });
    wrap.appendChild(el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Network ",
        el("span", { class: "muted small", id: "net-summary" }, "…")),
      el("div", { class: "pane-body", id: "net-body" }, skeletonLines(4))));
    screen.appendChild(wrap);
    load();
  }

  let loading = false;
  async function load() {
    if (loading) return;
    loading = true;
    let data;
    try { data = await api("/api/network"); }
    catch { data = { nodes: [] }; }
    loading = false;
    const body = document.getElementById("net-body");
    if (!body || !body.isConnected) return;
    const nodes = (data && data.nodes) || [];
    const online = nodes.filter(n => n.online).length;
    const summary = document.getElementById("net-summary");
    if (summary) summary.textContent = `— ${online} node${online === 1 ? "" : "s"} online`;
    clear(body);
    if (!nodes.length) {
      body.appendChild(emptyState("No nodes known yet.", "∅"));
      return;
    }
    nodes.forEach(node => body.appendChild(nodeCard(node)));
    if (nodes.length === 1 && nodes[0].kind === "local"
        && !(nodes[0].sessions || []).length) {
      body.appendChild(emptyState(
        "Only this machine so far — LAN clients will appear here.", "◌"));
    }
  }

  function nodeCard(node) {
    const sessions = node.sessions || [];
    if (!sessions.length && node.kind !== "local") {
      /* VM nodes (cua sandboxes) have no agent sessions — say what they are
         instead of showing an empty, collapsible list */
      return el("div", { class: "pane", style: "padding:0" },
        el("div", { style: "padding:10px 12px" },
          el("div", { class: "row", style: "flex-wrap:wrap" },
            el("span", { class: `pulse-dot${node.online ? "" : " off"}`, title: node.online ? "online" : "offline" }),
            el("strong", null, node.name || node.id),
            el("span", { class: "chip sm dim" }, node.kind),
            el("span", { class: "spacer" }),
            el("span", { class: "muted small" },
              "client machine — open the app URL inside its browser"))));
    }
    const expanded = sessions.length > 0 && sessions.length <= 6;
    const list = el("div", { style: expanded ? "" : "display:none" });
    sessions.forEach(s => list.appendChild(sessionRow(s)));
    const caret = el("span", { class: "muted small" }, expanded ? "▾" : "▸");
    const head = el("div", {
      class: "row", style: "cursor:pointer;flex-wrap:wrap",
      onclick: () => {
        const open = list.style.display !== "none";
        list.style.display = open ? "none" : "";
        caret.textContent = open ? "▸" : "▾";
      },
    },
      el("span", { class: `pulse-dot${node.online ? "" : " off"}`, title: node.online ? "online" : "offline" }),
      el("strong", null, node.name || node.id),
      el("span", { class: "chip sm dim" }, node.kind || "local"),
      el("span", { class: "muted small" }, `${sessions.length} session${sessions.length === 1 ? "" : "s"}`),
      el("span", { class: "spacer" }),
      caret);
    return el("div", { class: "pane", style: "padding:0" },
      el("div", { style: "padding:10px 12px" }, head),
      list);
  }

  function sessionRow(s) {
    const meta = TYPE_META[s.type] || { label: s.type || "session", icon: "◌" };
    const dotCls = s.busy ? "pulse-dot" : (s.status === "error" ? "pulse-dot err" : "pulse-dot off");
    const dotTitle = s.busy ? "busy" : (s.status ? `last: ${s.status}` : "idle");
    return el("div", { class: "row", style: "padding:6px 12px;border-top:1px solid var(--border);flex-wrap:wrap" },
      el("span", { class: dotCls, title: dotTitle }),
      el("span", { class: "chip sm" }, `${meta.icon} ${meta.label}`),
      el("span", { style: "flex:1;min-width:120px", title: s.cwd || "" }, s.title || s.key),
      el("span", { class: "muted small", title: s.key || "" }, s.busy ? "working…" : (s.status || "idle")));
  }

  // auto-refresh while this tab is on screen (showTab clears the screen on
  // every tab switch, so the isConnected guard stops updates once you leave)
  setInterval(() => {
    if (document.getElementById("net-body")?.isConnected) load();
  }, 3000);

  App.tabs.network = { render, built: false };
})();
