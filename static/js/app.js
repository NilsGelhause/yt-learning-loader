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
  const id = (location.hash || "#overview").slice(1);
  const known = App.settings.tabs.some(t => t.id === id && (t.visible || t.id === ALWAYS_VISIBLE));
  showTab(known ? id : "overview");
})();
