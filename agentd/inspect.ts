// Dump the transcript of the root conversation of a harness db (inspection tool).
// Usage: npx tsx inspect.ts <dbPath>
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { createModels } from "@earendil-works/pi-ai/models";
import { fauxProvider } from "@earendil-works/pi-ai/providers/faux";
import { AssistantEntry, createRegistry, Harness } from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";

const dbPath = process.argv[2];
const context = BACKGROUND_CONTEXT;
const models = createModels();
models.setProvider(fauxProvider().provider);
const harness = await Harness.open(await openNodeSqliteStorage(dbPath), { models, registry: createRegistry() }, context);
const root = await harness.root(context);
const page = await root.entries({}, 100, undefined, context);
for (const e of [...page.items].reverse()) {
  const msg = e.model?.[0];
  let summary = "";
  if (AssistantEntry.is(e) && msg && Array.isArray(msg.content)) {
    const t = msg.content.filter((b) => b.type === "text").map((b) => b.text).join(" ");
    summary = ` text=${JSON.stringify(t.slice(0, 60))} stopReason=${(msg as { stopReason?: string }).stopReason ?? "?"} aborted=${(msg as { errorMessage?: string }).errorMessage ?? ""}`;
  }
  console.log(`entry ${e.id} kind=${e.kind}${summary}`);
}
await harness.close(context);
