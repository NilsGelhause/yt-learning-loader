// Guest-side tool probe: which start-script tools exist on PATH? Diagnoses
// why `setsid nohup npx tsx ...` in start-agentd.sh produces no process.
// Run in the guest: curl -s <master>/kit/guest-probe.js | node
const fs = require("fs");
const path = (process.env.PATH || ":").split(":");
const tools = ["setsid", "nohup", "npx", "sh", "node", "curl", "env"];
for (const t of tools) {
  let found = "MISSING";
  for (const d of path) {
    try {
      if (fs.existsSync(d + "/" + t)) { found = d + "/" + t; break; }
    } catch (e) { }
  }
  console.log(t.padEnd(8), found);
}
console.log("PATH =", process.env.PATH);
