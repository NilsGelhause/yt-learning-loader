/* tab-visualize.js — mermaid diagram of a video's learnings
   Left pane mirrors the Overview/Teach video rail: same cards (thumbnail,
   duration pill, date, skill chips) and the same filter stack. */
"use strict";

(function () {
  let pickedId = null;
  let allViz = [];                 // analyzed videos (has_learnings)
  let vizSearch = "";
  let vizChannel = "";             // "" = all channels
  let vizSkill = "";               // "" = all skills
  let vizLength = "";              // "" | "shorts" | "long"
  let vizSortKey = "date_desc";
  const vizSkillsByVideo = {};     // video id -> [skill names lowercased]
  let zoomMode = "auto";           // auto | fit | 50 | 75 | 125 | 150
  let currentBox = null;           // mermaid container of the displayed diagram

  const ZOOM_OPTIONS = [
    ["auto", "Auto"], ["fit", "Fit width"],
    ["50", "50%"], ["75", "75%"], ["125", "125%"], ["150", "150%"],
  ];

  function render(screen) {
    pickedId = null;
    vizSearch = ""; vizChannel = ""; vizSkill = ""; vizLength = "";
    vizSortKey = "date_desc";
    const grid = el("div", {
      class: "duo-grid screen-scroll",
      style: "grid-template-columns:minmax(480px,600px) minmax(0,1fr)",
    });

    const searchInput = el("input", {
      placeholder: "Search title…",
      oninput: (e) => { vizSearch = e.target.value.toLowerCase(); renderVizList(); },
    });
    const channelSelect = el("select", {
      id: "viz-channel",
      onchange: (e) => { vizChannel = e.target.value; renderVizList(); },
    });
    const skillInput = el("input", {
      list: "viz-skills-dl",
      placeholder: "Filter by skill (type to search)…",
      autocomplete: "off",
      oninput: (e) => { vizSkill = e.target.value.trim().toLowerCase(); renderVizList(); },
    });
    const skillDl = el("datalist", { id: "viz-skills-dl" });
    const lenSel = lengthFilterSelect(vizLength, m => { vizLength = m; renderVizList(); });
    const sortSel = videoSortSelect(vizSortKey, k => { vizSortKey = k; renderVizList(); });

    const pickerPane = el("div", { class: "pane picker-pane" },
      el("div", { class: "pane-header" },
        el("span", null, "Pick a video ", el("span", { class: "muted small", id: "viz-count" }))),
      el("div", {
        style: "padding:8px 10px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:6px",
      }, searchInput, channelSelect, skillInput, lenSel, sortSel, skillDl),
      el("div", { class: "scroll picker-list", id: "viz-picker" }, skeletonLines(6)),
    );
    const canvasPane = el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Diagram",
        el("select", {
          title: "Diagram size", onchange: (e) => {
            zoomMode = e.target.value;
            if (currentBox) applyDiagramZoom(currentBox);
          },
        }, ZOOM_OPTIONS.map(([k, label]) =>
          el("option", { value: k, selected: zoomMode === k || null }, label)))),
      el("div", { class: "diagram-canvas scroll", id: "viz-canvas" },
        emptyState("Pick a video to visualize its learnings.", "◇")),
    );

    grid.append(pickerPane, canvasPane);
    screen.appendChild(grid);
    loadPicker();
  }

  async function loadPicker() {
    const box = document.getElementById("viz-picker");
    if (!box) return;
    clear(box).appendChild(skeletonLines(6));
    allViz = (await getVideos().catch(() => [])).filter(v => v.has_learnings);
    if (!box.isConnected) return;
    fillVizChannels();
    await collectVizSkills();
    renderVizList();
  }

  function fillVizChannels() {
    const sel = document.getElementById("viz-channel");
    if (!sel || !sel.isConnected) return;
    clear(sel);
    sel.appendChild(el("option", { value: "" }, "All channels"));
    [...new Set(allViz.map(v => v.channel).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b))
      .forEach(c => sel.appendChild(el("option", { value: c, selected: vizChannel === c || null }, c)));
  }

  async function collectVizSkills() {
    const seen = new Map();
    await Promise.all(allViz.map(v =>
      getLearnings(v.id).then(l => {
        if (!l || !l.skills) { vizSkillsByVideo[v.id] = []; return; }
        vizSkillsByVideo[v.id] = l.skills.map(s => {
          const name = s.name || "";
          const key = name.toLowerCase();
          if (!seen.has(key)) seen.set(key, name);
          return key;
        });
      }).catch(() => { vizSkillsByVideo[v.id] = []; }),
    ));
    const dl = document.getElementById("viz-skills-dl");
    if (dl && dl.isConnected) {
      clear(dl);
      [...seen.values()].sort((a, b) => a.localeCompare(b))
        .forEach(name => dl.appendChild(el("option", { value: name })));
    }
  }

  function renderVizList() {
    const box = document.getElementById("viz-picker");
    const cnt = document.getElementById("viz-count");
    if (!box) return;
    let rows = allViz;
    if (vizSearch) rows = rows.filter(v => v.title.toLowerCase().includes(vizSearch));
    if (vizSkill) rows = rows.filter(v =>
      (vizSkillsByVideo[v.id] || []).some(s => s.includes(vizSkill)));
    if (vizChannel) rows = rows.filter(v => v.channel === vizChannel);
    rows = rows.filter(v => lengthMatch(v, vizLength));
    const [skey, sdir] = vizSortKey.split("_");
    rows = videoSort(rows, skey, sdir === "desc" ? -1 : 1);
    if (cnt) cnt.textContent = (vizSearch || vizSkill || vizChannel || vizLength)
      ? `${rows.length} of ${allViz.length}` : `${rows.length}`;
    clear(box);
    if (!rows.length) {
      box.appendChild(emptyState(allViz.length ? "No analyzed videos match." : "No analyzed videos yet.", "∅"));
      return;
    }
    rows.forEach(v => box.appendChild(vizCard(v)));
  }

  function vizCard(v) {
    const skillsBox = el("div", { class: "vc-skills" }, el("span", { class: "muted small" }, "…"));
    getLearnings(v.id).then(l => {
      if (!skillsBox.isConnected) return;
      clear(skillsBox);
      if (l && l.skills && l.skills.length) {
        l.skills.slice(0, 8).forEach(s => skillsBox.appendChild(el("span", { class: "chip sm" }, s.name)));
        if (l.skills.length > 8) skillsBox.appendChild(el("span", { class: "chip sm dim" }, `+${l.skills.length - 8}`));
      }
    }).catch(() => {});
    return el("div", {
      class: `video-card${pickedId === v.id ? " selected" : ""}`,
      dataset: { vid: v.id },
      onclick: () => pick(v.id),
    },
      el("div", { class: "thumb-wrap" },
        thumbImg(v),
        el("span", { class: "dur-pill" }, fmtDur(v.duration))),
      el("div", { class: "vc-body" },
        el("div", { class: "vc-title" }, v.title),
        el("div", { class: "vc-meta" }, fmtDate(v.published), v.learned ? " · learned" : ""),
        skillsBox,
      ));
  }

  async function pick(id) {
    pickedId = id;
    document.querySelectorAll("#viz-picker .video-card").forEach(c =>
      c.classList.toggle("selected", c.dataset.vid === id));

    const canvas = document.getElementById("viz-canvas");
    if (!canvas) return;
    clear(canvas).appendChild(skeletonLines(6));
    const l = await getLearnings(id).catch(() => null);
    if (!canvas.isConnected || pickedId !== id) return;
    clear(canvas);

    /* key points panel beside the diagram */
    if (l && l.key_points && l.key_points.length) {
      canvas.appendChild(el("div", { style: "width:260px;flex:none;margin-right:20px;align-self:flex-start" },
        el("div", { class: "section-title" }, "Key points"),
        el("ul", { class: "check-list" }, l.key_points.map(k => el("li", null, k))),
      ));
    }
    if (!l || !l.mermaid) {
      canvas.appendChild(emptyState("No diagram for this video yet — it will appear once learnings include a mermaid source.", "◇"));
      return;
    }
    currentBox = await renderMermaid(l.mermaid);
    canvas.appendChild(currentBox);
    applyDiagramZoom(currentBox);
  }

  /* Scale the rendered svg. "auto" fills the pane but never upscales beyond
     1.5x the diagram's intrinsic size (the readable middle ground); "fit" is
     full pane width; the percentages are fractions of the available width. */
  function applyDiagramZoom(box) {
    const svg = box.querySelector("svg");
    if (!svg) return;
    const canvas = document.getElementById("viz-canvas");
    const avail = Math.max(0, (canvas && canvas.clientWidth || 0) - 300);
    svg.style.maxWidth = "none"; /* mermaid inlines a pixel cap — clear it */
    if (zoomMode === "fit") {
      svg.style.width = "100%";
      return;
    }
    if (zoomMode === "auto") {
      let natural = 600;
      try {
        if (svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width) {
          natural = svg.viewBox.baseVal.width;
        }
      } catch { /* stubbed DOM — keep fallback */ }
      const px = avail ? Math.min(avail, natural * 1.5) : natural * 1.5;
      svg.style.width = px + "px";
      return;
    }
    const pct = (parseInt(zoomMode, 10) || 100) / 100;
    svg.style.width = (avail ? Math.round(avail * pct) : Math.round(800 * pct)) + "px";
  }

  App.tabs.visualize = { render, built: false };
})();
