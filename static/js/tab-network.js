/* tab-network.js — agent network view: one node per machine (this one, plus
   cua sandboxes as VM nodes), its durable-agent sessions listed beneath,
   LAN-ready shape */
"use strict";

(function () {
  const TYPE_META = {
    teach:    { label: "teach",    icon: "🎓", color: "#34d399" },
    console:  { label: "console",  icon: "⌨", color: "#60a5fa" },
    distill:  { label: "distill",  icon: "⚗", color: "#c084fc" },
    analysis: { label: "analysis", icon: "🔍", color: "#94a3b8" },
    vmtask:   { label: "task",     icon: "🛠", color: "#fbbf24" },
  };

  function typeChip(s) {
    /* the four known session types get distinct chip colors — a glance tells
       what a session is; unknown types fall back to the plain chip */
    const meta = TYPE_META[s.type] || { label: s.type || "session", icon: "◌" };
    const style = meta.color
      ? `background: color-mix(in srgb, ${meta.color} 16%, transparent);`
        + ` color: ${meta.color}; border-color: color-mix(in srgb, ${meta.color} 38%, transparent)`
      : null;
    return el("span", { class: "chip sm", style }, `${meta.icon} ${meta.label}`);
  }

  function connectionHint(node) {
    /* a remote node is a presence beacon from another host — it may have no
       agent at all; with a reachable endpoint its sessions are listed above */
    if (node.remote) {
      if (!node.has_endpoint) {
        return "registered by its host — no agent endpoint; presence only";
      }
      return "remote agent endpoint — its sessions are listed above";
    }
    /* a VM with an installed agent doesn't need the browser workaround — its
       agent runs natively in the guest and its sessions are listed above */
    if (node.agent) {
      return "agent runs here — its sessions are listed above";
    }
    /* how a browser inside this VM reaches the app — a qemu VM sees the host
       as 10.0.2.2; a container shares the host's network, so the plain LAN
       URL applies. No flavor (older data): the generic line. */
    if (node.flavor === "qemu") {
      return "inside this VM open http://10.0.2.2:8471 "
        + "(the browser will ask for the LAN token)";
    }
    if (node.flavor === "container") {
      return "inside it open the LAN URL from Settings → LAN / other devices";
    }
    return "client machine — open the app URL inside its browser";
  }

  function stateChip(node) {
    /* raw sandbox state (running/suspended/…) — shown only when not running;
       node.online reflects the in-guest agent, not this */
    if (!node.vm_state || node.vm_state === "running") return null;
    return el("span", { class: "chip sm dim", title: "sandbox state" }, node.vm_state);
  }

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
    const stopped = nodes.filter(n => !n.online && n.kind !== "local");
    const shown = nodes.filter(n => !stopped.includes(n));
    shown.forEach(node => body.appendChild(nodeCard(node)));
    if (stopped.length) {
      const list = el("div", { style: "display:none;margin-top:8px" });
      stopped.forEach(node => list.appendChild(nodeCard(node)));
      const label = el("span", { class: "muted small" },
        `▸ stopped VMs (${stopped.length}) — click to show`);
      const toggle = el("div", {
        class: "row", style: "gap:8px;cursor:pointer;padding:8px 12px;"
          + "border:1px solid var(--border);border-radius:12px;margin-top:8px",
        onclick: () => {
          const open = list.style.display === "none";
          list.style.display = open ? "" : "none";
          label.textContent = (open ? "▾" : "▸")
            + ` stopped VMs (${stopped.length}) — click to ${open ? "hide" : "show"}`;
        },
      }, label);
      body.appendChild(toggle);
      body.appendChild(list);
    }
    if (nodes.length === 1 && nodes[0].kind === "local"
        && !(nodes[0].sessions || []).length) {
      body.appendChild(emptyState(
        "Only this machine so far — LAN clients will appear here.", "◌"));
    }
  }

  function nodeCard(node) {
    const sessions = node.sessions || [];
    const collapsible = sessions.length > 0 || node.kind === "local";
    const expanded = sessions.length > 0 && sessions.length <= 6;
    /* the sessions list lives inside the card, indented under the header —
       the list node is created first so the header toggle can close over it */
    const list = el("div", { class: "net-node-sessions", style: expanded ? "" : "display:none" });
    sessions.forEach(s => list.appendChild(sessionRow(s)));
    const caret = el("span", { class: "muted small", style: "flex:none" }, collapsible ? (expanded ? "▾" : "▸") : "");
    const head = el("div", {
      class: "net-node-head row",
      style: collapsible ? "cursor:pointer;flex-wrap:wrap" : "flex-wrap:wrap",
      onclick: () => {
        if (!collapsible) return;
        const open = list.style.display !== "none";
        list.style.display = open ? "none" : "";
        caret.textContent = open ? "▸" : "▾";
      },
    },
      el("span", { class: `pulse-dot${node.online ? "" : " off"}`, title: node.online ? "online" : "offline" }),
      el("strong", { style: "flex:none" }, node.name || node.id),
      el("span", { class: "chip sm dim" }, node.kind || "local"),
      node.remote ? el("span", { class: "chip sm dim", title: "registered by a remote host" },
        `remote @ ${node.host || "?"}`) : null,
      stateChip(node),
      sessions.length ? el("span", { class: "muted small", style: "flex:none" },
        `${sessions.length} session${sessions.length === 1 ? "" : "s"}`) : null,
      el("span", { class: "spacer" }),
      sessions.length
        ? (node.agent ? el("span", { class: "muted small" }, "agent runs here") : null)
        : el("span", { class: "muted small", style: "text-align:right" }, connectionHint(node)),
      caret);
    return el("div", { class: "net-node" }, head, collapsible ? list : null);
  }

  function sessionRow(s) {
    const dotCls = s.busy ? "pulse-dot" : (s.status === "error" ? "pulse-dot err" : "pulse-dot off");
    const dotTitle = s.busy ? "busy" : (s.status ? `last: ${s.status}` : "idle");
    return el("div", { class: "row", style: "padding:6px 6px;border-top:1px solid var(--border);flex-wrap:wrap" },
      el("span", { class: dotCls, title: dotTitle }),
      typeChip(s),
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
