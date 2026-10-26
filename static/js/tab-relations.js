/* tab-relations.js — skill-relation graph across selected videos */
"use strict";

(function () {
  let checked = new Set();
  let allVideos = [];        // videos with learnings
  let skillsByVideo = {};    // id -> [skill names lowercased]
  let searchText = "";
  let skillText = "";

  function render(screen) {
    checked = new Set();
    searchText = "";
    skillText = "";
    const grid = el("div", { class: "duo-grid screen-scroll" });

    const searchInput = el("input", {
      placeholder: "Search title…",
      oninput: (e) => { searchText = e.target.value.toLowerCase(); renderListRows(); },
    });
    const skillInput = el("input", {
      list: "rel-skills-dl",
      placeholder: "Filter by skill (type to search)…",
      autocomplete: "off",
      oninput: (e) => { skillText = e.target.value.trim().toLowerCase(); renderListRows(); },
    });
    const listPane = el("div", { class: "pane picker-pane" },
      el("div", { class: "pane-header" }, el("span", null, "Videos with learnings ", el("span", { class: "muted small", id: "rel-count" })),
        el("div", { class: "row" },
          el("button", { class: "icon-btn", onclick: () => setAll(true) }, "Select all"),
          el("button", { class: "icon-btn", onclick: () => setAll(false) }, "None"),
        )),
      el("div", { style: "padding:10px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:6px" },
        searchInput, skillInput, el("datalist", { id: "rel-skills-dl" })),
      el("div", { class: "scroll chk-list", id: "rel-list" }, skeletonLines(6)),
      el("div", { style: "padding:10px;border-top:1px solid var(--border)" },
        el("button", { class: "primary", style: "width:100%", onclick: renderGraph }, "Render graph")),
    );
    const graphPane = el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Skill graph"),
      el("div", {
        class: "hint-bar", id: "rel-hint",
        style: "display:none",
      }),
      el("div", { class: "diagram-canvas scroll relations-graph", id: "rel-canvas" },
        emptyState("Select videos and hit “Render graph”.", "◇")),
      el("div", { class: "pane-body", id: "rel-legend", style: "border-top:1px solid var(--border)" }),
    );

    grid.append(listPane, graphPane);
    screen.appendChild(grid);
    fillList();
  }

  async function fillList() {
    const box = document.getElementById("rel-list");
    if (!box) return;
    allVideos = (await getVideos()).filter(v => v.has_learnings);
    if (!allVideos.length) { clear(box).appendChild(emptyState("No videos with learnings yet.", "∅")); return; }
    allVideos.forEach(v => checked.add(v.id));
    /* collect skills for the filter + datalist */
    const seen = new Map();
    await Promise.all(allVideos.map(v =>
      getLearnings(v.id).then(l => {
        skillsByVideo[v.id] = (l && l.skills ? l.skills : []).map(s => {
          const name = s.name || "";
          if (!seen.has(name.toLowerCase())) seen.set(name.toLowerCase(), name);
          return name.toLowerCase();
        });
      }).catch(() => { skillsByVideo[v.id] = []; }),
    ));
    const dl = document.getElementById("rel-skills-dl");
    if (dl && dl.isConnected) {
      clear(dl);
      [...seen.values()].sort((a, b) => a.localeCompare(b))
        .forEach(name => dl.appendChild(el("option", { value: name })));
    }
    renderListRows();
  }

  function renderListRows() {
    const box = document.getElementById("rel-list");
    if (!box) return;
    let rows = allVideos;
    if (searchText) rows = rows.filter(v => v.title.toLowerCase().includes(searchText));
    if (skillText) rows = rows.filter(v =>
      (skillsByVideo[v.id] || []).some(s => s.includes(skillText)));
    const cnt = document.getElementById("rel-count");
    if (cnt) cnt.textContent = `(${rows.length})`;
    clear(box);
    if (!rows.length) { box.appendChild(emptyState("No videos match the filters.", "∅")); return; }
    for (const v of rows) {
      const cb = el("input", { type: "checkbox", checked: checked.has(v.id) || null });
      cb.addEventListener("change", () => { cb.checked ? checked.add(v.id) : checked.delete(v.id); });
      box.appendChild(el("label", { class: "chk-item" }, cb,
        el("span", { style: "flex:1" }, v.title),
        el("span", { class: "muted small" }, v.channel || "")));
    }
  }

  function setAll(on) {
    document.querySelectorAll("#rel-list .chk-item").forEach(row => {
      const cb = row.querySelector("input");
      cb.checked = on;
      const title = row.querySelector("span").textContent;
      const v = allVideos.find(x => x.title === title);
      if (v) on ? checked.add(v.id) : checked.delete(v.id);
    });
  }

  async function renderGraph() {
    const canvas = document.getElementById("rel-canvas");
    const legend = document.getElementById("rel-legend");
    const hint = document.getElementById("rel-hint");
    if (!canvas) return;
    clear(canvas).appendChild(skeletonLines(6));
    clear(legend);
    if (hint) hint.style.display = "none";
    if (!checked.size) {
      clear(canvas).appendChild(emptyState("Select at least one video.", "◇"));
      return;
    }
    if (checked.size > 12) {
      clear(canvas).appendChild(emptyState("Too many videos selected — the graph is limited to 12.", "◇"));
      if (hint) {
        clear(hint).append(
          el("span", null, "Too many videos selected — uncheck some (maximum 12 for the graph)."),
          el("button", {
            class: "icon-btn ghost", title: "Dismiss",
            onclick: () => { hint.style.display = "none"; },
          }, "✕"),
        );
        hint.style.display = "";
      }
      return;
    }
    const ids = [...checked].join(",");
    let data;
    try { data = await api(`/api/relations?ids=${encodeURIComponent(ids)}`); }
    catch { clear(canvas).appendChild(emptyState("Could not load relations.", "✕")); return; }
    clear(canvas);
    if (data.mermaid) canvas.appendChild(await renderMermaid(data.mermaid));
    else canvas.appendChild(emptyState("No shared skills among these videos — nothing to graph.", "◇"));

    /* dismissible hint bar: small selection or no edges */
    if (hint) {
      const edgeCount = (data.shared || []).length;
      let msg = null;
      if (edgeCount === 0) msg = "No shared skills between these videos — try another combination.";
      else if (checked.size < 4) msg = "Small selection — select more videos to see richer connections.";
      if (msg) {
        clear(hint).append(
          el("span", null, msg),
          el("button", {
            class: "icon-btn ghost", title: "Dismiss",
            onclick: () => { hint.style.display = "none"; },
          }, "✕"),
        );
        hint.style.display = "";
      }
    }

    /* shared skills legend */
    if (data.shared && data.shared.length) {
      const videos = App.videos || [];
      legend.appendChild(el("div", { class: "section-title" }, "Shared skills"));
      for (const pair of data.shared) {
        const [a, b] = pair;
        const va = videos.find(v => v.id === a), vb = videos.find(v => v.id === b);
        const skillsA = (data.skills && data.skills[a]) || [];
        const shared = skillsA.filter(s => ((data.skills && data.skills[b]) || []).includes(s));
        legend.appendChild(el("div", { class: "legend-pair" },
          el("strong", null, va ? va.title : a),
          el("span", { class: "lp-arrow" }, "↔"),
          el("strong", null, vb ? vb.title : b),
          el("span", { class: "spacer" }),
          shared.length
            ? shared.map(s => el("span", { class: "chip" }, s))
            : el("span", { class: "muted small" }, "shared edge"),
        ));
      }
    }
    legend.appendChild(el("p", { class: "muted small", style: "margin:10px 0 0" },
      "Layout is automatic — drag-free, clarity first."));
  }

  App.tabs.relations = { render, built: false };
})();
