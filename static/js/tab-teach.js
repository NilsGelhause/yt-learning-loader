/* tab-teach.js — teach workspaces: video mode, freestyle mode, agent console */
"use strict";

(function () {
  let mode = "video";              // "video" | "freestyle"
  let workspaces = [];             // from /api/teach/workspaces
  let currentWs = null;            // workspace id
  let wsData = null;               // {title, lessons, completed:Set, video_id, is_video}
  let videos = [];
  let pickedId = null;
  let pickedLearnings = null;
  let pickerSortKey = "date_desc"; // shared videoSort key (see core.js)
  let pickerLength = "";           // "" | "shorts" | "long"
  let pickerSearch = "";
  let pickerChannel = "";          // "" = all channels
  let pickerSkill = "";            // "" = all skills
  const pickerSkillsByVideo = {};  // video id -> [skill names lowercased]
  let agentAvailable = null;
  let job = null;                  // current agent job poll state
  let jobTimer = null;
  let pollCount = 0;
  let deleting = false;            // DELETE in flight
  /* per-workspace in-memory state */
  const sessionStarted = {};       // wsId -> bool (has had its first message)
  const consoleHist = {};          // wsId -> [{t, x}]

  /* ---------- entry ---------- */
  function render(screen) {
    const preset = App.teachPreset || null;
    App.teachPreset = null;

    const wrap = el("div", { class: "screen-scroll scroll", style: "display:flex;flex-direction:column;gap:14px" });

    /* top bar: mode switch + workspace switcher */
    const seg = el("div", { class: "segmented", style: "display:inline-flex" });
    const segVideo = el("button", { onclick: () => setMode("video") }, "From Video");
    const segFree = el("button", { onclick: () => setMode("freestyle") }, "Freestyle");
    seg.append(segVideo, segFree);
    const syncSeg = () => {
      segVideo.classList.toggle("on", mode === "video");
      segFree.classList.toggle("on", mode === "freestyle");
    };
    syncSeg();
    const wsSelect = el("select", {
      id: "ws-select", style: "max-width:320px",
      onchange: (e) => { if (e.target.value) loadWorkspace(e.target.value); },
    });
    /* delete acts on the currently loaded workspace; wrapper span carries the
       tooltip because disabled buttons don't fire mouse events */
    const deleteBtn = el("button", {
      id: "ws-delete", class: "icon-btn ws-delete-btn", title: "",
      onclick: deleteCurrentWorkspace,
    }, "🗑");
    const deleteWrap = el("span", { id: "ws-delete-wrap" }, deleteBtn);
    wrap.appendChild(el("div", { class: "row" }, seg, el("span", { class: "spacer" }), wsSelect, deleteWrap));
    wrap.appendChild(el("div", { id: "teach-body" }));

    screen.appendChild(wrap);
    refreshWorkspaces().then(() => {
      if (preset && preset.video_id) {
        mode = "video";
        syncSeg();
        pickedId = preset.video_id;
        if (preset.focus_skill) {
          startFromVideo(preset.video_id, preset.focus_skill);
          return;
        }
        showPicker();
        selectVideo(preset.video_id);
      } else {
        showPicker();
      }
    });
    checkAgent();
  }

  function setMode(m) {
    mode = m;
    const seg = document.querySelector("#screen .segmented");
    if (seg) seg.querySelectorAll("button").forEach(b => b.classList.remove("on"));
    if (seg) (m === "video" ? seg.querySelector("button") : seg.querySelectorAll("button")[1]).classList.add("on");
    showPicker();
  }

  async function checkAgent() {
    try { agentAvailable = (await api("/api/agent/status")).available; }
    catch { agentAvailable = false; }
    if (!agentAvailable && currentWs) renderWorkspaceView();
  }

  /* ---------- workspaces ---------- */
  async function refreshWorkspaces() {
    const data = await api("/api/teach/workspaces").catch(() => ({ workspaces: [] }));
    workspaces = data.workspaces || [];
    const sel = document.getElementById("ws-select");
    if (sel && sel.isConnected) {
      clear(sel);
      sel.appendChild(el("option", { value: "" }, `Workspaces (${workspaces.length})`));
      workspaces.forEach(w => sel.appendChild(el("option", {
        value: w.id, selected: currentWs === w.id || null,
      }, `${w.title.slice(0, 48)}${w.title.length > 48 ? "…" : ""} (${Math.round(w.progress * 100)}%)`)));
    }
    syncDeleteBtn();
  }

  /* Delete + Send button states: disabled with tooltip while an agent job
     runs or a delete is in flight. */
  function syncDeleteBtn() {
    const btn = document.getElementById("ws-delete");
    const wrap = document.getElementById("ws-delete-wrap");
    const running = !!(job && job.status !== "done" && job.status !== "error" && job.status !== "cancelled");
    const sendBtn = document.getElementById("agent-send");
    if (sendBtn) sendBtn.disabled = running || null;
    if (!btn || !wrap) return;
    const blocked = deleting || running;
    btn.disabled = blocked || !currentWs || null;
    let tip;
    if (deleting) tip = "Deleting…";
    else if (running) tip = "wait for the agent to finish";
    else if (currentWs) tip = `Delete workspace '${(wsData && wsData.title) || currentWs}'`;
    else tip = "Load a workspace first";
    wrap.title = tip;
    btn.title = tip;
    wrap.classList.toggle("no-ws", !currentWs);
  }

  async function deleteCurrentWorkspace() {
    if (!currentWs || deleting) return;
    if (job) {
      const running = job.status !== "done" && job.status !== "error" && job.status !== "cancelled";
      if (running) { toast("wait for the agent to finish", "info"); return; }
    }
    const title = (wsData && wsData.title) || currentWs;
    if (!confirm(`Delete workspace '${title}'? All its lessons, progress and agent session history in this workspace will be removed.`)) return;
    deleting = true;
    syncDeleteBtn();
    try {
      await api(`/api/teach/${currentWs}`, { method: "DELETE" });
      toast("Workspace deleted", "success");
      delete consoleHist[currentWs];
      delete sessionStarted[currentWs];
      closeWorkspace(); /* back to the mode picker, refreshes the list */
    } catch {
      /* api() already toasted (404/409 incl. the server's "in use" message) */
    } finally {
      deleting = false;
      syncDeleteBtn();
    }
  }

  async function loadWorkspace(id) {
    stopJob();
    currentWs = id;
    await fetchWsData(id);
    refreshWorkspaces();
    renderWorkspaceView();
  }

  async function fetchWsData(id) {
    const [lessons, state] = await Promise.all([
      api(`/api/teach/${id}/lessons`).then(d => d.lessons || []).catch(() => []),
      api(`/api/teach/${id}/state`).then(d => d.completed || []).catch(() => []),
    ]);
    const meta = workspaces.find(w => w.id === id) || {};
    wsData = {
      id,
      title: meta.title || id,
      lessons,
      completed: new Set(state),
      video_id: meta.video_id || null,
      is_video: !!meta.is_video,
    };
  }

  function closeWorkspace() {
    stopJob();
    currentWs = null;
    wsData = null;
    refreshWorkspaces();
    showPicker();
  }

  /* ---------- picker views (no workspace open) ---------- */
  async function showPicker() {
    currentWs = null;
    wsData = null;
    stopJob();
    refreshWorkspaces();
    const body = document.getElementById("teach-body");
    if (!body) return;
    clear(body);
    if (mode === "video") renderVideoPicker(body);
    else renderFreestyle(body);
  }

  /* The From-Video picker mirrors the Overview rail: same filters (title
     search, channel select, skill filter), same video cards (thumbnail,
     duration pill, date, skill chips), plus a date-direction toggle.
     Clicking a card selects it for teaching (the "What to learn" pane). */
  function renderVideoPicker(body) {
    const searchInput = el("input", {
      placeholder: "Search title…",
      oninput: (e) => { pickerSearch = e.target.value.toLowerCase(); renderPickerList(); },
    });
    const channelSelect = el("select", {
      id: "teach-channel",
      onchange: (e) => { pickerChannel = e.target.value; renderPickerList(); },
    });
    const skillInput = el("input", {
      list: "teach-skills-dl",
      placeholder: "Filter by skill (type to search)…",
      autocomplete: "off",
      oninput: (e) => { pickerSkill = e.target.value.trim().toLowerCase(); renderPickerList(); },
    });
    const skillDl = el("datalist", { id: "teach-skills-dl" });
    const sortSel = videoSortSelect(pickerSortKey, k => { pickerSortKey = k; renderPickerList(); });
    const lenSel = lengthFilterSelect(pickerLength, m => { pickerLength = m; renderPickerList(); });
    const filterRow = el("div", {
      style: "padding:8px 10px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:6px",
    }, searchInput, channelSelect, skillInput, lenSel, sortSel, skillDl);
    const listBox = el("div", { class: "scroll picker-list", id: "teach-picker" }, skeletonLines(6));
    const detail = el("div", { class: "pane-body scroll", id: "teach-detail", style: "flex:1" },
      emptyState("Pick a video on the left.", "▸"));
    body.appendChild(el("div", {
      class: "duo-grid",
      style: "flex:1;min-height:0;grid-template-columns:minmax(480px,600px) minmax(0,1fr)",
    },
      el("div", { class: "pane picker-pane" },
        el("div", { class: "pane-header" },
          el("span", null, "Videos ", el("span", { class: "muted small", id: "teach-count" }))),
        filterRow,
        listBox),
      el("div", { class: "pane" },
        el("div", { class: "pane-header" }, "What to learn"),
        detail)));
    loadPickerVideos();
  }

  async function loadPickerVideos() {
    const box = document.getElementById("teach-picker");
    if (!box) return;
    clear(box).appendChild(skeletonLines(6));
    videos = await getVideos().catch(() => []);
    if (!box.isConnected) return;
    fillPickerChannels();
    await collectPickerSkills();
    renderPickerList();
  }

  function fillPickerChannels() {
    const sel = document.getElementById("teach-channel");
    if (!sel || !sel.isConnected) return;
    clear(sel);
    sel.appendChild(el("option", { value: "" }, "All channels"));
    [...new Set(videos.map(v => v.channel).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b))
      .forEach(c => sel.appendChild(el("option", { value: c, selected: pickerChannel === c || null }, c)));
  }

  async function collectPickerSkills() {
    const seen = new Map();
    await Promise.all(videos.filter(v => v.has_learnings).map(v =>
      getLearnings(v.id).then(l => {
        if (!l || !l.skills) { pickerSkillsByVideo[v.id] = []; return; }
        pickerSkillsByVideo[v.id] = l.skills.map(s => {
          const name = s.name || "";
          const key = name.toLowerCase();
          if (!seen.has(key)) seen.set(key, name);
          return key;
        });
      }).catch(() => { pickerSkillsByVideo[v.id] = []; }),
    ));
    const dl = document.getElementById("teach-skills-dl");
    if (dl && dl.isConnected) {
      clear(dl);
      [...seen.values()].sort((a, b) => a.localeCompare(b))
        .forEach(name => dl.appendChild(el("option", { value: name })));
    }
  }

  function renderPickerList() {
    const box = document.getElementById("teach-picker");
    const cnt = document.getElementById("teach-count");
    if (!box) return;
    let rows = videos;
    if (pickerSearch) rows = rows.filter(v => v.title.toLowerCase().includes(pickerSearch));
    if (pickerSkill) rows = rows.filter(v =>
      (pickerSkillsByVideo[v.id] || []).some(s => s.includes(pickerSkill)));
    if (pickerChannel) rows = rows.filter(v => v.channel === pickerChannel);
    rows = rows.filter(v => lengthMatch(v, pickerLength));
    const [skey, sdir] = pickerSortKey.split("_");
    rows = videoSort(rows, skey, sdir === "desc" ? -1 : 1);
    if (cnt) cnt.textContent = (pickerSearch || pickerSkill || pickerChannel || pickerLength)
      ? `${rows.length} of ${videos.length}` : `${rows.length}`;
    clear(box);
    if (!rows.length) {
      box.appendChild(emptyState(
        videos.length ? "No videos match the filters." : "No videos yet — add some in the Videos tab.",
        "∅"));
      return;
    }
    rows.forEach(v => box.appendChild(pickerVideoCard(v)));
  }

  function pickerVideoCard(v) {
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
    return el("div", {
      class: `video-card${pickedId === v.id ? " selected" : ""}`,
      dataset: { vid: v.id },
      onclick: () => selectVideo(v.id),
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


  async function selectVideo(id) {
    pickedId = id;
    document.querySelectorAll("#teach-picker .video-card").forEach(c =>
      c.classList.toggle("selected", c.dataset.vid === id));
    const detail = document.getElementById("teach-detail");
    if (!detail) return;
    clear(detail).appendChild(skeletonLines(4));
    const v = videos.find(x => x.id === id);
    pickedLearnings = await getLearnings(id).catch(() => null);
    if (!detail.isConnected) return;
    clear(detail);
    if (!v) return;
    detail.append(
      el("h2", { style: "margin-top:0;font-size:17px" }, v.title),
      el("div", { class: "muted small", style: "margin-bottom:12px" }, v.channel, " · ", fmtDate(v.published)),
    );
    if (!pickedLearnings || !pickedLearnings.skills || !pickedLearnings.skills.length) {
      detail.appendChild(el("p", { class: "muted" },
        "This video has no skills listed yet — generate learnings first, or teach the whole video."));
    } else {
      detail.appendChild(el("div", { class: "section-title" }, "Pick a skill"));
      detail.appendChild(el("div", { class: "row wrap", style: "margin-bottom:14px" },
        pickedLearnings.skills.map(s => el("button", {
          class: "chip skill-pick", title: s.description || "",
          onclick: () => startFromVideo(id, s.name),
        }, s.name))));
    }
    detail.appendChild(el("button", {
      class: "primary",
      onclick: () => startFromVideo(id, null),
    }, "Teach the whole video"));
  }

  function renderFreestyle(body) {
    const topicInput = el("input", {
      placeholder: "e.g. Docker fundamentals for backend developers",
      style: "max-width:520px",
      onkeydown: (e) => { if (e.key === "Enter") btn.click(); },
    });
    const btn = el("button", {
      class: "primary",
      onclick: async (e) => {
        const topic = topicInput.value.trim();
        if (!topic) { toast("Enter a topic first", "info"); return; }
        e.currentTarget.disabled = true;
        try {
          const res = await api("/api/teach/start", { method: "POST", body: { topic } });
          await refreshWorkspaces();
          await loadWorkspace(res.id);
          /* prefill the console input; user reviews and sends */
          const input = document.getElementById("agent-input");
          if (input) {
            input.value = freestyleMessage(topic);
            input.focus();
          }
          toast("Workspace created — review the message and hit Send", "success");
        } catch { e.currentTarget.disabled = false; }
      },
    }, "Start learning this");
    body.appendChild(el("div", { class: "pane", style: "max-width:640px;margin:0 auto;width:100%" },
      el("div", { class: "pane-header" }, "Freestyle learning"),
      el("div", { class: "pane-body" },
        el("p", { class: "muted", style: "margin-top:0" },
          "Type any topic. A teaching workspace is created and the agent builds lessons for it — no video needed."),
        el("div", { class: "row" }, topicInput, btn))));
  }

  /* ---------- video-mode workspace start ---------- */
  async function startFromVideo(videoId, focusSkill) {
    const v = videos.find(x => x.id === videoId) ||
      (await getVideos().catch(() => [])).find(x => x.id === videoId);
    if (!v) { toast("Video not found", "error"); return; }
    try {
      const body = { video_id: videoId };
      if (focusSkill) body.focus_skill = focusSkill;
      const res = await api("/api/teach/start", { method: "POST", body });
      await refreshWorkspaces();
      await loadWorkspace(res.id);
      /* auto-send the first message — this is the user's explicit intent */
      const msg = focusSkill ? skillMessage(v, focusSkill) : wholeVideoMessage(v);
      await sendAgentMessage(msg);
    } catch { /* api() toasted the error (400/404) */ }
  }

  /* Suggest a precise skill name from the workspace id (freestyle ids carry
     the topic: "free-i-want-to-learn-prompting") or the mission title for
     video workspaces (whose id is the opaque video id). */
  function suggestSkillName(meta) {
    if (!meta) return "Workspace Skill";
    let base = String(meta.id || "").replace(/^free-/, "").replace(/-[A-Za-z0-9_-]{11}$/, "");
    if (!base || /^[A-Za-z0-9_-]{11}$/.test(base)) {
      base = String(meta.title || "").replace(/^Mission:\s*/i, "");
    }
    const words = base.split(/[-_\s]+/).filter(Boolean)
      .map(w => w[0].toUpperCase() + w.slice(1));
    return words.join(" ") || "Workspace Skill";
  }

  /* ---------- first-message prompt builder ----------
     Ordered parts joined by blank lines — no concatenation bugs.
     The prompts only INVOKE the teach skill; the skill itself drives the
     workflow (including its interview step). No workflow restatement here. */
  function buildPrompt(...parts) {
    return parts.filter(Boolean).join("\n\n");
  }
  /* the kickoff carries the workflow contract (interview, lesson format) at its
     END — never trim below its full length; the Windows command line handles
     ~32k chars and these prompts stay well under 8k */
  function trimMsg(s) { return s.length > 8000 ? s.slice(0, 7997) + "…" : s; }

  const SKILL_LINE = "Use the teach skill — read ~/.kimi-code/skills/teach/SKILL.md directly (it is not in your auto-discovered skill listing) — and follow it exactly, starting from its first step.";
  const FREESTYLE_TURNS_LINE = "Work step by step in turns. MISSION.md in this directory contains the learner's profile and the mission, with a STATUS line — read it first. If STATUS is DRAFT (not yet confirmed), interview me first: ask me why I want to learn this, what success looks like, and what I already know, in plain text, then STOP and wait for my answers. Do not infer the mission before my answers. If STATUS is CONFIRMED, do NOT re-interview me: check learning-records/ and lessons/ and continue from where the workspace left off (lessons/00-curriculum.md is the plan — build the next unbuilt lesson from it). Either way, once you have what you need, do NOT ask another round of questions: briefly state your understanding of the mission and any assumptions, update MISSION.md if needed, and CONTINUE autonomously — populate RESOURCES.md if it is sparse, then build lessons. Mention that you will proceed on this understanding and that I can correct it later, but keep working. Only stop again if you genuinely cannot proceed without my input.";
  const VIDEO_TURNS_LINE = "Work step by step in turns, mirroring the freestyle flow. First read MISSION.md (the learner's profile + this video's learning summary), the transcript, and the extracted learnings — paths are in the message above; they are local files, never fetch from the network. Understand what the video is actually about, then INTERVIEW me before creating or extending any lesson — even if lessons already exist in lessons/, ask me first what I want out of them: in plain text, ask me why I want to learn this video's material, what I already know about the topic, and what success looks like for me — tailor the questions to the video's actual content (mention what it covers so I can react to it). Then STOP and wait for my answers; do not infer the answers from my profile, my answers decide how you teach. Once I have answered, do NOT ask another round of questions: briefly state your understanding of my intent plus any assumptions, note them in NOTES.md, and CONTINUE autonomously — build the shared assets first, then the lessons, never stopping between lessons. Mention that I can correct your understanding later, but keep working. Only stop again if you genuinely cannot proceed without my input.";
  const NO_OPEN_LINE = "The teach skill may tell you to open lesson files with a CLI command (it has a step like 'If possible, open the lesson file for the user') — IGNORE that step completely in this environment: never open files in a browser and never run start, open, explorer, xdg-open or any file-opening command, even if the skill explicitly suggests it. Never open lessons for the user — this webapp displays the lessons itself; just write the files into lessons/ and tell the user the lesson appeared in the Lessons panel. LESSON FILES MUST be self-contained HTML named 000N-<dash-case>.html inside lessons/ (0001-, 0002-, …, incrementing) — the webapp's Lessons panel ONLY lists .html files, so Markdown lessons are INVISIBLE to the learner; never write a lesson as .md. Create the workspace's shared stylesheet at assets/lesson.css as the first asset (clean, readable, print-friendly) and link it from every lesson as ../assets/lesson.css together with <script src=\"../assets/links.js\" defer></script>; use ../assets/quiz.js for interactive quizzes (copy both assets' structure from any existing workspace if unsure). NOTES.md, learning-records/*.md and reference/*.md stay Markdown — ONLY lessons/ must be HTML. Every lesson and reference HTML you write MUST link ../assets/links.js (next to ../assets/lesson.css): lessons render inside an iframe, and that script makes external links open in a new browser tab — without it, sites that send X-Frame-Options (arxiv.org, Reddit, most vendor docs) refuse to load inside the iframe and show a blocking error instead of the page.";

  const SUBJECT_LINE = "Teach THE ACTUAL SUBJECT — the video's material or the named topic — and build the learner's competence in that subject itself. Never replace the subject with, or scaffold it onto, alternative tools the learner already knows: familiar tools may appear only as clearly-labeled brief analogies, never as the lesson's content. If the learner mentions a tool or model they use (e.g. their editor or model provider), treat it as environment context — which model runs the exercises — not as a substitute for the subject.";

  const HANDHOLD_LINE = "HAND-HOLDING IS THE POINT OF THIS APP: the learner comes here to be walked through NEW technology, not to be sent elsewhere. Whenever a lesson needs concrete setup steps, install commands, config, or flags that the source material does not spell out, RESEARCH them yourself (web search, official docs, GitHub readmes) and write verified, copy-paste-ready, step-by-step instructions INTO the lesson — never answer with 'check the docs' or 'see the video description'. If something genuinely cannot be verified, state exactly what you verified, what you could not, and give precise search terms plus how to confirm the answer. Also: every check-yourself or quiz question must give the learner a way to know whether they were right — a revealable answer in the lesson (e.g. a <details> block) or an explicit pointer to ask you in the console. Never leave a question with no feedback path.";

  const AUTHENTIC_LINE = "Follow the teach skill's own pedagogy faithfully — its lesson shape (short, one tangible win), quiz discipline, citations, and primary-source recommendations ARE the teaching method. The extra instructions here (file naming, no browser opening, interview first) are environment adapters for this webapp, not replacements for the skill's teaching style; where they overlap, the skill's pedagogy wins.";

  function freestyleMessage(topic) {
    return buildPrompt(
      SKILL_LINE,
      `Teach me: ${topic}`,
      FREESTYLE_TURNS_LINE,
      SUBJECT_LINE,
      HANDHOLD_LINE,
      AUTHENTIC_LINE,
      NO_OPEN_LINE,
    );
  }

  function videoContext(v) {
    return `Context: the transcript is at ../../data/transcripts/${v.id}.json and the extracted learnings at ../../data/learnings/${v.id}.json — paths relative to THIS workspace directory (the app root is two levels up: ../..). Both files already exist locally: read them directly, do NOT try to download or fetch the transcript from the network; if a file is unexpectedly missing, say so and work with what exists. MISSION.md in this directory summarizes the material and contains the learner's profile — read it first.`;
  }
  function skillMessage(v, skill) {
    return trimMsg(buildPrompt(
      SKILL_LINE,
      `Teach me the skill "${skill}" from the video "${v.title}" (${v.url}).`,
      videoContext(v),
      VIDEO_TURNS_LINE,
      SUBJECT_LINE,
      HANDHOLD_LINE,
      AUTHENTIC_LINE,
      NO_OPEN_LINE,
    ));
  }
  function wholeVideoMessage(v) {
    return trimMsg(buildPrompt(
      SKILL_LINE,
      `Teach me the video "${v.title}" (${v.url}).`,
      videoContext(v),
      VIDEO_TURNS_LINE,
      SUBJECT_LINE,
      HANDHOLD_LINE,
      AUTHENTIC_LINE,
      NO_OPEN_LINE,
    ));
  }

  /* ---------- workspace view ---------- */
  function renderWorkspaceView() {
    const body = document.getElementById("teach-body");
    if (!body || !wsData) return;
    clear(body);
    const done = wsData.completed.size, total = wsData.lessons.length;
    const pct = total ? Math.round((done / total) * 100) : 0;

    /* header */
    const head = el("div", { class: "row wrap" },
      el("h2", { style: "margin:0;font-size:16px;flex:1;min-width:200px" }, wsData.title),
      el("button", {
        title: "Create a reusable SKILL.md from this workspace — an agent distills it into a loadable skill",
        onclick: async (e) => {
          /* Read live values: refreshWorkspaceQuiet() updates lessons and
             progress in place without re-rendering this header, so values
             captured at render time can be stale (showing e.g. "0 of 0"). */
          const dNow = wsData ? wsData.completed.size : 0;
          const tNow = wsData ? wsData.lessons.length : 0;
          if (!(tNow > 0 && dNow === tNow) &&
              !confirm(`Only ${dNow} of ${tNow} lessons completed. Create the skill anyway?`)) return;
          const suggested = suggestSkillName(wsData);
          const name = prompt(
            "Skill name — precise and short. It becomes the file name and the /skill:<name> command:",
            suggested);
          if (name === null) return;
          const finalName = name.trim() || suggested;
          e.currentTarget.disabled = true;
          try {
            const res = await api("/api/skill/from_workspace", {
              method: "POST", body: { workspace: currentWs, name: finalName, distill: true },
            });
            App.skillsIndex = null;
            if (res.job_id) {
              toast(`Skill "${finalName}" created — the agent is now distilling it. Watch the console.`, "success");
              pollAgent(res.job_id);
            } else {
              toast(`Skill created: ${res.path} — baseline version (agent busy or unavailable; click Turn into skill again to distill it)`, "info");
            }
          } catch { e.currentTarget.disabled = false; }
        },
      }, "Turn into skill"),
      el("button", {
        title: "Next message starts a fresh agent session for this workspace",
        onclick: () => { sessionStarted[currentWs] = false; toast("Next send starts a new agent session", "info"); },
      }, "New agent session"),
      el("button", { class: "ghost", title: "Close workspace", onclick: closeWorkspace }, "✕ Close"),
    );

    /* progress */
    const progressLabel = el("span", { class: "progress-label" }, total ? `${done} of ${total} lessons · ${pct}%` : "No lessons yet");
    const bar = el("div", { class: "progress-bar", style: `width:${pct}%` });

    /* console */
    const hist = consoleHist[currentWs] = consoleHist[currentWs] || [];
    const consoleBox = el("div", { class: "agent-console scroll", id: "agent-console" },
      hist.length ? null : el("div", { class: "cl-meta" }, "Console — send a message below to start the agent."));
    hist.forEach(l => consoleBox.appendChild(consoleLine(l)));
    let userAtBottom = true;
    consoleBox.addEventListener("scroll", () => {
      userAtBottom = consoleBox.scrollTop + consoleBox.clientHeight >= consoleBox.scrollHeight - 30;
    });
    const statusRow = el("div", { class: "row", id: "agent-status-row", style: "min-height:22px" });

    /* input — Enter must insert a newline: the agent invites long, numbered,
       multi-line answers, and Enter-to-send fires mid-answer (sending only
       line 1). Send with the button or Ctrl+Enter. */
    const input = el("textarea", {
      id: "agent-input", rows: "2", placeholder: "Message the agent… (Enter for a new line, Ctrl+Enter to send)",
      onkeydown: (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doSend(); }
      },
    });
    const sendBtn = el("button", { id: "agent-send", class: "primary", onclick: () => doSend() }, "Send");
    async function doSend() {
      const text = input.value.trim();
      if (!text || job) return;
      const sent = await sendAgentMessage(text);
      if (sent) input.value = ""; /* on failure (e.g. 409) keep the text for a resend */
    }

    const banner = agentAvailable === false
      ? el("div", { class: "instructions-box", style: "margin:0 0 10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap" },
          el("span", null, "kimi CLI not found — open Kimi Code and run /teach manually in this workspace: "),
          el("code", null, `teach/${currentWs}`),
          el("button", { class: "icon-btn", onclick: (e) => copyText(`teach/${currentWs}`, e.currentTarget) }, "Copy"))
      : null;
    const hintLine = el("div", { class: "muted small", style: "margin-top:4px" },
      "The agent may ask you questions — just type your answer and send; lessons it writes appear in the Lessons panel below and open right here.");
    const inputRow = agentAvailable === false ? null : el("div", null,
      el("div", { class: "row", style: "align-items:flex-start;margin-top:8px" }, input, sendBtn),
      hintLine);

    /* lessons */
    const lessonList = el("div", { id: "lesson-list" });
    renderLessonList(lessonList);

    body.appendChild(el("div", { class: "pane", style: "flex:1;min-height:0" },
      el("div", { class: "pane-body scroll", style: "flex:1;display:flex;flex-direction:column;gap:8px" },
        banner,
        head,
        el("div", { class: "row" }, progressLabel, el("div", { class: "spacer" })),
        el("div", { class: "progress-wrap" }, bar),
        el("div", { class: "section-title" }, "Agent console"),
        consoleBox,
        statusRow,
        inputRow,
        el("div", { class: "section-title" }, "Lessons"),
        lessonList,
      )));
    scrollConsole(true);
  }

  function consoleLine(l) {
    return el("div", { class: l.t === "err" ? "cl-err" : "cl-out" }, l.x);
  }

  function scrollConsole(force) {
    const box = document.getElementById("agent-console");
    if (box && (force || box.dataset.pin !== "0")) box.scrollTop = box.scrollHeight;
  }

  function renderLessonList(box) {
    if (!box) return;
    clear(box);
    if (!wsData.lessons.length) {
      box.appendChild(el("p", { class: "muted small" }, "No lessons yet — the agent writes them into lessons/ as it works."));
      return;
    }
    wsData.lessons.forEach(lesson => {
      const name = lesson.file;
      const cb = el("input", { type: "checkbox", checked: wsData.completed.has(name) || null });
      cb.addEventListener("change", async () => {
        cb.checked ? wsData.completed.add(name) : wsData.completed.delete(name);
        await api(`/api/teach/${currentWs}/state`, {
          method: "POST", body: { completed: [...wsData.completed] },
        }).catch(() => toast("Could not save lesson state", "error"));
        updateProgressUI();
        refreshWorkspaces(); /* keep the sidebar "(x%)" in sync with saves */
      });
      box.appendChild(el("div", { class: "lesson-row" },
        cb,
        el("span", {
          class: "lesson-link", style: "flex:1;border:none",
          onclick: () => openLessonModal(name),
        }, name),
        wsData.completed.has(name) ? el("span", { class: "chip success" }, "done") : null));
    });
  }

  function updateProgressUI() {
    const done = wsData.completed.size, total = wsData.lessons.length;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const bar = document.querySelector("#teach-body .progress-bar");
    const label = document.querySelector("#teach-body .progress-label");
    if (bar) bar.style.width = pct + "%";
    if (label) label.textContent = total ? `${done} of ${total} lessons · ${pct}%` : "No lessons yet";
  }

  function openLessonModal(name) {
    closeLessonModal();
    const overlay = el("div", { class: "modal-overlay", id: "lesson-modal" },
      el("div", { class: "modal-box" },
        el("div", { class: "modal-head" },
          el("strong", { class: "mono small" }, name),
          el("button", { onclick: closeLessonModal }, "Close ✕")),
        el("iframe", {
          // serve under /teach/<ws>/lessons/<file> so the lesson's relative
          // asset links (../assets/lesson.css, ../assets/quiz.js) resolve
          src: `/teach/${encodeURIComponent(currentWs)}/lessons/${encodeURIComponent(name)}`,
        })));
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeLessonModal(); });
    document.body.appendChild(overlay);
  }
  function closeLessonModal() {
    const m = document.getElementById("lesson-modal");
    if (m) m.remove();
  }

  /* ---------- agent job ---------- */
  async function sendAgentMessage(text) {
    if (!currentWs) return false;
    const isNew = !sessionStarted[currentWs];
    let res;
    try {
      res = await api("/api/agent/run", { method: "POST", body: { workspace: currentWs, message: text, new_session: isNew } });
    } catch (err) {
      /* 409: a job is already running for this workspace — api() toasted the
         server's message; the caller keeps the input text for a later resend */
      return false;
    }
    sessionStarted[currentWs] = true;
    appendConsole({ t: "out", x: `> ${text}` });
    appendConsole({ t: "err", x: "─ agent starting…" });
    pollAgent(res.job_id);
    return true;
  }

  function appendConsole(line) {
    const hist = consoleHist[currentWs];
    if (hist) hist.push(line);
    const box = document.getElementById("agent-console");
    if (box) {
      const meta = box.querySelector(".cl-meta");
      if (meta) meta.remove();
      box.appendChild(consoleLine(line));
      const pinned = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
      if (pinned) box.scrollTop = box.scrollHeight;
    }
  }

  function pollAgent(jobId) {
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
      if (j.status === "done" || j.status === "error" || j.status === "cancelled") {
        stopJob();
        appendConsole({ t: "err", x: `─ agent ${j.status}${j.exit_code != null ? ` (exit ${j.exit_code})` : ""}` });
        refreshWorkspaceQuiet();
        return;
      }
      pollCount++;
      /* refresh lessons ~every 3s while running so new files appear promptly */
      if (pollCount % 2 === 0) refreshWorkspaceQuiet();
    };
    pollCount = 0;
    jobTimer = setInterval(tick, 1500);
    tick();
    refreshWorkspaceQuiet(); /* baseline right when the job starts */
  }

  async function refreshWorkspaceQuiet() {
    if (!currentWs) return;
    await fetchWsData(currentWs);
    const list = document.getElementById("lesson-list");
    if (list && list.isConnected) renderLessonList(list);
    updateProgressUI();
    refreshWorkspaces(); /* sidebar % also advances while the agent builds */
  }

  function renderStatus(j) {
    const row = document.getElementById("agent-status-row");
    if (row) {
      clear(row);
      if (j && j.status !== "done" && j.status !== "error" && j.status !== "cancelled") {
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
    syncDeleteBtn();
  }

  function stopJob() {
    if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
    job = null;
    const row = document.getElementById("agent-status-row");
    if (row) clear(row);
    syncDeleteBtn();
  }

  App.tabs.teach = { render, built: false };
})();
