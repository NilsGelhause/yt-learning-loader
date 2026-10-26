/* tab-tests.js — test suite results: summary tiles, filters, sortable table */
"use strict";

(function () {
  let results = null;      // last fetched /api/test-results payload
  let running = false;
  let search = "";
  let statusFilter = "all"; // all | passed | failed | skipped
  let moduleFilter = "all";
  let groupByModule = false;
  let sortKey = "name";     // name | module | duration | status
  let sortDir = 1;

  const STATUS_LABEL = { passed: "Passed", failed: "Failed", error: "Error", skipped: "Skipped" };
  const STATUS_FILTERS = [
    ["all", "All"], ["passed", "Passed"], ["failed", "Failed"], ["skipped", "Skipped"],
  ];

  function render(screen) {
    search = ""; statusFilter = "all"; moduleFilter = "all";
    groupByModule = false; sortKey = "name"; sortDir = 1;
    const wrap = el("div", { class: "screen-scroll scroll", style: "display:flex;flex-direction:column;gap:14px" });
    wrap.appendChild(el("div", { id: "tests-content" }, skeletonLines(8)));
    screen.appendChild(wrap);
    refresh();
  }

  async function refresh() {
    const box = document.getElementById("tests-content");
    if (!box) return;
    try { results = await api("/api/test-results"); }
    catch { clear(box).appendChild(emptyState("Could not load test results.", "✕")); return; }
    if (!box.isConnected) return;
    renderContent(box);
  }

  function renderContent(box) {
    clear(box);
    if (!results || !results.total) { renderEmpty(box); return; }

    /* ---- summary tiles ---- */
    const health = results.failed > 0 ? "bad" : "good";
    box.appendChild(el("div", { class: "stat-tiles", style: "grid-template-columns:repeat(auto-fill,minmax(130px,1fr))" },
      tile("Total", results.total, ""),
      tile("Passed", results.passed, "good"),
      tile("Failed", results.failed, results.failed ? "bad" : ""),
      tile("Duration", fmtDur(results.duration), health === "bad" ? "" : ""),
      tile("Last run", results.ran_at ? fmtDateTime(results.ran_at) : "—", ""),
    ));

    /* ---- controls row ---- */
    const modules = [...new Set((results.tests || []).map(t => t.module || ""))].sort((a, b) => a.localeCompare(b));
    const searchInput = el("input", {
      placeholder: "Search tests…", style: "max-width:260px", value: search,
      oninput: (e) => { search = e.target.value.toLowerCase(); renderTableRegion(); },
    });
    const moduleSel = el("select", {
      style: "max-width:200px",
      onchange: (e) => { moduleFilter = e.target.value; renderTableRegion(); },
    },
      el("option", { value: "all" }, "All modules"),
      modules.map(m => el("option", { value: m, selected: moduleFilter === m || null }, m || "(none)")));
    const groupChk = el("input", {
      type: "checkbox", checked: groupByModule || null,
      onchange: (e) => { groupByModule = e.target.checked; renderTableRegion(); },
    });
    const chipRow = el("div", { class: "row", style: "flex-wrap:wrap" },
      STATUS_FILTERS.map(([val, label]) => el("button", {
        class: `chip filter-chip${statusFilter === val ? " on" : ""}`,
        onclick: (e) => {
          statusFilter = val;
          chipRow.querySelectorAll(".filter-chip").forEach(c => c.classList.remove("on"));
          e.currentTarget.classList.add("on");
          renderTableRegion();
        },
      }, label)),
    );
    box.appendChild(el("div", { class: "pane" }, el("div", { class: "pane-body" },
      el("div", { class: "row wrap" },
        searchInput,
        chipRow,
        moduleSel,
        el("label", { class: "row", style: "gap:6px;margin:0;color:var(--muted);font-size:13px" },
          groupChk, "Group by module"),
        el("span", { class: "spacer" }),
        el("button", {
          class: "primary",
          disabled: running || null,
          onclick: runTests,
        }, running ? "Running tests…" : "Run tests"),
      ))));

    /* ---- table region (re-rendered on filter/sort changes) ---- */
    box.appendChild(el("div", { id: "tests-table-region" }));
    renderTableRegion();
  }

  function tile(label, value, cls) {
    return el("div", { class: `stat-tile${cls ? " " + cls : ""}` },
      el("div", { class: "st-value" }, String(value)),
      el("div", { class: "st-label" }, label));
  }

  function renderEmpty(box) {
    box.appendChild(el("div", { class: "pane" }, el("div", { class: "pane-body", style: "text-align:center;padding:44px 20px" },
      el("div", { class: "empty-state", style: "padding:0 0 18px" },
        el("span", { class: "icon" }, "🧪"),
        (results && results.note) || "The test suite hasn't been run yet."),
      el("button", {
        class: "primary", disabled: running || null, onclick: runTests,
      }, running ? "Running tests…" : "Run tests"))));
  }

  async function runTests(e) {
    if (running) return;
    running = true;
    document.querySelectorAll("#tests-content button.primary").forEach(b => { b.disabled = true; b.textContent = "Running tests…"; });
    try {
      results = await api("/api/tests/run", { method: "POST", body: {} });
      const box = document.getElementById("tests-content");
      if (box && box.isConnected) {
        running = false;
        renderContent(box);
      }
      const msg = results.total
        ? `Tests: ${results.passed}/${results.total} passed` + (results.failed ? `, ${results.failed} failed` : "")
        : "Test run finished";
      toast(msg, results.failed ? "error" : "success");
    } catch (err) {
      running = false;
      if (err.status === 409) toast("A test run is already in progress", "info");
      refresh();
    }
  }

  /* ---------- filtering / sorting ---------- */
  function visibleTests() {
    let rows = (results && results.tests) || [];
    if (statusFilter !== "all") {
      rows = rows.filter(t => statusFilter === "failed"
        ? (t.status === "failed" || t.status === "error")
        : t.status === statusFilter);
    }
    if (moduleFilter !== "all") rows = rows.filter(t => (t.module || "") === moduleFilter);
    if (search) {
      rows = rows.filter(t =>
        (t.name || "").toLowerCase().includes(search) ||
        (t.description || "").toLowerCase().includes(search) ||
        (t.module || "").toLowerCase().includes(search) ||
        (t.class || "").toLowerCase().includes(search) ||
        (t.status || "").toLowerCase().includes(search));
    }
    return sortTests(rows);
  }

  function sortTests(rows) {
    const val = {
      name: t => (t.name || "").toLowerCase(),
      module: t => (t.module || "").toLowerCase(),
      duration: t => +t.duration || 0,
      status: t => t.status || "",
    }[sortKey];
    return rows.slice().sort((a, b) => {
      const x = val(a), y = val(b);
      return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
    });
  }

  function header(label, key) {
    const active = sortKey === key;
    return el("th", {
      class: `sortable${active ? " sorted" : ""}`,
      onclick: () => {
        if (sortKey === key) sortDir = sortDir === 1 ? -1 : 1;
        else { sortKey = key; sortDir = 1; }
        renderTableRegion();
      },
    }, label + (active ? (sortDir === 1 ? " ▲" : " ▼") : ""));
  }

  function testRow(t) {
    const failedish = t.status === "failed" || t.status === "error";
    const tr = el("tr", null,
      el("td", null, el("span", { class: `badge-dot st-${t.status}` }), " ",
        el("span", { class: `st-word st-${t.status}` }, STATUS_LABEL[t.status] || t.status)),
      el("td", { style: "font-weight:600" }, titleCase(t.name)),
      el("td", { class: "muted small" }, t.module || ""),
      el("td", { class: "muted small", style: "min-width:280px" }, t.description || ""),
      el("td", { class: "muted small", style: "white-space:nowrap;text-align:right" },
        t.duration != null ? `${Math.round(t.duration * 1000)} ms` : ""),
    );
    if (failedish && t.message) {
      return [tr, el("tr", { class: "msg-row" }, el("td", { colspan: "5" },
        el("pre", { class: "test-msg" }, t.message.slice(0, 300) + (t.message.length > 300 ? "…" : ""))))];
    }
    return [tr];
  }

  function titleCase(name) {
    return String(name || "").replace(/\b\w/g, c => c.toUpperCase());
  }

  function renderTableRegion() {
    const region = document.getElementById("tests-table-region");
    if (!region || !results) return;
    clear(region);
    const rows = visibleTests();
    if (!rows.length) { region.appendChild(el("div", { class: "pane" }, el("div", { class: "pane-body" },
      emptyState("No tests match the current filters.", "∅")))); return; }

    if (groupByModule) {
      const byMod = {};
      rows.forEach(t => { const m = t.module || "(none)"; (byMod[m] = byMod[m] || []).push(t); });
      for (const m of Object.keys(byMod).sort((a, b) => a.localeCompare(b))) {
        region.appendChild(el("div", { class: "section-title" }, `${m} (${byMod[m].length})`));
        region.appendChild(tableFor(byMod[m]));
      }
    } else {
      region.appendChild(tableFor(rows));
    }
  }

  function tableFor(rows) {
    const tbody = el("tbody", null, rows.flatMap(testRow));
    return el("div", { class: "pane", style: "margin-bottom:14px;overflow:hidden" },
      el("div", { class: "scroll", style: "max-height:60vh" },
        el("table", { class: "videos-table" },
          el("thead", null, el("tr", null,
            header("Status", "status"),
            header("Name", "name"),
            header("Module", "module"),
            el("th", null, "Description"),
            header("Duration", "duration"))),
          tbody)));
  }

  /* ---------- helpers ---------- */
  function fmtDateTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) +
      ", " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
  }

  App.tabs.tests = { render, built: false };
})();
