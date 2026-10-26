/* tab-overview.js — 3-pane centerpiece: video rail / learnings & skills / details */
"use strict";

(function () {
  let selectedId = null;
  let railAll = [];            // all unlearned videos
  let skillsByVideo = {};      // video id -> [skill names lowercased]
  let skillNames = [];         // display names, unique + sorted
  let railSearch = "";
  let railSkill = "";          // "" = all skills
  let railChannel = "";        // "" = all channels
  let railSortKey = "date_desc";
  let railLength = "";         // "" | "shorts" | "long"

  function render(screen) {
    railSearch = "";
    railSkill = "";
    railChannel = "";
    railSortKey = "date_desc";
    railLength = "";
    const grid = el("div", { class: "overview-grid screen-scroll scroll" });

    /* rail filter row: search + channel select + typeable skill filter (datalist) */
    const searchInput = el("input", {
      placeholder: "Search title…",
      oninput: (e) => { railSearch = e.target.value.toLowerCase(); renderRailList(); },
    });
    const channelSelect = el("select", {
      id: "rail-channel",
      onchange: (e) => { railChannel = e.target.value; renderRailList(); },
    });
    const skillInput = el("input", {
      id: "rail-skill",
      list: "rail-skills-dl",
      placeholder: "Filter by skill (type to search)…",
      autocomplete: "off",
      oninput: (e) => { railSkill = e.target.value.trim().toLowerCase(); renderRailList(); },
    });
    const skillDl = el("datalist", { id: "rail-skills-dl" });
    const sortSel = videoSortSelect(railSortKey, k => { railSortKey = k; renderRailList(); });
    const lenSel = lengthFilterSelect(railLength, m => { railLength = m; renderRailList(); });
    const filterRow = el("div", {
      style: "padding:8px 10px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:6px",
    }, searchInput, channelSelect, skillInput, lenSel, sortSel, skillDl);

    const railPane = el("div", { class: "pane overview-pane", dataset: { pane: "rail" } },
      el("div", { class: "pane-header" }, "To Learn", el("span", { class: "muted small", id: "rail-count" })),
      filterRow,
      el("div", { class: "pane-body scroll picker-list", id: "rail-list", style: "padding:0" },
        skeletonLines(8)),
    );
    const centerPane = el("div", { class: "pane overview-pane", dataset: { pane: "learnings" } },
      el("div", { class: "pane-header" }, "Learnings & Skills"),
      el("div", { class: "pane-body scroll", id: "overview-learnings", style: "flex:1" }),
    );
    const rightPane = el("div", { class: "pane overview-pane", dataset: { pane: "center" } },
      el("div", { class: "pane-header" }, "Details"),
      el("div", { class: "pane-body scroll", id: "overview-detail", style: "flex:1" }),
    );

    /* mobile segmented control */
    const seg = el("div", { class: "segmented pane-switch" },
      ...[["rail", "Videos"], ["learnings", "Learnings"], ["center", "Details"]].map(([p, label], i) =>
        el("button", {
          class: i === 0 ? "on" : "",
          onclick: (e) => {
            seg.querySelectorAll("button").forEach(b => b.classList.remove("on"));
            e.currentTarget.classList.add("on");
            grid.querySelectorAll(".overview-pane").forEach(pn =>
              pn.classList.toggle("mobile-on", pn.dataset.pane === p));
          },
        }, label)),
    );

    grid.append(seg, railPane, centerPane, rightPane);
    screen.appendChild(grid);
    railPane.classList.add("mobile-on");

    renderRail();
    renderDetail();
    renderLearningsPanel();
  }

  /* ---------- LEFT: video rail ---------- */
  async function renderRail() {
    const list = document.getElementById("rail-list");
    if (!list) return;
    clear(list).appendChild(skeletonLines(8));
    railAll = (await getVideos()).filter(v => !v.learned);
    fillChannelSelect();
    collectRailSkills();
    renderRailList();
  }

  function fillChannelSelect() {
    const sel = document.getElementById("rail-channel");
    if (!sel || !sel.isConnected) return;
    clear(sel);
    sel.appendChild(el("option", { value: "" }, "All channels"));
    [...new Set(railAll.map(v => v.channel).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b))
      .forEach(c => sel.appendChild(el("option", { value: c, selected: railChannel === c || null }, c)));
  }

  /* union of all skills across rail videos' learnings (dedupe case-insensitively) */
  async function collectRailSkills() {
    const seen = new Map(); // lowercase -> display name
    await Promise.all(railAll.filter(v => v.has_learnings).map(v =>
      getLearnings(v.id).then(l => {
        if (!l || !l.skills) { skillsByVideo[v.id] = []; return; }
        skillsByVideo[v.id] = l.skills.map(s => {
          const name = s.name || "";
          const key = name.toLowerCase();
          if (!seen.has(key)) seen.set(key, name);
          return key;
        });
      }).catch(() => { skillsByVideo[v.id] = []; }),
    ));
    skillNames = [...seen.values()].sort((a, b) => a.localeCompare(b));
    const dl = document.getElementById("rail-skills-dl");
    if (dl && dl.isConnected) {
      clear(dl);
      skillNames.forEach(name => dl.appendChild(el("option", { value: name })));
    }
  }

  function renderRailList() {
    const list = document.getElementById("rail-list");
    const cnt = document.getElementById("rail-count");
    if (!list) return;
    let rows = railAll;
    if (railSearch) rows = rows.filter(v => v.title.toLowerCase().includes(railSearch));
    if (railSkill) rows = rows.filter(v =>
      (skillsByVideo[v.id] || []).some(s => s.includes(railSkill)));
    if (railChannel) rows = rows.filter(v => v.channel === railChannel);
    rows = rows.filter(v => lengthMatch(v, railLength));
    const [skey, sdir] = railSortKey.split("_");
    rows = videoSort(rows, skey, sdir === "desc" ? -1 : 1);
    if (cnt) cnt.textContent = (railSearch || railSkill || railChannel || railLength) ? `${rows.length} of ${railAll.length}` : `${railAll.length}`;
    clear(list);
    if (!rows.length) {
      list.appendChild(emptyState(railAll.length ? "No videos match the filters." : "All caught up — nothing left to learn.", "✓"));
      return;
    }
    for (const v of rows) list.appendChild(videoCard(v));
  }

  function videoCard(v) {
    const skillsBox = el("div", { class: "vc-skills" },
      v.has_learnings ? el("span", { class: "muted small" }, "…") :
        el("span", { class: "chip dim" }, "not analyzed yet"));
    if (v.has_learnings) {
      getLearnings(v.id).then(l => {
        if (!skillsBox.isConnected) return;
        clear(skillsBox);
        if (l && l.skills && l.skills.length) {
          l.skills.slice(0, 8).forEach(s => skillsBox.appendChild(el("span", { class: "chip sm" }, s.name)));
          if (l.skills.length > 8) skillsBox.appendChild(el("span", { class: "chip sm dim" }, `+${l.skills.length - 8}`));
        } else {
          skillsBox.appendChild(el("span", { class: "chip dim" }, "not analyzed yet"));
        }
      }).catch(() => {});
    }

    /* checkbox toggle, top-right corner */
    const cb = el("input", { type: "checkbox", class: "learned-cb" });
    const toggle = el("label", { class: "learned-toggle", title: "Mark as learned" }, cb,
      el("span", { class: "lt-label" }, "Learned"));
    cb.addEventListener("click", (e) => e.stopPropagation());
    cb.addEventListener("change", async () => {
      cb.disabled = true;
      try {
        await api(`/api/video/${v.id}/learned`, { method: "POST", body: { learned: true } });
        invalidateVideos();
        card.classList.add("leaving");
        setTimeout(() => {
          railAll = railAll.filter(x => x.id !== v.id);
          renderRailList();
        }, 300);
        toast("Marked as learned", "success");
      } catch { cb.disabled = false; cb.checked = false; }
    });

    /* thumbnail with duration pill */
    const thumbWrap = el("div", { class: "thumb-wrap" },
      thumbImg(v),
      el("span", { class: "dur-pill" }, fmtDur(v.duration)));

    const card = el("div", {
      class: `video-card${selectedId === v.id ? " selected" : ""}`,
      dataset: { vid: v.id },
      onclick: () => { selectedId = v.id; selectCard(); renderDetail(); renderLearningsPanel(); },
    },
      thumbWrap,
      el("div", { class: "vc-body" },
        el("div", { class: "vc-title" }, v.title),
        el("div", { class: "vc-meta" }, fmtDate(v.published)),
        skillsBox,
      ),
      toggle,
    );
    return card;
  }

  function selectCard() {
    document.querySelectorAll("#rail-list .video-card").forEach(c =>
      c.classList.toggle("selected", c.dataset.vid === selectedId));
  }

  /* ---------- CENTER: detail ---------- */
  async function renderDetail() {
    const box = document.getElementById("overview-detail");
    if (!box) return;
    clear(box);
    const videos = App.videos || [];
    const v = videos.find(x => x.id === selectedId);
    if (!v) { box.appendChild(emptyState("Select a video on the left.", "▸")); return; }
    box.appendChild(el("div", { class: "detail-hero" },
      thumbImg(v),
      el("div", null,
        el("h2", null, v.title),
        el("div", { class: "muted small", style: "margin-bottom:6px" },
          v.channel, " · ", fmtDate(v.published), " · ", fmtDur(v.duration)),
        el("a", { href: v.url, target: "_blank", rel: "noopener" }, "Watch on YouTube ↗"),
      ),
    ));
    box.appendChild(el("div", { class: "section-title" }, "Summary"));
    const sumBox = el("div", null, skeletonLines(3));
    box.appendChild(sumBox);
    getLearnings(v.id).then(l => {
      if (!sumBox.isConnected) return;
      clear(sumBox);
      if (l && l.summary) sumBox.appendChild(el("p", { style: "margin:0;color:var(--muted)" }, l.summary));
      else sumBox.appendChild(el("p", { class: "muted", style: "margin:0" },
        "No summary yet — this video hasn't been analyzed."));
      if (l && l.what_you_learn) {
        box.appendChild(el("div", { class: "section-title" }, "What you can learn"));
        box.appendChild(el("p", { style: "margin:0;color:var(--muted)" }, l.what_you_learn));
      }
    }).catch(() => { if (sumBox.isConnected) clear(sumBox).appendChild(el("p", { class: "muted" }, "Could not load learnings.")); });
  }

  /* ---------- RIGHT: learnings & skills ---------- */
  async function renderLearningsPanel() {
    const box = document.getElementById("overview-learnings");
    if (!box) return;
    clear(box);
    const v = (App.videos || []).find(x => x.id === selectedId);
    if (!v) { box.appendChild(emptyState("Select a video to see its learnings.", "◌")); return; }
    box.appendChild(skeletonLines(5));
    const l = await getLearnings(v.id).catch(() => null);
    if (!box.isConnected) return;
    clear(box);
    if (!l) {
      box.appendChild(emptyState(
        "This video hasn't been analyzed yet — ask Kimi Code to generate learnings.", "✨"));
      return;
    }
    if (l.skills && l.skills.length) {
      box.appendChild(el("div", { class: "section-title" }, "Skills"));
      const dl = el("dl", { class: "skill-list" });
      l.skills.forEach(s => dl.appendChild(el("dt", null, s.name), el("dd", null, s.description || "")));
      box.appendChild(dl);
    }
    if (l.learnings && l.learnings.length) {
      box.appendChild(el("div", { class: "section-title" }, `Learnings (${l.learnings.length})`));
      l.learnings.forEach(item => box.appendChild(el("div", { class: "learning-item" },
        el("span", { class: "li-title" }, item.title),
        item.time_hint ? el("span", { class: "time-hint" }, item.time_hint) : null,
        item.detail ? el("div", { class: "li-detail" }, item.detail) : null,
      )));
    }
    if (l.key_points && l.key_points.length) {
      box.appendChild(el("div", { class: "section-title" }, "Key points"));
      box.appendChild(el("ul", { class: "check-list" }, l.key_points.map(k => el("li", null, k))));
    }
    if (!((l.skills && l.skills.length) || (l.learnings && l.learnings.length) || (l.key_points && l.key_points.length))) {
      box.appendChild(emptyState("Learnings exist but are empty.", "◌"));
    }
  }

  App.tabs.overview = { render, built: false };
})();
