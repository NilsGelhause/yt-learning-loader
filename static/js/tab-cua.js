/* tab-cua.js — CUA Spaces: create Ubuntu VMs (cua sandboxes) that act as
   LAN clients of this app. Creation streams as an agent-style job (image
   downloads take minutes); the list auto-refreshes while the tab is open. */
"use strict";

(function () {
  const INSTALL_WIN = "irm https://cua.ai/install.ps1 | iex";
  const INSTALL_NIX = "curl -fsSL https://cua.ai/install.sh | bash";

  let pollTimer = null;      // status auto-refresh
  const jobs = new Map();    // running/polling create/remove/delegate jobs
  let jobTimer = null;       // shared poller for everything in `jobs`

  function render(screen) {
    stopPoll();
    const wrap = el("div", { class: "screen-scroll scroll", style: "display:flex;flex-direction:column;gap:14px;max-width:960px;width:100%;margin:0 auto" });
    wrap.appendChild(el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "CUA Spaces ",
        el("span", { class: "muted small", id: "cua-summary" }, "…")),
      el("div", { class: "pane-body", id: "cua-body" }, skeletonLines(3))));
    screen.appendChild(wrap);
    load();
  }

  function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  async function load() {
    let data;
    try { data = await api("/api/cua/status"); }
    catch { data = null; }
    const body = document.getElementById("cua-body");
    if (!body || !body.isConnected) return;
    clear(body);
    if (!data || !data.installed) return renderNotInstalled(body, data);
    renderInstalled(body, data);
    /* auto-refresh the sandbox table while the tab is open (showTab clears
       the screen on tab switch, so isConnected stops the polling) */
    stopPoll();
    pollTimer = setInterval(async () => {
      const b = document.getElementById("cua-body");
      if (!b || !b.isConnected) return stopPoll();
      let d;
      try { d = await api("/api/cua/status"); }
      catch { return; }
      /* keep create/remove controls and any running job output untouched —
         only swap the table + summary */
      if (d && d.installed) refreshTable(d);
      else { clear(b); renderNotInstalled(b, d); stopPoll(); }
    }, 4000);
  }

  /* ---------- not installed ---------- */

  function renderNotInstalled(body, data) {
    const summary = document.getElementById("cua-summary");
    if (summary) summary.textContent = "— cua CLI not found";
    body.append(
      el("div", { class: "instructions-box" },
        el("strong", null, "Spin up VMs that use this app as LAN clients. "),
        "CUA (",
        el("a", { href: "https://github.com/trycua/cua", target: "_blank", rel: "noopener" }, "trycua/cua"),
        ") is an open-source “computers for AI agents” project. Its CLI creates Ubuntu ",
        "sandboxes (gVisor containers — on Windows they need WSL2); each VM just needs a ",
        "browser pointed at this machine’s LAN address. No sign-in is needed for local ",
        "sandboxes."),
      el("div", { class: "row", style: "flex-wrap:wrap" },
        el("code", { style: "flex:1;min-width:260px" }, INSTALL_WIN),
        el("button", { onclick: (e) => copyText(INSTALL_WIN, e.target) }, "Copy")),
      el("div", { class: "row", style: "flex-wrap:wrap" },
        el("code", { style: "flex:1;min-width:260px" }, INSTALL_NIX),
        el("button", { onclick: (e) => copyText(INSTALL_NIX, e.target) }, "Copy")),
      el("div", { class: "muted small" },
        "Paste the command in a terminal (PowerShell on Windows) and let the installer ",
        "finish — it is interactive, so this app never runs it for you."),
      el("div", { class: "row", style: "flex-wrap:wrap" },
        el("a", { href: "/cua-spaces-infographic.html", target: "_blank" },
          "What is CUA Spaces? (infographic) →"),
        el("button", {
          class: "primary",
          onclick: () => { const b = document.getElementById("cua-body"); if (b) { clear(b); b.appendChild(skeletonLines(3)); } load(); },
        }, "Check again")),
    );
    if (data && data.note) body.appendChild(el("div", { class: "muted small" }, data.note));
  }

  /* ---------- installed ---------- */

  function renderInstalled(body, data) {
    const summary = document.getElementById("cua-summary");
    if (summary) summary.textContent = `— cua ${data.version || "?"} installed`;

    const nameInput = el("input", {
      id: "cua-name", placeholder: "vm name (lowercase, dashes)",
      style: "flex:1;min-width:200px",
      onkeydown: (e) => { if (e.key === "Enter") createBtn.click(); },
    });
    const createBtn = el("button", {
      class: "primary",
      onclick: () => doCreate(nameInput),
    }, "Create VM");
    const createThreeBtn = el("button", {
      title: "Create vm-1, vm-2 and vm-3 (existing ones are skipped)",
      onclick: () => doCreateThree(),
    }, "Create 3 VMs");

    body.append(
      el("div", { class: "row", style: "flex-wrap:wrap" },
        nameInput, createBtn, createThreeBtn,
        el("span", { class: "chip sm dim" }, "Ubuntu"),
        el("span", { class: "muted small" },
          "created as ytl-<name> — the first creation downloads the image, which can take minutes")),
      tablePane(data),
      el("div", { class: "agent-console scroll", id: "cua-job", style: "max-height:220px" }),
      usageHint(),
    );
  }

  function tablePane(data) {
    return el("div", { class: "pane", style: "padding:0" },
      el("div", { class: "pane-header" }, "Sandboxes"),
      el("div", { class: "pane-body", id: "cua-table" }, tableBody(data.sandboxes || [])));
  }

  function refreshTable(data) {
    const box = document.getElementById("cua-table");
    if (!box) return;
    clear(box);
    box.appendChild(tableBody(data.sandboxes || []));
  }

  function tableBody(sandboxes) {
    if (!sandboxes.length) {
      return emptyState("No sandboxes yet — create one above.", "◌");
    }
    const rows = sandboxes.map(sb => {
      const taskInput = el("input", {
        placeholder: "delegate a task…",
        style: "flex:1;min-width:160px",
        onkeydown: (e) => { if (e.key === "Enter") delegateBtn.click(); },
      });
      const delegateBtn = el("button", {
        title: `Delegate a task to ${sb.name} — the agent runs its commands on this VM`,
        onclick: () => doDelegate(sb.name, taskInput),
      }, "Delegate");
      return el("div", { class: "row", style: "padding:6px 12px;border-top:1px solid var(--border);flex-wrap:wrap" },
        el("span", { class: `pulse-dot${sb.status === "running" ? "" : " off"}`, title: sb.status || "unknown" }),
        el("strong", { style: "min-width:160px" }, sb.name),
        el("span", { class: "chip sm dim" }, sb.status || "unknown"),
        el("span", { class: "muted small", style: "flex:1" }, sb.image || ""),
        taskInput, delegateBtn,
        el("button", {
          class: "icon-btn danger",
          title: `Delete ${sb.name}`,
          onclick: () => doRemove(sb.name),
        }, "🗑"));
    });
    return el("div", null, rows);
  }

  function usageHint() {
    const slot = el("div", { class: "muted small", id: "cua-hint" },
      "How do I use a VM? Open the app inside the sandbox’s browser — the LAN address of this machine appears here…");
    (async () => {
      let lan;
      try { lan = await api("/api/lan"); } catch { return; }
      const hint = document.getElementById("cua-hint");
      if (!hint || !hint.isConnected) return;
      const urls = (lan && lan.urls) || [];
      hint.textContent = urls.length
        ? `How do I use a VM? Inside the sandbox open a browser at ${urls.join(" or ")}${lan.token_set ? " (the browser will ask for the LAN token)" : ""}.`
        : "How do I use a VM? The server currently binds 127.0.0.1 — set Host to 0.0.0.0 in Settings → LAN / other devices (restart required) so VMs can reach it, and the URL will appear here.";
    })();
    return slot;
  }

  /* ---------- create / remove / delegate jobs ---------- */

  async function doCreate(input) {
    const raw = (input.value || "").trim();
    if (!raw || anyJobRunning()) return;
    let res;
    try {
      res = await api("/api/cua/sandboxes", { method: "POST", body: { name: raw } });
    } catch { return; }  /* api() toasted the reason (409/400/503) */
    input.value = "";
    toast(`creating ytl-${raw.toLowerCase().replace(/^ytl-/, "")}…`);
    startJob(res.job_id, `create ytl-${raw.toLowerCase().replace(/^ytl-/, "")}`);
  }

  async function doCreateThree() {
    /* the classic trio in one click: create vm-1/vm-2/vm-3 sequentially,
       skipping any that already exist */
    if (anyJobRunning()) return;
    let data;
    try { data = await api("/api/cua/status"); }
    catch { return; }
    const existing = new Set((data.sandboxes || []).map(s => s.name));
    for (const n of ["vm-1", "vm-2", "vm-3"]) {
      const full = `ytl-${n}`;
      if (existing.has(full)) { toast(`${full} already exists — skipped`); continue; }
      try {
        const res = await api("/api/cua/sandboxes", { method: "POST", body: { name: n } });
        toast(`creating ${full}…`);
        startJob(res.job_id, `create ${full}`);
      } catch { /* api() toasted the reason; try the next one */ }
    }
  }

  async function doRemove(name) {
    if (!confirm(`Delete sandbox ${name}? This cannot be undone.`)) return;
    let res;
    try {
      res = await api("/api/cua/sandboxes/remove", { method: "POST", body: { name } });
    } catch { return; }
    startJob(res.job_id, `remove ${name}`);
  }

  async function doDelegate(name, input) {
    /* delegate-a-task v1: the agent thinks in the master's durable
       conversation, its commands run ON the VM via `cua sb exec` */
    const task = (input.value || "").trim();
    if (!task) return;
    let res;
    try {
      res = await api("/api/agent/vm-task", { method: "POST", body: { vm: name, task } });
    } catch { return; }
    input.value = "";
    startJob(res.job_id, `task ${name}`);
  }

  function isRunning(j) {
    return j.status !== "done" && j.status !== "error" && j.status !== "cancelled";
  }

  function anyJobRunning() {
    return jobs.size > 0;
  }

  function startJob(jobId, label) {
    jobs.set(jobId, { rendered: 0, label });
    if (!jobTimer) jobTimer = setInterval(tickJobs, 1500);
    tickJobs();
  }

  async function tickJobs() {
    await Promise.all([...jobs.keys()].map(async (jobId) => {
      const state = jobs.get(jobId);
      let j;
      try { j = await api(`/api/agent/job/${jobId}`); }
      catch { return; }
      if (!jobs.has(jobId)) return;
      (j.lines || []).slice(state.rendered).forEach(l => appendJob(l));
      state.rendered = (j.lines || []).length;
      if (!isRunning(j)) {
        jobs.delete(jobId);
        appendJob({ t: "err", x: `─ ${state.label}: ${j.status}${j.exit_code != null ? ` (exit ${j.exit_code})` : ""}` });
        refreshStatus();  /* fresh sandbox list, keep the transcript */
      }
    }));
    if (!jobs.size && jobTimer) { clearInterval(jobTimer); jobTimer = null; }
  }

  async function refreshStatus() {
    let d;
    try { d = await api("/api/cua/status"); } catch { return; }
    if (d && d.installed) refreshTable(d);
  }

  function appendJob(line) {
    const box = document.getElementById("cua-job");
    if (!box) return;
    box.appendChild(el("div", { class: line.t === "err" ? "cl-err" : "cl-out" }, line.x));
  }

  App.tabs.cua = { render, built: false };
})();
