// Inspect the transcripts stored in a Pi Durable harness db (read-only viewer).
//
//   npx tsx inspect.ts                          # root conversation of the app's db
//   npx tsx inspect.ts teach:my-workspace       # any app conversation, by key
//   npx tsx inspect.ts path/to/other.sqlite [key] [--full]
//
// Safe to run while the app is live: the db is copied to a temp snapshot first
// (two processes must never open one harness storage at the same time).
import { copyFileSync, existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { createModels } from "@earendil-works/pi-ai/models";
import { fauxProvider } from "@earendil-works/pi-ai/providers/faux";
import {
  AssistantEntry,
  createRegistry,
  Harness,
  ToolResultEntry,
  UserEntry,
} from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";

const here = resolve(fileURLToPath(new URL(".", import.meta.url)));
const DEFAULT_DB = resolve(here, "..", "data", "agent", "harness.sqlite");

// --- args: [db] [key] [--full]; db optional, key resolved via conversations.json
const args = process.argv.slice(2);
const full = args.includes("--full");
const positional = args.filter((a) => a !== "--full");
let dbPath = DEFAULT_DB;
let key: string | undefined;
if (positional[0] && (positional[0].endsWith(".sqlite") || existsSync(positional[0]))) {
  dbPath = resolve(positional[0]);
  key = positional[1];
} else if (positional[0]) {
  key = positional[0];
}

// --- snapshot the db (never open live storage owned by the sidecar)
const snapDir = mkdtempSync(join(tmpdir(), "agentd-inspect-"));
const snapDb = join(snapDir, "snapshot.sqlite");
for (const suffix of ["", "-wal", "-shm"]) {
  const src = dbPath + suffix;
  if (existsSync(src)) copyFileSync(src, snapDb + suffix);
}

const context = BACKGROUND_CONTEXT;
const models = createModels();
models.setProvider(fauxProvider().provider); // never called; inspect is read-only
const harness = await Harness.open(await openNodeSqliteStorage(snapDb), { models, registry: createRegistry() }, context);

let conversation;
if (key) {
  const mapPath = join(resolve(dbPath, ".."), "conversations.json");
  const map: Record<string, { id: string; cwd: string }> = existsSync(mapPath)
    ? JSON.parse(readFileSync(mapPath, "utf8"))
    : {};
  const hit = map[key];
  if (!hit) {
    console.log(`no conversation for key "${key}" in ${mapPath}`);
    console.log("known keys:");
    for (const k of Object.keys(map)) console.log(`  ${k}  (conversation ${map[k].id})`);
    process.exit(1);
  }
  conversation = await harness.conversation(hit.id as never, context);
  console.log(`conversation ${hit.id} for key "${key}"  cwd=${hit.cwd}`);
} else {
  conversation = await harness.root(context);
  console.log(`root conversation ${conversation.id}`);
}
if (!conversation) {
  console.log("conversation not found in this db");
  process.exit(1);
}

const TRUNC = full ? Infinity : 200;

function contentText(content: unknown): { text: string; tools: string[] } {
  if (typeof content === "string") return { text: content, tools: [] };
  if (!Array.isArray(content)) return { text: "", tools: [] };
  const text: string[] = [];
  const tools: string[] = [];
  for (const b of content as { type?: string; text?: string; name?: string }[]) {
    if (b?.type === "text" && b.text) text.push(b.text);
    if (b?.type === "tool_use" && b.name) tools.push(b.name);
  }
  return { text: text.join(" "), tools };
}

function preview(s: string): string {
  const oneLine = s.replace(/\s+/g, " ").trim();
  return oneLine.length > TRUNC ? oneLine.slice(0, TRUNC) + " …" : oneLine;
}

type Entry = (typeof conversation.entries extends (...a: never[]) => Promise<{ items: infer I }> ? I : never) extends (infer E)[] ? E : never;

function describe(e: Entry): string {
  let line = `entry ${e.id} ${e.kind}`;
  const msg = e.model?.[0] as
    | { content?: unknown; stopReason?: string; errorMessage?: string }
    | undefined;
  if (AssistantEntry.is(e) && msg) {
    const { text, tools } = contentText(msg.content);
    if (tools.length) line += ` tools=[${tools.join(", ")}]`;
    if (text) line += `\n  ${preview(text)}`;
    line += `\n  stopReason=${msg.stopReason ?? "?"}`;
    if (msg.errorMessage) line += ` error=${preview(msg.errorMessage)}`;
  } else if (UserEntry.is(e) && msg) {
    const { text } = contentText(msg.content);
    if (text) line += `\n  ${preview(text)}`;
  } else if (ToolResultEntry.is(e)) {
    line += ` tool=${(e as { toolName?: string }).toolName ?? "?"}`;
    const { text } = contentText(msg?.content);
    if (text) line += `\n  ${preview(text)}`;
  }
  return line;
}

// pages come newest-first; collect, then print oldest-first
const collected: Entry[] = [];
let cursor: unknown;
for (;;) {
  const page = await conversation.entries({}, 100, cursor as never, context);
  collected.push(...(page.items as Entry[]));
  if (!page.hasMore) break;
  cursor = page.nextCursor;
}
for (const e of [...collected].reverse()) console.log(describe(e));

await harness.close(context);
rmSync(snapDir, { recursive: true, force: true });
