// Pipe-probe: replicate the agentd relay-out wiring exactly (dial to the
// master relay + pipe both ways to the local sidecar) and report which side
// ends first. Diagnoses the ~1s channel drops.
// Run in the guest: curl -s <master>/kit/pipe-test.js | node
const net = require("net");
const t0 = Date.now();
const up = net.connect(18472, "192.168.50.111", () => {
  console.log("up connected");
  const local = net.connect(8472, "127.0.0.1", () => {
    console.log("local connected — piping");
    up.pipe(local);
    local.pipe(up);
  });
  local.on("end", () => console.log("local END after", Date.now() - t0, "ms"));
  local.on("close", () => console.log("local CLOSE after", Date.now() - t0, "ms"));
  local.on("error", (e) => console.log("local ERROR:", e.message));
  setTimeout(() => {
    console.log("12s report: up.destroyed=" + up.destroyed + " local.destroyed=" + local.destroyed);
    process.exit(0);
  }, 12000);
});
up.on("end", () => console.log("up END after", Date.now() - t0, "ms"));
up.on("close", () => console.log("up CLOSE after", Date.now() - t0, "ms"));
up.on("error", (e) => console.log("up ERROR:", e.message));
