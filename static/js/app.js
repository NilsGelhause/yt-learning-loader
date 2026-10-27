/* app.js — boot: settings, appearance, mermaid, tab bar, routing */
"use strict";

(async function boot() {
  /* power / shutdown button */
  document.getElementById("power-btn").addEventListener("click", async () => {
    if (!confirm("Stop the learning server? You'll need to run start.bat to launch it again.")) return;
    try { await api("/api/shutdown", { method: "POST" }); } catch { /* server may already be down */ }
    document.getElementById("dead-overlay").style.display = "flex";
  });
  try {
    await getSettings(true);
  } catch (err) {
    document.getElementById("screen").appendChild(
      el("div", { class: "empty-state" },
        el("span", { class: "icon" }, "⚠"),
        "Could not load settings — is the server running? Refresh to retry."));
    return;
  }
  applyAppearance();
  initMermaid();
  renderTabbar();
  // the power button stops the whole app — master machine only
  try {
    const client = await api("/api/client");
    if (!client.master) {
      const btn = document.getElementById("power-btn");
      btn.classList.add("disabled");
      btn.title = "Only the master machine can stop the server";
    }
  } catch { /* on error leave the button enabled — local is the default */ }
  const id = (location.hash || "#overview").slice(1);
  const known = App.settings.tabs.some(t => t.id === id && (t.visible || t.id === ALWAYS_VISIBLE));
  showTab(known ? id : "overview");
})();
