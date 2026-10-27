/* tab-console.js — standalone agent console: one persistent conversation for
   the whole app (key console:main, no workspace) */
"use strict";

(function () {
  let agentAvailable = null;       // null = status not fetched yet
  let agentMode = null;            // "pi-durable" | "kimi-cli" | null (from /api/agent/status)
  let job = null;                  // current agent job poll state
  let jobTimer = null;
  const hist = [];                 // [{t, x}] console lines (session-persistent)
  let sessionStarted = false;      // false -> next send starts a new agent session
  let runOnNode = "";              // "" = this machine; a sandbox name = that VM's agent

  /* ---------- entry ---------- */
  function render(screen) {
    const wrap = el("div", { class: "screen-scroll scroll", style: "display:flex;flex-direction:column;gap:14px;max-width:960px;width:100%;margin:0 auto" });

    const consoleBox = el("div", { class: "agent-console scroll", id: "console-box" },
      hist.length ? null : el("div", { class: "cl-meta" },
        "Console — ask anything; this agent can read your app files (data/, skills/, server.py) to answer."));
    hist.forEach(l => consoleBox.appendChild(consoleLine(l)));

    const statusRow = el("div", { class: "row", id: "console-status-row", style: "min-height:22px" });

    /* input — Enter inserts a newline (same contract as the teach console);
       send with the button or Ctrl+Enter */
    const input = el("textarea", {
      id: "console-input", rows: "2",
      placeholder: "Ask anything… (Enter for a new line, Ctrl+Enter to send)",
      onkeydown: (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doSend(); }
      },
    });
    const sendBtn = el("button", { id: "console-send", class: "primary", onclick: () => doSend() }, "Send");

    /* "Run on" — route the console conversation to a VM's in-guest agent.
       Options come from /api/network (vm nodes with a healthy agent); the
       conversation stays key console:main but each node has its OWN one, so
       the header notes where it lives. */
    const runOnSel = el("select", {
      id: "console-runon", style: "min-width:200px",
      title: "Which machine's agent answers — master (this machine) or a VM with an installed agent",
      onchange: () => { runOnNode = runOnSel.value; refreshRunOnNote(); },
    });
    const runOnNote = el("span", { class: "muted small", id: "console-runon-note" });
    const runOnRow = el("div", { class: "row", id: "console-runon-row", style: "display:none" },
      el("span", { class: "muted small" }, "Run on"), runOnSel, runOnNote);
    function refreshRunOnNote() {
      runOnNote.textContent = runOnNode
        ? `conversation console:main on ${runOnNode} — that VM's own conversation (the master's is untouched)`
        : "master (this machine)";
    }
    (async () => {
      let data;
      try { data = await api("/api/network"); }
      catch { return; }
      const vms = (((data || {}).nodes) || []).filter(n => n.kind === "vm" && n.agent);
      if (!vms.length) return;
      runOnSel.appendChild(el("option", { value: "" }, "master (this machine)"));
      vms.forEach(n => runOnSel.appendChild(el("option", { value: n.name }, `${n.name} (this VM's agent)`)));
      if (runOnNode && !vms.some(n => n.name === runOnNode)) runOnNode = "";
      runOnSel.value = runOnNode;
      refreshRunOnNote();
      const row = document.getElementById("console-runon-row");
      if (row && row.isConnected) row.style.display = "";
    })();
    async function doSend() {
      const text = input.value.trim();
      if (!text || (job && isRunning(job))) return;
      const sent = await sendMessage(text);
      if (sent) input.value = ""; /* on failure (e.g. 409) keep the text for a resend */
    }

    const banner = agentAvailable === false
      ? el("div", { class: "instructions-box", style: "margin:0 0 10px" },
          "No AI agent available — the agentd sidecar is down and the kimi CLI is not on PATH. Start the sidecar or install the CLI, then reload.")
      : null;
    const inputRow = agentAvailable === false ? null : el("div", null,
      el("div", { class: "row", style: "align-items:flex-start" }, input, sendBtn,
        el("button", {
          class: "ghost",
          title: "Reset the console conversation — the next message starts fresh",
          onclick: () => { sessionStarted = false; toast("Next send starts a new session", "info"); },
        }, "New session")),
      el("div", { class: "muted small", style: "margin-top:4px" },
        "One conversation for the whole app — it remembers what you asked across visits, until you press New session."));

    wrap.appendChild(el("div", { class: "pane", style: "flex:1;min-height:0" },
      el("div", { class: "pane-header" },
        "Agent console",
        agentMode ? el("span", { class: "chip sm dim", style: "margin-left:8px" },
          agentMode === "pi-durable" ? "Pi Durable (kimi)" : "kimi CLI") : null,
        runOnNode ? el("span", { class: "chip sm", style: "margin-left:8px",
          title: "This console conversation lives on that VM's agent" },
          `on: ${runOnNode}`) : null),
      el("div", { class: "pane-body scroll", style: "flex:1;display:flex;flex-direction:column;gap:8px" },
        banner,
        consoleBox,
        statusRow,
        runOnRow,
        inputRow)));
    screen.appendChild(wrap);
    checkAgent();
  }

  async function checkAgent() {
    try {
      const st = await api("/api/agent/status");
      agentAvailable = st.available;
      agentMode = st.mode || null;
    } catch { agentAvailable = false; agentMode = null; }
    /* re-render the header/input when availability is known and the tab is up */
    const screen = document.getElementById("screen");
    if (screen && agentAvailable === false && !document.getElementById("console-input")
        && !document.querySelector("#screen .instructions-box")) {
      clear(screen);
      render(screen);
    }
  }

  function isRunning(j) {
    return j.status !== "done" && j.status !== "error" && j.status !== "cancelled";
  }

  function consoleLine(l) {
    return el("div", { class: l.t === "err" ? "cl-err" : "cl-out" }, l.x);
  }

  function appendConsole(line) {
    hist.push(line);
    const box = document.getElementById("console-box");
    if (box) {
      const meta = box.querySelector(".cl-meta");
      if (meta) meta.remove();
      box.appendChild(consoleLine(line));
      const pinned = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
      if (pinned) box.scrollTop = box.scrollHeight;
    }
  }

  /* ---------- agent job ---------- */
  async function sendMessage(text) {
    const isNew = !sessionStarted;
    let res;
    try {
      res = await api("/api/agent/console/run", { method: "POST",
        body: { message: text, new_session: isNew, node: runOnNode || undefined } });
    } catch (err) {
      /* 409: a console job is already running — api() toasted the server's
         message; the caller keeps the input text for a later resend */
      return false;
    }
    sessionStarted = true;
    appendConsole({ t: "out", x: `> ${text}` });
    appendConsole({ t: "err", x: "─ agent starting…" });
    pollJob(res.job_id);
    return true;
  }

  function pollJob(jobId) {
    stopJob();
    job = { id: jobId, rendered: 0 };
    const tick = async () => {
      let j;
      try { j = await api(`/api/agent/job/${jobId}`); }
      catch { renderStatus(); return; }
      if (!job || job.id !== jobId) return;
      (j.lines || []).slice(job.rendered).forEach(l => appendConsole(l));
      job.rendered = (j.lines || []).length;
      job.status = j.status;
      renderStatus(j);
      if (!isRunning(j)) {
        stopJob();
        appendConsole({ t: "err", x: `─ agent ${j.status}${j.exit_code != null ? ` (exit ${j.exit_code})` : ""}` });
        return;
      }
    };
    jobTimer = setInterval(tick, 1500);
    tick();
  }

  function renderStatus(j) {
    const row = document.getElementById("console-status-row");
    if (row) {
      clear(row);
      if (j && isRunning(j)) {
        row.append(
          el("span", { class: "pulse-dot" }),
          el("span", { class: "muted small" }, `agent is working… ${j.cmd_preview ? `(${j.cmd_preview})` : ""}`),
          el("span", { class: "spacer" }),
          el("button", {
            class: "icon-btn danger",
            onclick: async () => {
              await api(`/api/agent/job/${j.id}/cancel`, { method: "POST" }).catch(() => {});
              stopJob();
              appendConsole({ t: "err", x: "─ cancelled by user" });
              renderStatus(null);
            },
          }, "Cancel"),
        );
      }
    }
    const sendBtn = document.getElementById("console-send");
    if (sendBtn) sendBtn.disabled = (j && isRunning(j)) || null;
  }

  function stopJob() {
    if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
    job = null;
    const row = document.getElementById("console-status-row");
    if (row) clear(row);
    const sendBtn = document.getElementById("console-send");
    if (sendBtn) sendBtn.disabled = null;
  }

  App.tabs.console = { render, built: false };
})();
