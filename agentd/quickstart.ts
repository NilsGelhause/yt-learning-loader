// Quickstart: prove Pi Durable + Kimi For Coding works end to end, durably.
// Run with: npx tsx quickstart.ts ["your question"]
// With a dead/absent key you can still demo durability: YTL_FAUX_MODEL=1 npx tsx quickstart.ts
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { createModels } from "@earendil-works/pi-ai/models";
import { kimiCodingProvider } from "@earendil-works/pi-ai/providers/kimi-coding";
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  type FauxResponseFactory,
} from "@earendil-works/pi-ai/providers/faux";
import { AssistantEntry, createRegistry, Harness } from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";

const here = dirname(fileURLToPath(import.meta.url)); // agentd/
const appRoot = resolve(here, ".."); // yt learning loader/

// Minimal .env loading (same rule as the server: real env vars win).
const envFile = resolve(here, ".env");
if (existsSync(envFile)) {
  for (const line of readFileSync(envFile, "utf8").split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const i = t.indexOf("=");
    if (i <= 0) continue;
    const k = t.slice(0, i).trim();
    if (process.env[k] === undefined) process.env[k] = t.slice(i + 1).trim();
  }
}

const dbPath = resolve(process.env.YTL_AGENT_DB ?? resolve(appRoot, "data", "agent", "harness.sqlite"));
mkdirSync(dirname(dbPath), { recursive: true });

const context = BACKGROUND_CONTEXT;
const models = createModels();
const FAUX = process.env.YTL_FAUX_MODEL === "1";
let modelRef: { provider: string; modelId: string };
if (FAUX) {
  // Offline stand-in for the Kimi provider: echoes the last user message with
  // enough padding (and an optional slow token rate) to demo kill/resume.
  const tps = Number(process.env.YTL_FAUX_TPS ?? "") || undefined;
  const faux = fauxProvider({ tokensPerSecond: tps });
  const echo: FauxResponseFactory = (ctx) => {
    faux.appendResponses([echo]);
    const lastUser = [...ctx.messages].reverse().find((m) => m.role === "user");
    const text =
      typeof lastUser?.content === "string"
        ? lastUser.content
        : lastUser && Array.isArray(lastUser.content)
          ? lastUser.content.filter((b) => b.type === "text").map((b) => b.text).join(" ")
          : "";
    const padding = " The quick brown fox jumps over the lazy dog.".repeat(30);
    return fauxAssistantMessage(fauxText(`faux reply to "${text}":${padding}`));
  };
  faux.setResponses([echo]);
  models.setProvider(faux.provider);
  modelRef = { provider: "faux", modelId: "faux-1" };
} else {
  models.setProvider(kimiCodingProvider()); // reads KIMI_API_KEY
  modelRef = { provider: "kimi-coding", modelId: process.env.YTL_AGENT_MODEL || "kimi-for-coding" };
}

const registry = createRegistry();
const harness = await Harness.open(await openNodeSqliteStorage(dbPath), { models, registry }, context);

const root = await harness.root(context, {
  agent: { model: { provider: modelRef.provider, modelId: modelRef.modelId } },
});
harness.resume();

const question = process.argv[2] ?? "What model are you, and what can you do?";
const requestId = `quickstart:${Buffer.from(question).toString("base64").slice(0, 24)}`;
console.log(`Submitting: ${question}`);
const submission = await root.submit({ type: "input", content: question, requestId }, context);
console.log(`Submission id: ${submission.id} (requestId ${requestId})`);
const settled = await submission.wait(context);
console.log(`Settled: status=${settled.status}`);
if (settled.status === "done" && settled.type === "input") {
  const answer = await root.commit((tx) => tx.entry(AssistantEntry, settled.answer), context);
  const msg = answer?.model?.[0];
  const text =
    msg && Array.isArray(msg.content)
      ? msg.content.filter((b) => b.type === "text").map((b) => b.text).join("")
      : "";
  console.log("--- assistant ---");
  console.log(text);
  console.log("--- end ---");
} else {
  console.log("Not answered:", settled);
}
await harness.close(context);
