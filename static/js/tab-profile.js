/* tab-profile.js — developer profile markdown, editor, skills from this app */
"use strict";

(function () {
  let currentMarkdown = null;
  let currentPath = null;

  const GROUP_HEADINGS = [
    "### Strong / Professional Experience",
    "### Basic / Touched",
    "### Weak / Gaps (be patient here, explain more)",
  ];

  function render(screen) {
    currentMarkdown = null;
    currentPath = null;
    const wrap = el("div", { class: "screen-scroll scroll", style: "display:flex;flex-direction:column;gap:14px;max-width:900px;width:100%;margin:0 auto" });
    screen.appendChild(wrap);
    wrap.appendChild(el("div", { id: "profile-body" }, skeletonLines(10)));
    wrap.appendChild(el("div", { id: "profile-app-skills" }, skeletonLines(3)));
    load(wrap);
  }

  async function load(wrap) {
    const body = wrap.querySelector("#profile-body");
    const appBox = wrap.querySelector("#profile-app-skills");
    const [profile, appSkills] = await Promise.all([
      api("/api/profile").catch(() => null),
      api("/api/profile/skills").then(d => d.skills || []).catch(() => []),
    ]);
    if (!body.isConnected) return;
    clear(body);
    if (profile && profile.markdown) {
      currentMarkdown = profile.markdown;
      currentPath = profile.path;
      body.appendChild(buildProfileMarkdown(profile.markdown));
      body.appendChild(el("div", { class: "row", style: "margin-top:14px" },
        el("button", {
          class: "primary",
          onclick: () => openEditor(body, profile.markdown),
        }, "Edit profile"),
        el("span", { class: "muted small" }, "Source: ", el("code", null, profile.path))));
    } else {
      body.appendChild(el("div", { class: "pane" }, el("div", { class: "pane-body" },
        emptyState("No profile found — create the markdown file and refresh.", "◌"))));
    }
    clear(appBox);
    appBox.appendChild(el("div", { class: "pane" }, el("div", { class: "pane-header" }, "Skills added via this app"),
      el("div", { class: "pane-body" },
        appSkills.length
          ? el("div", null, appSkills.map(s => el("span", { class: "profile-chip" },
              el("strong", null, s.skill),
              el("span", { class: "level-badge" }, s.level || "basic"),
              el("a", {
                href: "#", class: "muted small",
                onclick: (e) => { e.preventDefault(); switchTab("learned"); },
                title: "See in Learned tab",
              }, s.source_video || ""))))
          : emptyState("No skills added through the app yet.", "◌"))));
  }

  /* render markdown; the "Technical Skills" section becomes skill-group cards */
  function buildProfileMarkdown(md) {
    const lines = String(md).split(/\r?\n/);
    /* extract first h1 as the header */
    let headerTitle = null;
    const rest = [];
    let tookH1 = false;
    for (const line of lines) {
      if (!tookH1 && /^#\s+/.test(line.trim())) { headerTitle = line.trim().replace(/^#\s+/, ""); tookH1 = true; }
      else rest.push(line);
    }
    const root = el("div", null);
    if (headerTitle) {
      root.appendChild(el("div", { class: "pane", style: "margin-bottom:14px" }, el("div", { class: "pane-body" },
        el("h1", { class: "profile-header" }, headerTitle),
        el("div", { class: "muted small" }, "AI-Assisted Developer Profile"))));
    }

    /* split sections by h2; find the Technical Skills one */
    const sections = [];
    let cur = null;
    for (const line of rest) {
      if (/^##\s+/.test(line.trim())) { cur = { title: line.trim().replace(/^##\s+/, ""), lines: [] }; sections.push(cur); }
      else if (cur) cur.lines.push(line);
      else { cur = { title: null, lines: [line] }; sections.push(cur); }
    }
    for (const sec of sections) {
      if (sec.title && /technical skills/i.test(sec.title)) {
        root.appendChild(el("div", { class: "section-title" }, sec.title));
        root.appendChild(renderSkillGroups(sec.lines));
        root.appendChild(el("div", { style: "height:14px" }));
      } else {
        const frag = renderMarkdown([sec.title ? `## ${sec.title}` : null, ...sec.lines].filter(x => x != null).join("\n"));
        root.appendChild(el("div", { class: "pane", style: "margin-bottom:14px" }, el("div", { class: "pane-body" }, frag)));
      }
    }
    return root;
  }

  /* the section's real format: "### Group name" headings with "- skill" bullets
     (each bullet may hold several comma-separated skills) -> group cards */
  function renderSkillGroups(lines) {
    const groups = parseGroups(lines);
    if (!groups.length) return el("div", { class: "pane" }, el("div", { class: "pane-body" },
      renderMarkdown(lines.join("\n"))));
    return el("div", { class: "skill-group-grid" }, groups.map(g => el("div", { class: "skill-group-card" },
      el("h4", null, g.heading.replace(/^###\s+/, "")),
      el("div", null, g.bullets.flatMap(splitBullet).map(s => el("span", { class: "chip" }, s))))));
  }

  /* ---------- editing ---------- */

  /* parse "### heading" + "- bullet" lines into groups (heading kept verbatim) */
  function parseGroups(lines) {
    const groups = [];
    let cur = null;
    for (const raw of lines) {
      const t = raw.trim();
      if (/^###\s+/.test(t)) { cur = { heading: t, bullets: [] }; groups.push(cur); continue; }
      if (cur && t.startsWith("- ")) cur.bullets.push(t.slice(2).trim());
    }
    return groups;
  }

  /* split a bullet into comma-separated items; bold flag = segment starts with ** */
  function splitBulletItems(bullet) {
    return bullet.split(",")
      .map(seg => {
        const trimmed = seg.trim();
        const bold = /^\*\*[^*]+\*\*/.test(trimmed);
        return { name: trimmed.replace(/\*\*/g, "").trim(), bold };
      })
      .filter(i => i.name);
  }
  function splitBullet(bullet) {
    return splitBulletItems(bullet).map(i => i.name);
  }

  /* rebuild a bullet string from items, preserving each item's bold flag */
  function joinBulletItems(items) {
    const clean = items.filter(i => i.name && i.name.trim());
    if (!clean.length) return null;
    return clean.map(i => i.bold ? `**${i.name.trim()}**` : i.name.trim()).join(", ");
  }
  function joinBullet(names, strong) {
    return joinBulletItems(names.map(n => ({ name: n, bold: strong })));
  }

  /* replace the "## ... Technical Skills" section inside full markdown lines */
  function replaceTechSection(mdLines, groups) {
    const start = mdLines.findIndex(l => /^##\s+.*technical skills/i.test(l.trim()));
    let end = mdLines.length;
    if (start >= 0) {
      end = mdLines.findIndex((l, i) => i > start && /^##\s+/.test(l.trim()));
      if (end < 0) end = mdLines.length;
    }
    const section = start >= 0 ? mdLines[start] : "## 2. Technical Skills";
    const out = [
      section,
      "",
      ...groups.flatMap(g => [
        g.heading,
        ...g.bullets.map(b => `- ${b}`),
        "",
      ]),
    ];
    return [...mdLines.slice(0, start >= 0 ? start : mdLines.length), ...out, ...mdLines.slice(end)];
  }

  function openEditor(body, markdown) {
    /* editor state: full markdown lines; structured edits rewrite the section */
    let mdLines = String(markdown).split(/\r?\n/);
    let rawMode = false;

    clear(body);
    const editorBox = el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Edit profile",
        el("div", { class: "row" },
          el("button", {
            class: "icon-btn ghost", id: "prof-mode-btn",
            onclick: (e) => {
              if (!rawMode) {
                /* structured -> raw: serialize current state */
                mdLines = syncFromStructured(mdLines);
                rawMode = true;
                e.currentTarget.textContent = "Structured editor";
                renderRaw();
              } else {
                /* raw -> structured: parse textarea back */
                mdLines = ta.value.split("\n");
                rawMode = false;
                e.currentTarget.textContent = "Edit raw markdown";
                renderStructured();
              }
            },
          }, "Edit raw markdown"),
        )),
      el("div", { class: "pane-body", id: "prof-editor-body" }),
      el("div", { class: "pane-body row", style: "border-top:1px solid var(--border)" },
        el("button", {
          class: "primary",
          onclick: async (e) => {
            const finalMd = rawMode ? ta.value : syncFromStructured(mdLines).join("\n");
            e.currentTarget.disabled = true;
            try {
              await api("/api/profile", { method: "PUT", body: { markdown: finalMd } });
              toast("Profile saved", "success");
              currentMarkdown = finalMd;
              load(body.closest(".screen-scroll") || document.querySelector(".screen"));
            } catch { e.currentTarget.disabled = false; }
          },
        }, "Save"),
        el("button", {
          onclick: () => { load(body.closest(".screen-scroll") || document.querySelector(".screen")); },
        }, "Cancel"),
        el("span", { class: "muted small" }, "Changes are saved to the markdown file via the server."),
      ),
    );
    body.appendChild(editorBox);

    const ta = el("textarea", {
      class: "raw-md",
      spellcheck: "false",
    });

    function renderStructured() {
      const box = editorBox.querySelector("#prof-editor-body");
      clear(box);
      const groups = normalizeGroups(parseGroups(extractTechLines(mdLines)));
      box.appendChild(el("div", { class: "muted small", style: "margin-bottom:12px" },
        "Edit the Technical Skills section — every other part of the profile stays untouched."));
      const grid = el("div", { class: "skill-group-grid" });
      groups.forEach((g, gi) => {
        const chipsBox = el("div", { class: "row wrap", style: "margin-bottom:10px" },
          g.bullets.flatMap(splitBullet).map((skill, si) => el("span", { class: "chip skill-edit-chip" },
            skill,
            el("button", {
              class: "chip-x", title: `Remove ${skill}`,
              onclick: () => {
                groups[gi].bullets = removeSkillFromBullets(groups[gi].bullets, skill);
                mdLines = replaceTechSection(mdLines, groups);
                renderStructured();
              },
            }, "×"),
          )));
        const addInput = el("input", {
          placeholder: "Add a skill…",
          onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); doAdd(); } },
        });
        const doAdd = () => {
          const val = addInput.value.trim();
          if (!val) return;
          const strong = /^strong/i.test(g.heading);
          groups[gi].bullets.push(joinBullet([val], strong));
          mdLines = replaceTechSection(mdLines, groups);
          renderStructured();
        };
        grid.appendChild(el("div", { class: "skill-group-card" },
          el("h4", null, g.heading.replace(/^###\s+/, "")),
          chipsBox,
          el("div", { class: "row" },
            addInput,
            el("button", { onclick: doAdd }, "Add")),
        ));
      });
      box.appendChild(grid);
    }

    function renderRaw() {
      const box = editorBox.querySelector("#prof-editor-body");
      clear(box);
      ta.value = mdLines.join("\n");
      box.appendChild(ta);
    }

    renderStructured();
  }

  /* section lines currently in mdLines (for the structured parser) */
  function extractTechLines(lines) {
    const start = lines.findIndex(l => /^##\s+.*technical skills/i.test(l.trim()));
    if (start < 0) return [];
    let end = lines.findIndex((l, i) => i > start && /^##\s+/.test(l.trim()));
    if (end < 0) end = lines.length;
    return lines.slice(start + 1, end);
  }

  /* ensure all three level groups exist so the editor always shows them */
  function normalizeGroups(groups) {
    const out = groups.slice();
    for (const h of GROUP_HEADINGS) {
      if (!out.some(g => g.heading === h)) out.push({ heading: h, bullets: [] });
    }
    return out;
  }

  /* remove one skill (by plain name) from a group's bullets */
  function removeSkillFromBullets(bullets, skill) {
    const next = [];
    for (const b of bullets) {
      const items = splitBulletItems(b);
      const filtered = items.filter(i => i.name.toLowerCase() !== skill.toLowerCase());
      const rebuilt = joinBulletItems(filtered);
      if (rebuilt) next.push(rebuilt);
    }
    return next;
  }

  /* structured -> raw: rebuild markdown from current groups (no-op if section absent) */
  function syncFromStructured(lines) {
    const groups = parseGroups(extractTechLines(lines));
    if (!groups.length) return lines;
    return replaceTechSection(lines, groups);
  }

  App.tabs.profile = { render, built: false };
})();
