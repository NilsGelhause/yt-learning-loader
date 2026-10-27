/* tab-settings.js — appearance, scraping, tab manager, data, learnings, profile */
"use strict";

(function () {
  function render(screen) {
    const wrap = el("div", { class: "screen-scroll scroll" });
    const grid = el("div", { class: "settings-grid" });
    wrap.appendChild(grid);
    screen.appendChild(wrap);
    grid.append(
      appearanceCard(),
      scrapingCard(),
      lanCard(),
      tabsCard(),
      el("div", { id: "settings-data" }, skeletonLines(6)),
      learningsCard(),
      el("div", { id: "settings-profile" }, skeletonLines(3)),
    );
    loadDataCards();
    fillLanPanel();
  }

  async function postSettings(body, okMsg) {
    try {
      const res = await api("/api/settings", { method: "POST", body });
      App.settings = res;
      applyAppearance();
      renderTabbar();
      if (okMsg) toast(okMsg, "success");
      if (res.note) toast(res.note, "info");
      return res;
    } catch { return null; }
  }

  /* ---------- appearance ---------- */
  function appearanceCard() {
    const s = App.settings;
    const themeSeg = el("div", { class: "theme-toggle" },
      el("button", { class: s.theme !== "light" ? "on" : "", onclick: (e) => setTheme("dark", e) }, "Dark"),
      el("button", { class: s.theme === "light" ? "on" : "", onclick: (e) => setTheme("light", e) }, "Light"));
    function setTheme(theme, e) {
      themeSeg.querySelectorAll("button").forEach(b => b.classList.remove("on"));
      e.currentTarget.classList.add("on");
      App.settings.theme = theme;
      applyAppearance();
      postSettings({ theme });
    }
    const colorInput = el("input", {
      type: "color", value: s.accent || "#7c5cff", title: "Accent color",
      oninput: (e) => {
        App.settings.accent = e.target.value;
        applyAppearance();
      },
      onchange: (e) => postSettings({ accent: e.target.value }, "Accent saved"),
    });
    return el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Appearance"),
      el("div", { class: "pane-body" },
        el("div", { class: "field" }, el("label", null, "Theme"), themeSeg),
        el("div", { class: "field" }, el("label", null, "Accent color"),
          el("div", { class: "row" }, colorInput,
            el("span", { class: "muted small" }, "Recolors the whole UI live."))),
      ));
  }

  /* ---------- scraping ---------- */
  function scrapingCard() {
    const s = App.settings;
    const portIn = el("input", { type: "number", value: String(s.port ?? 8471) });
    const limitIn = el("input", { type: "number", min: "1", value: String(s.channel_default_limit ?? 30) });
    const delayIn = el("input", { type: "number", step: "0.1", min: "0", value: String(s.request_delay ?? 0.8) });
    return el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Scraping"),
      el("div", { class: "pane-body" },
        el("div", { class: "field" }, el("label", null, "Channel default limit"), limitIn),
        el("div", { class: "field" }, el("label", null, "Request delay (seconds)"), delayIn),
        el("div", { class: "field" }, el("label", null, "Port"), portIn,
          el("div", { class: "muted small", style: "margin-top:3px" }, "Changing the port requires a server restart.")),
        el("button", {
          class: "primary",
          onclick: async (e) => {
            e.currentTarget.disabled = true;
            try {
              await postSettings({
                port: parseInt(portIn.value, 10) || s.port,
                channel_default_limit: parseInt(limitIn.value, 10) || s.channel_default_limit,
                request_delay: parseFloat(delayIn.value) || s.request_delay,
              }, "Settings saved");
            } finally { e.currentTarget.disabled = false; }
          },
        }, "Save"),
      ));
  }

  /* ---------- lan / other devices ---------- */
  const WILDCARD_HOSTS = ["0.0.0.0", "::"];
  const isWildcardHost = (h) => WILDCARD_HOSTS.includes((h || "").trim());

  function lanCard() {
    const s = App.settings;
    const hostIn = el("input", {
      type: "text", value: s.host || "", placeholder: "127.0.0.1",
    });
    const tokenIn = el("input", { type: "password", value: s.lan_token || "", placeholder: "(none)" });
    const lanToggle = el("input", { type: "checkbox" });
    lanToggle.checked = isWildcardHost(s.host);
    lanToggle.addEventListener("change", () => {
      hostIn.value = lanToggle.checked ? "0.0.0.0" : "127.0.0.1";
    });
    hostIn.addEventListener("input", () => {
      lanToggle.checked = isWildcardHost(hostIn.value);
    });
    return el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "LAN / other devices"),
      el("div", { class: "pane-body" },
        el("label", { class: "row", style: "gap:8px;cursor:pointer;margin-bottom:10px" },
          lanToggle,
          el("span", null, el("b", null, "LAN mode"),
            " — allow other devices & VMs on my network to use this app")),
        el("div", { class: "field" }, el("label", null, "Host"), hostIn,
          el("div", { class: "muted small", style: "margin-top:3px" },
            "0.0.0.0 = LAN mode on, 127.0.0.1 = this PC only (advanced — the toggle above sets it). Restart required.")),
        el("div", { class: "field" }, el("label", null, "LAN token"), tokenIn,
          el("div", { class: "muted small", style: "margin-top:3px" },
            "Optional. When set, clients must enter this token.")),
        el("button", {
          class: "primary",
          onclick: async (e) => {
            e.currentTarget.disabled = true;
            try {
              await postSettings({
                host: hostIn.value.trim() || "127.0.0.1",
                lan_token: tokenIn.value,
              }, "LAN settings saved");
              fillLanPanel();
            } finally { e.currentTarget.disabled = false; }
          },
        }, "Save"),
        el("div", { id: "lan-urls-panel", style: "margin-top:14px" }),
      ));
  }

  async function fillLanPanel() {
    const box = document.getElementById("lan-urls-panel");
    if (!box) return;
    const lan = await api("/api/lan").catch(() => null);
    if (!box.isConnected) return;
    clear(box);
    if (!lan) {
      box.appendChild(el("div", { class: "muted small" }, "LAN status unavailable."));
      return;
    }
    const urls = lan.urls || [];
    const active = urls.length > 0;
    box.appendChild(el("div", { class: "row", style: "gap:8px;margin-bottom:10px" },
      el("span", {
        class: "pulse-dot" + (active ? "" : " off"),
        title: active ? "LAN mode active" : "normal mode",
      }),
      el("b", null, active ? "LAN mode" : "Normal mode"),
      el("span", { class: "muted small" },
        active ? "— other devices can connect (restart already applied this)"
               : `— localhost only (host ${lan.host}; tick “LAN mode” above and restart to share)`)));
    if (!urls.length) {
      return;
    }
    box.appendChild(el("div", { class: "muted small", style: "margin-bottom:6px" },
      "Clients on your network can open:"));
    for (const url of urls) {
      box.appendChild(el("div", { class: "row", style: "gap:8px;margin-bottom:4px" },
        el("code", { class: "small" }, url),
        el("button", { onclick: (e) => copyText(url, e.currentTarget) }, "Copy")));
    }
    if (lan.token_set) {
      box.appendChild(el("div", { class: "muted small", style: "margin-top:8px" },
        "A LAN token is set — clients will be asked for it when they connect."));
    }
    box.appendChild(el("div", { class: "muted small", style: "margin-top:8px" },
      "Windows firewall: allow inbound TCP ", String((App.settings && App.settings.port) || 8471),
      " — Settings → allow python.exe, or run: netsh advfirewall firewall add rule name=\"YT Learning Loader\" dir=in action=allow protocol=TCP localport=",
      String((App.settings && App.settings.port) || 8471)));
  }

  /* ---------- tab manager ---------- */
  function tabsCard() {
    const box = el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Tabs",
        el("span", { class: "muted small" }, "drag to reorder")),
      el("div", { class: "pane-body", id: "tab-manage-list" }));
    fillTabManager(box.querySelector("#tab-manage-list"));
    return box;
  }

  function fillTabManager(list) {
    if (!list) return;
    clear(list);
    const tabs = App.settings.tabs.slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
    let dragId = null;
    for (const tab of tabs) {
      /* rows must mutate the CURRENT object in App.settings.tabs — a prior
         persistTabs() replaces the array, so captured refs go stale */
      const fresh = () => App.settings.tabs.find(t => t.id === tab.id) || tab;
      const isSettings = tab.id === ALWAYS_VISIBLE;
      const row = el("div", { class: "tab-manage-row", dataset: { tabId: tab.id } },
        el("span", { class: "drag-handle", title: "Drag to reorder" }, "⠿"),
        el("span", { class: "tab-dot", style: `background:${tab.color || "#7c5cff"}` }),
        el("input", {
          class: "label-edit", value: tab.label || tab.id,
          onchange: async (e) => { fresh().label = e.target.value; await persistTabs(); },
        }),
        el("div", { class: "swatch-row" },
          TAB_COLORS.map(c => el("button", {
            class: `swatch${c === tab.color ? " selected" : ""}`, style: `background:${c}`,
            title: c, onclick: async () => { fresh().color = c; await persistTabs(); },
          })),
        ),
        el("label", { class: "row", style: "gap:4px;margin:0", title: "Visible in tab bar" },
          el("input", {
            type: "checkbox", checked: (tab.visible !== false) || isSettings || null,
            disabled: isSettings || null,
            onchange: async (e) => { fresh().visible = e.target.checked; await persistTabs(); },
          })),
      );
      row.draggable = true;
      row.addEventListener("dragstart", (e) => { dragId = tab.id; row.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
      row.addEventListener("dragend", () => { dragId = null; row.classList.remove("dragging"); list.querySelectorAll(".tab-manage-row").forEach(n => n.classList.remove("drag-over")); });
      row.addEventListener("dragover", (e) => { e.preventDefault(); if (dragId && dragId !== tab.id) row.classList.add("drag-over"); });
      row.addEventListener("dragleave", () => row.classList.remove("drag-over"));
      row.addEventListener("drop", async (e) => {
        e.preventDefault();
        row.classList.remove("drag-over");
        const fromId = dragId;
        if (!fromId || fromId === tab.id) return;
        const arr = App.settings.tabs.slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
        const fi = arr.findIndex(t => t.id === fromId), ti = arr.findIndex(t => t.id === tab.id);
        if (fi < 0 || ti < 0) return;
        const [moved] = arr.splice(fi, 1);
        arr.splice(ti, 0, moved);
        arr.forEach((t, i) => { t.order = i; });
        await persistTabs();
      });
      list.appendChild(row);
    }
  }

  /* ---------- data ---------- */
  async function loadDataCards() {
    const grid = document.querySelector(".settings-grid");
    if (!grid) return;
    const [state, profile] = await Promise.all([
      api("/api/state").catch(() => null),
      api("/api/profile").catch(() => null),
    ]);
    if (!grid.isConnected) return;
    const stats = state ? state.stats : null;
    const dataBox = document.getElementById("settings-data");
    if (dataBox && stats) {
      clear(dataBox).appendChild(el("div", { class: "pane" },
        el("div", { class: "pane-header" }, "Data"),
        el("div", { class: "pane-body" },
          el("div", { class: "stat-tiles", style: "margin-bottom:14px" },
            [["Total videos", stats.total], ["Learned", stats.learned],
             ["With transcript", stats.with_transcript], ["With learnings", stats.with_learnings]]
              .map(([label, value]) => el("div", { class: "stat-tile" },
                el("div", { class: "st-value" }, String(value)),
                el("div", { class: "st-label" }, label)))),
          el("button", {
            onclick: async () => {
              const data = await api("/api/videos");
              const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
              const a = el("a", { href: URL.createObjectURL(blob), download: "videos.json" });
              document.body.appendChild(a); a.click(); a.remove();
              setTimeout(() => URL.revokeObjectURL(a.href), 5000);
              toast("Export downloaded", "success");
            },
          }, "Export videos JSON"),
        )));
    }
    const profileBox = document.getElementById("settings-profile");
    if (profileBox) {
      clear(profileBox).appendChild(el("div", { class: "pane" },
        el("div", { class: "pane-header" }, "Profile"),
        el("div", { class: "pane-body" },
          profile && profile.path
            ? el("div", { class: "row" },
                el("span", { class: "status-dot ok" }),
                el("code", { class: "small" }, profile.path))
            : el("div", { class: "row" },
                el("span", { class: "status-dot no" }),
                el("span", { class: "muted small" }, "Profile file not found — set profile_path below.")),
          el("div", { class: "field", style: "margin-top:12px" },
            el("label", null, "profile_path"),
            el("input", {
              value: (App.settings && App.settings.profile_path) || "../nils-profile.md",
              onchange: async (e) => { await postSettings({ profile_path: e.target.value }, "Profile path saved"); },
            })),
        )));
    }
  }

  /* ---------- learnings ---------- */
  function learningsCard() {
    const box = el("div", { class: "pane" },
      el("div", { class: "pane-header" }, "Learnings"),
      el("div", { class: "pane-body" },
        el("p", { class: "muted small", style: "margin-top:0" },
          "Learnings are generated by Kimi Code from transcripts. To (re)generate: ",
          el("strong", { style: "color:var(--text)" }, "ask in chat to regenerate learnings for a video.")),
        el("div", { id: "learnings-status-list", class: "scroll", style: "max-height:260px" }, skeletonLines(4))),
    );
    fillLearningsStatus(box.querySelector("#learnings-status-list"));
    return box;
  }

  async function fillLearningsStatus(list) {
    if (!list) return;
    const videos = await getVideos().catch(() => []);
    if (!list.isConnected) return;
    clear(list);
    for (const v of videos) {
      list.appendChild(el("div", { class: "chk-item", style: "cursor:default" },
        el("span", { class: `status-dot ${v.has_learnings ? "ok" : "no"}` }),
        el("span", { style: "flex:1" }, v.title),
        el("span", { class: "muted small" }, v.has_learnings ? "analyzed" : "no learnings")));
    }
  }

  App.tabs.settings = { render, built: false };
})();
