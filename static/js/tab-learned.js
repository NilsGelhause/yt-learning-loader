/* tab-learned.js — grid of completed videos with detail expansion */
"use strict";

(function () {
  let selectedId = null;
  let skillsIndex = [];
  let learnedSortKey = "date_desc";
  let learnedLength = "";

  function render(screen) {
    learnedSortKey = "date_desc";
    learnedLength = "";
    const wrap = el("div", { class: "screen-scroll scroll" });
    const toolbar = el("div", { class: "row", style: "margin-bottom:10px" },
      videoSortSelect(learnedSortKey, k => { learnedSortKey = k; load(); }),
      lengthFilterSelect(learnedLength, m => { learnedLength = m; load(); }));
    const gridBox = el("div", { id: "learned-grid" }, skeletonLines(6));
    const detailBox = el("div", { id: "learned-detail", style: "margin-top:14px" });
    wrap.append(toolbar, gridBox, detailBox);
    screen.appendChild(wrap);
    load();
  }

  async function load() {
    const gridBox = document.getElementById("learned-grid");
    if (!gridBox) return;
    const [videos, idx] = await Promise.all([getVideos(), getSkillsIndex()]);
    skillsIndex = idx;
    if (!gridBox.isConnected) return;
    clear(gridBox);
    const learned = videos.filter(v => v.learned && lengthMatch(v, learnedLength));
    if (!learned.length) { gridBox.appendChild(emptyState("Nothing learned yet — finish a video in the Teach tab.", "◌")); return; }
    const [skey, sdir] = learnedSortKey.split("_");
    const sorted = videoSort(learned, skey, sdir === "desc" ? -1 : 1);
    gridBox.appendChild(el("div", { class: "learned-grid" }, sorted.map(v => {
      const nSkills = v.has_learnings ? null : 0;
      const card = el("div", { class: "learned-card", dataset: { vid: v.id }, onclick: () => { selectedId = v.id; renderDetail(v); markSelected(); } },
        thumbImg(v),
        el("div", { class: "lc-body" },
          el("p", { class: "lc-title" }, v.title),
          el("div", { class: "muted small" }, fmtDate(v.published)),
          el("div", { style: "margin-top:6px", id: `lc-skills-${v.id}` },
            nSkills === 0 ? el("span", { class: "chip dim" }, "not analyzed") : el("span", { class: "muted small" }, "…")),
        ));
      if (v.has_learnings) {
        getLearnings(v.id).then(l => {
          const box = document.getElementById(`lc-skills-${v.id}`);
          if (!box) return;
          clear(box);
          const n = l && l.skills ? l.skills.length : 0;
          box.appendChild(n ? el("span", { class: "chip" }, `${n} skill${n === 1 ? "" : "s"}`) : el("span", { class: "chip dim" }, "no skills"));
        }).catch(() => {});
      }
      return card;
    })));
  }

  function markSelected() {
    document.querySelectorAll("#learned-grid .learned-card").forEach(c => c.style.borderColor = "");
  }

  async function renderDetail(v) {
    const box = document.getElementById("learned-detail");
    if (!box) return;
    clear(box).appendChild(el("div", { class: "pane learned-detail" }, el("div", { class: "pane-body" }, skeletonLines(6))));
    const [l, progress] = await Promise.all([
      getLearnings(v.id).catch(() => null),
      api(`/api/progress/${v.id}`).then(d => d.completed || []).catch(() => []),
    ]);
    if (!box.isConnected) return;
    const skill = skillsIndex.find(s => s.video_id === v.id);
    const total = l && l.learnings ? l.learnings.length : 0;
    const done = total ? progress.filter(t => l.learnings.some(item => item.title === t)).length : 0;
    clear(box).appendChild(el("div", { class: "pane learned-detail" }, el("div", { class: "pane-body" },
      el("div", { class: "detail-hero" },
        thumbImg(v),
        el("div", null,
          el("h2", null, v.title),
          el("div", { class: "muted small", style: "margin-bottom:6px" }, v.channel, " · ", fmtDate(v.published)),
          el("a", { href: v.url, target: "_blank", rel: "noopener" }, "Watch on YouTube ↗"),
          el("div", { class: "row", style: "margin-top:10px" },
            el("span", { class: "chip success" }, "Learned"),
            skill ? el("span", { class: "chip" }, `skill: ${skill.name}`) : null,
            total ? el("span", { class: "chip dim" }, `checklist: ${done}/${total}`) : null,
          ),
        )),
      l && l.summary ? el("div", null,
        el("div", { class: "section-title" }, "Summary"),
        el("p", { class: "muted", style: "margin:0" }, l.summary)) : null,
      l && l.skills && l.skills.length ? el("div", null,
        el("div", { class: "section-title" }, "Skills"),
        el("div", { class: "row wrap" }, l.skills.map(s => el("span", { class: "chip" }, s.name)))) : null,
      el("div", { style: "margin-top:16px" },
        el("button", {
          class: "danger",
          onclick: async (e) => {
            if (!confirm("Unmark as learned? It will return to the Overview rail.")) return;
            e.currentTarget.disabled = true;
            try {
              await api(`/api/video/${v.id}/learned`, { method: "POST", body: { learned: false } });
              invalidateVideos();
              toast("Returned to Overview", "success");
              selectedId = null;
              /* clear detail pane and re-render the grid immediately */
              const detail = document.getElementById("learned-detail");
              if (detail) clear(detail);
              const grid = document.getElementById("learned-grid");
              const card = grid && grid.querySelector(`.learned-card[data-vid="${v.id}"]`);
              if (card) {
                card.style.transition = "all 300ms ease";
                card.style.opacity = "0";
                card.style.transform = "scale(0.92)";
                setTimeout(load, 300);
              } else load();
            } catch { e.currentTarget.disabled = false; }
          },
        }, "Unmark learned")),
    )));
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  App.tabs.learned = { render, built: false };
})();
