/* tab-videos.js — add videos/channels, job progress, full video table */
"use strict";

(function () {
  let filter = "";
  let lengthFilter = "";   // "" | "shorts" | "long"
  let jobTimer = null;

  function render(screen) {
    filter = "";
    lengthFilter = "";
    sortKey = null;
    sortDir = 1;
    if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }

    const wrap = el("div", { class: "screen-scroll scroll", style: "display:flex;flex-direction:column;gap:14px" });

    /* add rows */
    const urlTa = el("textarea", { placeholder: "Paste one or more YouTube URLs (one per line)" });
    const chanInput = el("input", { placeholder: "Channel URL (e.g. https://www.youtube.com/@veritasium)" });
    const limitInput = el("input", {
      type: "number", min: "1", value: String((App.settings && App.settings.channel_default_limit) || 10),
    });
    wrap.append(
      el("div", { class: "pane" }, el("div", { class: "pane-body" },
        el("div", { class: "row", style: "align-items:flex-start" },
          el("div", { style: "flex:1" }, urlTa),
          el("button", {
            class: "primary",
            onclick: async (e) => {
              const urls = urlTa.value.split("\n").map(s => s.trim()).filter(Boolean);
              if (!urls.length) { toast("Paste at least one URL", "info"); return; }
              e.currentTarget.disabled = true;
              try { await startJob(e.currentTarget, "/api/videos/add", { urls, channel: null, limit: null }); urlTa.value = ""; }
              finally { e.currentTarget.disabled = false; }
            },
          }, "Add videos"),
        ),
        el("div", { class: "row", style: "margin-top:10px;align-items:flex-end" },
          el("div", { style: "flex:1" }, chanInput),
          el("div", { style: "width:110px" },
            el("label", null, "Limit"), limitInput),
          el("button", {
            onclick: async (e) => {
              const channel = chanInput.value.trim();
              if (!channel) { toast("Enter a channel URL", "info"); return; }
              const limit = parseInt(limitInput.value, 10);
              e.currentTarget.disabled = true;
              try {
                await startJob(e.currentTarget, "/api/videos/add", { urls: [], channel, limit: isNaN(limit) ? null : limit });
                chanInput.value = "";
              } finally { e.currentTarget.disabled = false; }
            },
          }, "Add channel"),
        ),
      )),
      el("div", { id: "job-panel" }),
    );

    /* table */
    const filterInput = el("input", {
      placeholder: "Filter by title…", style: "max-width:280px",
      oninput: (e) => { filter = e.target.value.toLowerCase(); fillTable(); },
    });
    const lenSel = lengthFilterSelect(lengthFilter, m => { lengthFilter = m; fillTable(); });
    wrap.append(
      el("div", { class: "pane", style: "flex:1;min-height:0" },
        el("div", { class: "pane-header" }, el("span", null, "All videos ", el("span", { class: "muted small", id: "videos-count" })), filterInput, lenSel),
        el("div", { class: "scroll", style: "flex:1;min-height:200px", id: "videos-table-box" }, skeletonLines(8)),
      ));
    screen.appendChild(wrap);
    fillTable();
  }

  async function startJob(btn, path, body) {
    const panel = document.getElementById("job-panel");
    try {
      const res = await api(path, { method: "POST", body });
      if (res.job_id) pollJob(res.job_id);
    } catch { /* toast already shown by api() */ }
  }

  function pollJob(jobId) {
    if (jobTimer) clearInterval(jobTimer);
    const panel = document.getElementById("job-panel");
    if (!panel) return;
    clear(panel).appendChild(el("div", { class: "job-panel" },
      el("div", { class: "row" }, el("strong", { id: "job-title" }, "Adding videos…"), el("span", { class: "spacer" }), el("span", { class: "muted small", id: "job-eta" })),
      el("div", { class: "progress-wrap", style: "margin:10px 0 6px" }, el("div", { class: "progress-bar", id: "job-bar" })),
      el("div", { class: "muted small", id: "job-msg" }, "Starting…"),
      el("div", { id: "job-analysis", style: "display:none;margin-top:10px" },
        el("div", { class: "row" },
          el("span", { class: "muted small", id: "job-analysis-label" }, "Analyzing with AI agent"),
          el("span", { class: "spacer" }),
          el("span", { class: "muted small" }, "this can take several minutes per batch — you can keep using the app")),
        el("div", { class: "progress-wrap", style: "margin:6px 0" },
          el("div", { class: "progress-bar", id: "job-analysis-bar", style: "background:var(--success)" })),
      ),
    ));
    const tick = async () => {
      let job;
      try { job = await api(`/api/job/${jobId}`); } catch { stop(); return; }
      const bar = document.getElementById("job-bar");
      const msg = document.getElementById("job-msg");
      const eta = document.getElementById("job-eta");
      if (!bar) { stop(); return; }
      const pct = job.total ? Math.round((job.done_count / job.total) * 100) : 0;
      bar.style.width = pct + "%";
      if (msg) msg.textContent = job.message || job.status;
      if (eta) eta.textContent = (job.eta_seconds != null && job.status === "running")
        ? `ETA ${fmtDur(job.eta_seconds)}` : "";
      /* analysis phase: second progress bar once scraping hands off to the agent */
      const analysisBox = document.getElementById("job-analysis");
      if (analysisBox) {
        const analysisTotal = job.analysis_total || 0;
        const analysisDone = job.analysis_done || 0;
        const show = job.phase === "analyzing" || analysisTotal > 0;
        analysisBox.style.display = show ? "" : "none";
        if (show) {
          const aBar = document.getElementById("job-analysis-bar");
          const aLabel = document.getElementById("job-analysis-label");
          if (aBar) aBar.style.width = analysisTotal ? Math.round((analysisDone / analysisTotal) * 100) + "%" : "0%";
          if (aLabel) aLabel.textContent = `Analyzing with AI agent (${analysisDone}/${analysisTotal})`;
        }
      }
      if (job.status === "done") {
        stop();
        const analyzed = job.analysis_done || 0;
        clear(panel).appendChild(el("div", { class: "success-card" },
          el("strong", null, "Done. "), `${job.done_count}/${job.total} videos added` +
          (analyzed ? `, ${analyzed} analyzed` : "") + "."));
        invalidateVideos();
        App.learningsCache = {};
        if (App.currentTab === "videos") fillTable();
        toast(`Added ${job.done_count} videos` + (analyzed ? `, analyzed ${analyzed}` : ""), "success");
      } else if (job.status === "error") {
        stop();
        panel.querySelector(".job-panel").classList.add("error");
        invalidateVideos();
        if (App.currentTab === "videos") fillTable();
        toast(job.message || "Job failed", "error");
      }
    };
    const stop = () => { if (jobTimer) { clearInterval(jobTimer); jobTimer = null; } };
    jobTimer = setInterval(tick, 1500);
    tick();
  }

  /* ---------- sortable table ---------- */
  let sortKey = null;   // null | "title" | "channel" | "date" | "dur" | "status"
  let sortDir = 1;      // 1 asc, -1 desc

  const SORT_COLS = [
    { key: null, label: "" },
    { key: "title", label: "Title" },
    { key: "channel", label: "Channel" },
    { key: "date", label: "Date" },
    { key: "dur", label: "Dur" },
    { key: "status", label: "Status" },
    { key: null, label: "Actions" },
  ];

  function sortVideos(rows) {
    if (!sortKey) return rows;
    const val = {
      title: v => (v.title || "").toLowerCase(),
      channel: v => (v.channel || "").toLowerCase(),
      date: v => v.published || "",
      dur: v => +v.duration || 0,
      status: v => (v.learned ? 2 : 0) + (v.has_learnings ? 1 : 0),
    }[sortKey];
    return rows.slice().sort((a, b) => {
      const x = val(a), y = val(b);
      return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
    });
  }

  function headerCell(col) {
    if (!col.key) return el("th", null, col.label);
    const active = sortKey === col.key;
    const arrow = active ? (sortDir === 1 ? " ▲" : " ▼") : "";
    return el("th", {
      class: `sortable${active ? " sorted" : ""}`,
      title: "Click to sort",
      onclick: () => {
        if (sortKey === col.key) {
          sortDir = sortDir === 1 ? -1 : 1;
        } else { sortKey = col.key; sortDir = 1; }
        fillTable();
      },
    }, col.label + arrow);
  }

  async function fillTable() {
    const box = document.getElementById("videos-table-box");
    if (!box) return;
    if (!box.querySelector("table")) clear(box).appendChild(skeletonLines(6));
    const videos = await getVideos();
    if (!box.isConnected) return;
    clear(box);
    const cnt = document.getElementById("videos-count");
    const filtered = videos.filter(v => (!filter || v.title.toLowerCase().includes(filter)) && lengthMatch(v, lengthFilter));
    if (cnt) cnt.textContent = (filter || lengthFilter) ? `${filtered.length} of ${videos.length}` : `(${videos.length})`;
    const rows = sortVideos(filtered);
    if (!rows.length) { box.appendChild(emptyState("No videos found.", "∅")); return; }
    const tbody = el("tbody", null, rows.map(v => el("tr", null,
      el("td", null, thumbImg(v)),
      el("td", null, el("div", { style: "font-weight:600" }, v.title),
        el("div", { class: "muted small" }, v.description ? v.description.slice(0, 90) + (v.description.length > 90 ? "…" : "") : "")),
      el("td", { class: "muted small" }, v.channel),
      el("td", { class: "muted small", style: "white-space:nowrap" }, fmtDate(v.published)),
      el("td", { class: "muted small" }, fmtDur(v.duration)),
      el("td", null, el("div", { class: "badge-row" },
        v.is_short ? el("span", { class: "chip" }, "short") : null,
        v.learned ? el("span", { class: "chip success" }, "Learned") : null,
        v.has_transcript ? el("span", { class: "chip" }, "transcript") : null,
        v.has_learnings ? el("span", { class: "chip" }, "analyzed") : null,
      )),
      el("td", null, el("div", { class: "row" },
        el("a", { href: v.url, target: "_blank", rel: "noopener", title: "Open on YouTube" }, "↗"),
        el("button", {
          class: "icon-btn danger",
          onclick: async (e) => {
            if (!confirm(`Delete “${v.title}”?`)) return;
            const btn = e.currentTarget;
            btn.disabled = true;
            try {
              await api(`/api/video/${v.id}`, { method: "DELETE" });
              invalidateVideos(); invalidateLearnings(v.id); App.skillsIndex = null;
              toast("Video deleted", "success");
              fillTable();
            } catch { btn.disabled = false; }
          },
        }, "Delete"),
      )),
    )));
    box.appendChild(el("table", { class: "videos-table" },
      el("thead", null, el("tr", null, SORT_COLS.map(headerCell))),
      tbody));
  }

  App.tabs.videos = { render, built: false };
})();
