/* tab-skills.js — skills library: known vs learnable, detail pane with video cards */
"use strict";

(function () {
  let library = { known: [], learnable: [] };
  let personal = [];             // SKILL.md files created from videos/workspaces
  let knownSearch = "", learnSearch = "";
  let selected = null; // {kind: "known"|"learnable"|"personal", name}

  function render(screen) {
    knownSearch = ""; learnSearch = ""; selected = null;
    const grid = el("div", { class: "skills-grid screen-scroll" });

    const knownPane = el("div", { class: "pane" },
      el("div", { class: "pane-header" }, el("span", null, "Known skills ", el("span", { class: "muted small", id: "known-count" }))),
      el("div", { style: "padding:10px;border-bottom:1px solid var(--border)" },
        el("input", { placeholder: "Search known…", oninput: (e) => { knownSearch = e.target.value.toLowerCase(); fillKnown(); } })),
      el("div", { class: "pane-body scroll picker-list", id: "known-list", style: "padding:8px" }, skeletonLines(5)),
    );
    const learnPane = el("div", { class: "pane" },
      el("div", { class: "pane-header" }, el("span", null, "Skills to learn ", el("span", { class: "muted small", id: "learn-count" }))),
      el("div", { style: "padding:10px;border-bottom:1px solid var(--border)" },
        el("input", { placeholder: "Search learnable…", oninput: (e) => { learnSearch = e.target.value.toLowerCase(); fillLearnable(); } })),
      el("div", { class: "pane-body scroll picker-list", id: "learn-list", style: "padding:8px" }, skeletonLines(5)),
    );
    const detailPane = el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Detail"),
      el("div", { class: "pane-body scroll", id: "skills-detail", style: "flex:1" },
        emptyState("Pick a skill on the left.", "◇")),
    );

    grid.append(knownPane, learnPane, detailPane);
    screen.appendChild(grid);
    load();
  }

  async function load() {
    const [data, persBody] = await Promise.all([
      api("/api/skills/library").catch(() => null),
      api("/api/skills").catch(() => null),
    ]);
    /* /api/skills returns {"skills": [...]} — the envelope, not a bare array */
    personal = persBody && Array.isArray(persBody.skills) ? persBody.skills : [];
    if (!data) {
      const k = document.getElementById("known-list");
      if (k) clear(k).appendChild(emptyState("Could not load skills.", "✕"));
      return;
    }
    library = data;
    const kc = document.getElementById("known-count");
    const lc = document.getElementById("learn-count");
    if (kc) kc.textContent = `(${library.known.length + personal.length})`;
    if (lc) lc.textContent = `(${library.learnable.length})`;
    fillKnown();
    fillLearnable();
  }

  /* Known skills = skills from learned videos (library) merged with personal
     SKILL.md files, listed flat: each row has a small origin icon — hover for
     how it was learned. */
  function fillKnown() {
    const box = document.getElementById("known-list");
    if (!box) return;
    clear(box);
    const match = s => !knownSearch || s.name.toLowerCase().includes(knownSearch);
    const wsSkills = personal.filter(s => s.source_workspace && match(s));
    const vidPersonal = personal.filter(s => !s.source_workspace && s.video_id && match(s));
    const otherPersonal = personal.filter(s => !s.source_workspace && !s.video_id && match(s));
    const libKnown = library.known.filter(match);
    if (!wsSkills.length && !vidPersonal.length && !otherPersonal.length && !libKnown.length) {
      box.appendChild(emptyState(
        (personal.length + library.known.length) ? "No matches."
          : "Nothing here yet — mark videos as learned, or use \"Turn into skill\" in a Teach workspace.",
        "◌"));
      return;
    }
    const row = (icon, tip, onclick, name, badge) => el("div", {
      class: "contents-row", title: tip, onclick,
    },
      el("span", { class: "muted small", style: "width:18px;text-align:center;flex:none" }, icon),
      el("span", { class: "contents-name" }, name),
      badge ? el("span", { class: "tree-meta" }, badge) : null);
    wsSkills.forEach(s => box.appendChild(row("✦",
      "Learned through freestyle — distilled from a teaching workspace; click to open and install",
      () => renderPersonalDetail(s), s.name)));
    vidPersonal.forEach(s => box.appendChild(row("✦",
      "Learned through video — personal skill created from a video; click to open and install",
      () => renderPersonalDetail(s), s.name)));
    otherPersonal.forEach(s => box.appendChild(row("✦",
      "Personal skill — click to open and install",
      () => renderPersonalDetail(s), s.name)));
    libKnown.forEach(s => box.appendChild(row("▶",
      `Learned through video — from ${s.count} learned video${s.count === 1 ? "" : "s"}`,
      () => { selected = { kind: "known", name: s.name }; fillKnown(); fillLearnable(); renderDetail(s, "known"); },
      s.name, String(s.count))));
  }

  function renderPersonalDetail(s) {
    const box = document.getElementById("skills-detail");
    if (!box) return;
    clear(box);
    box.append(
      el("h2", { style: "margin:0 0 4px;font-size:17px" }, s.name),
      el("div", { class: "muted small", style: "margin-bottom:12px" },
        s.source_workspace
          ? `Personal skill — from teaching workspace "${s.source_workspace}"`
          : s.video_id
            ? "Personal skill — created from a learned video"
            : "Personal skill"),
    );
    if (s.description) box.appendChild(el("p", null, s.description));
    box.appendChild(el("div", { class: "section-title" }, "File"));
    box.appendChild(el("code", { style: "font-size:12px" }, s.path));
    /* Install button reflects the installed copy in ~/.kimi-code/skills:
       disabled when up to date; "update" when a re-distill changed it. */
    if (s.installed && !s.installed_stale) {
      box.appendChild(el("p", { class: "muted small", style: "margin-top:10px" },
        "Installed into ~/.kimi-code/skills — load it in any Kimi Code session:"));
      box.appendChild(el("button", {
        class: "ghost", style: "margin-top:4px", disabled: true,
      }, `Installed ✓ (/skill:${s.slug})`));
    } else {
      box.appendChild(el("p", { class: "muted small", style: "margin-top:10px" },
        s.installed_stale
          ? "The installed copy is outdated (the skill was re-distilled since) — update it:"
          : "Install a copy into ~/.kimi-code/skills to load it in any Kimi Code session:"));
      box.appendChild(el("button", {
        class: "primary", style: "margin-top:4px",
        onclick: async (e) => {
          e.currentTarget.disabled = true;
          try {
            const res = await api("/api/skill/install", { method: "POST", body: { slug: s.slug } });
            toast(`Installed — invoke with ${res.invoke} in any Kimi Code session`, "success");
            s.installed = true; s.installed_stale = false;
            e.currentTarget.textContent = `Installed ✓ (${res.invoke})`;
            e.currentTarget.className = "ghost";
          } catch { e.currentTarget.disabled = false; }
        },
      }, s.installed_stale ? `Update installation (/skill:${s.slug})` : `Install to Kimi Code (/skill:${s.slug})`));
    }
    if (s.video_id) box.appendChild(el("button", {
      class: "icon-btn", style: "margin-top:10px",
      onclick: () => { App.teachPreset = { video_id: s.video_id }; location.hash = "#teach"; },
    }, "Open in Teach"));
  }

  function skillButton(s, kind) {
    const active = selected && selected.kind === kind && selected.name === s.name;
    return el("button", {
      class: `chip skill-lib-chip${active ? " on" : ""}`,
      onclick: () => { selected = { kind, name: s.name }; fillKnown(); fillLearnable(); renderDetail(s, kind); },
    },
      s.name,
      el("span", { class: "count-badge" }, String(s.count)));
  }

  async function fillLearnable() {
    const box = document.getElementById("learn-list");
    if (!box) return;
    clear(box);
    const rows = library.learnable.filter(s => !learnSearch || s.name.toLowerCase().includes(learnSearch));
    if (!rows.length) { box.appendChild(emptyState("No matches.", "◌")); return; }
    const wrap = el("div", { class: "row wrap", style: "gap:6px" });
    rows.forEach(s => wrap.appendChild(skillButton(s, "learnable")));
    box.appendChild(wrap);
  }

  function renderDetail(s, kind) {
    const box = document.getElementById("skills-detail");
    if (!box) return;
    clear(box);
    box.append(
      el("h2", { style: "margin:0 0 4px;font-size:17px" }, s.name),
      el("div", { class: "muted small", style: "margin-bottom:12px" },
        kind === "known"
          ? `Known — learned across ${s.count} video${s.count === 1 ? "" : "s"}`
          : `Learnable — appears in ${s.count} video${s.count === 1 ? "" : "s"} you haven't learned`),
      el("div", { class: "section-title" }, kind === "known" ? "You learned this in:" : "You can learn this from:"),
    );
    if (!s.videos || !s.videos.length) { box.appendChild(emptyState("No videos listed.", "◌")); return; }
    s.videos.forEach(v => box.appendChild(el("div", { class: "video-card", style: "cursor:default" },
      thumbImg(v),
      el("div", { class: "vc-body" },
        el("div", { class: "vc-title" }, v.title),
        v.published ? el("div", { class: "vc-meta" }, fmtDate(v.published)) : null,
        el("div", { class: "row", style: "margin-top:6px" },
          el("a", { href: v.url, target: "_blank", rel: "noopener", class: "icon-btn" }, "YouTube ↗"),
          kind === "known"
            ? el("button", {
                class: "icon-btn",
                onclick: () => {
                  App.teachPreset = { video_id: v.id };
                  location.hash = "#teach";
                },
              }, "Open in Teach")
            : el("button", {
                class: "icon-btn primary",
                onclick: () => {
                  App.teachPreset = { video_id: v.id, focus_skill: s.name };
                  location.hash = "#teach";
                },
              }, "Teach me"))))));
  }

  App.tabs.skills = { render, built: false };
})();
