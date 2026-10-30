// Idle-connection probe: connect to the master's relay dial port and watch
// how long the TCP stream survives with zero traffic. Diagnoses the ~1s
// drops (QEMU slirp idle timeout vs. relay logic vs. local sidecar).
// Run in the guest: curl -s <master>/kit/idle-test.js | node
const net = require("net");
const t0 = Date.now();
const s = net.connect(18472, "192.168.50.111", () => {
  console.log("connected, watching for 12s of silence...");
  s.on("close", () => {
    console.log("CLOSED after", Date.now() - t0, "ms");
    process.exit(0);
  });
  s.on("error", (e) => {
    console.log("ERROR after", Date.now() - t0, "ms:", e.message);
    process.exit(0);
  });
  setTimeout(() => {
    console.log("ALIVE after 12s idle — slirp is NOT the dropper");
    process.exit(0);
  }, 12000);
});
