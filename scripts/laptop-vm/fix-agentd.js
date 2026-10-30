// One-shot guest-side fix for the laptop-vm agentd sidecar: kill the stale
// no-relay sidecar (pkill/pgrep are missing on the guest image, so we scan
// /proc and SIGKILL by pid), then re-run start-agentd.sh which carries
// YTL_RELAY_OUT, and finally probe 127.0.0.1:8472 to confirm it listens.
// Run: curl -s <master>/kit/fix-agentd.js | node
const fs = require("fs");
const net = require("net");
const { spawn } = require("child_process");

const self = process.pid;
const killed = [];
for (const p of fs.readdirSync("/proc")) {
  if (!/^[0-9]+$/.test(p)) continue;
  const pid = parseInt(p, 10);
  if (pid === self) continue;
  try {
    const c = fs.readFileSync("/proc/" + p + "/cmdline", "utf8")
      .split(String.fromCharCode(0)).join(" ");
    if (c.includes("src/server.ts")) {
      process.kill(pid, "SIGKILL");
      killed.push(pid + " " + c.slice(0, 60));
    }
  } catch (e) { }
}
console.log("killed:", killed.join(" | ") || "none");

// A SIGKILLed sidecar leaves stale SQLite WAL/SHM files behind; the next
// sidecar then blocks silently on the DB open. Clean them before restart.
const db = "/home/cua/agentd-data/harness.sqlite";
for (const suffix of ["-wal", "-shm"]) {
  try { fs.unlinkSync(db + suffix); console.log("removed", db + suffix); }
  catch (e) { }
}

// Rewrite the start script ourselves: keep the relay target from whatever
// version exists (install-agent drops it, expose-agent sets it) so a
// restart never comes up relay-less.
let relay = "";
try {
  const cur = fs.readFileSync("/home/cua/start-agentd.sh", "utf8");
  const m = cur.match(/YTL_RELAY_OUT=([^\n]+)/);
  if (m) relay = "export YTL_RELAY_OUT=" + m[1].trim() + "\n";
} catch (e) { }
const script = "#!/bin/sh\n"
  + "mkdir -p /home/cua/agentd-data\n"
  + "export YTL_AGENT_PORT=8472\n"
  + "export YTL_AGENT_HOST=0.0.0.0\n"
  + "export YTL_AGENT_DB=/home/cua/agentd-data/harness.sqlite\n"
  + relay
  + "cd /home/cua/agentd\n"
  + "setsid nohup npx tsx src/server.ts >>/home/cua/agentd-data/server.log 2>&1 </dev/null &\n";
try {
  fs.writeFileSync("/home/cua/start-agentd.sh", script);
  fs.chmodSync("/home/cua/start-agentd.sh", 0o755);
  console.log("start-agentd.sh rewritten" + (relay ? " (with relay)" : " (NO RELAY)"));
} catch (e) {
  console.log("script rewrite failed:", e.message);
}

setTimeout(() => {
  const child = spawn("sh", ["/home/cua/start-agentd.sh"],
    { detached: true, stdio: "ignore" });
  child.on("error", (e) => console.log("spawn error:", e.message));
  child.unref();
  setTimeout(() => {
    let tries = 0;
    const probe = () => {
      tries += 1;
      const s = net.connect(8472, "127.0.0.1", () => {
        console.log("sidecar 127.0.0.1:8472: ACCEPTING");
        s.end();
        process.exit(0);
      });
      s.on("error", (e) => {
        if (tries >= 15) {
          console.log("sidecar 127.0.0.1:8472:", e.message);
          process.exit(1);
        }
        setTimeout(probe, 2000);
      });
    };
    probe();
  }, 3000);
}, 1000);
