// Guest-side diagnostic for the laptop-vm agentd sidecar. Served via the
// master's /kit endpoint, run in the guest with: curl -s <master>/kit/diag.js | node
const fs = require("fs");
const out = [];
function show(label, text) {
  out.push("=== " + label + " ===");
  out.push(text || "(missing)");
  out.push("");
}

// 1. sidecar server log
try {
  const log = fs.readFileSync("/home/cua/agentd-data/server.log", "utf8");
  show("server.log (tail)", log.slice(-1500));
} catch (e) {
  show("server.log (tail)", "NO LOG: " + e.message);
}

// 2. node/tsx processes via /proc
let procs = [];
try {
  for (const p of fs.readdirSync("/proc")) {
    if (!/^[0-9]+$/.test(p)) continue;
    try {
      const c = fs.readFileSync("/proc/" + p + "/cmdline", "utf8")
        .split(String.fromCharCode(0)).join(" ");
      if (c.includes("node") || c.includes("tsx")) procs.push(p + "  " + c);
    } catch (e) { }
  }
} catch (e) {
  procs.push("proc scan failed: " + e.message);
}
show("node/tsx processes", procs.join("\n") || "NONE RUNNING");

// 3. start script
try {
  show("start-agentd.sh", fs.readFileSync("/home/cua/start-agentd.sh", "utf8"));
} catch (e) {
  show("start-agentd.sh", "NO SCRIPT: " + e.message);
}

// 4. agentd bundle present?
try {
  const files = fs.readdirSync("/home/cua/agentd").join(", ");
  show("agentd bundle", files.slice(0, 400));
} catch (e) {
  show("agentd bundle", "MISSING: " + e.message);
}

console.log(out.join("\n"));
