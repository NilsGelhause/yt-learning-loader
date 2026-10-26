// agentd: long-lived HTTP sidecar hosting one Pi Durable Harness for the
// yt learning loader app. Binds 127.0.0.1 only. Never crashes on bad input.
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import type { Context } from "@earendil-works/chord";
import { createModels } from "@earendil-works/pi-ai/models";
import { kimiCodingProvider } from "@earendil-works/pi-ai/providers/kimi-coding";
import { moonshotaiProvider } from "@earendil-works/pi-ai/providers/moonshotai";
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  type FauxResponseFactory,
} from "@earendil-works/pi-ai/providers/faux";
import {
  AssistantEntry,
  ConversationBusy,
  createRegistry,
  Harness,
  watchEvents,
  type AgentEvent,
  type Conversation,
} from "@earendil-works/pi-durable";
import { CodingTools } from "@earendil-works/pi-durable/tools";
import { NodeExecutionEnv } from "@earendil-works/pi-durable/env/node";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";

const here = dirname(fileURLToPath(import.meta.url)); // agentd/src
const agentdRoot = resolve(here, ".."); // agentd/
const appRoot = resolve(agentdRoot, ".."); // yt learning loader/

// ---------- env ----------
function loadDotEnv(path: string): void {
  if (!existsSync(path)) return;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const i = t.indexOf("=");
    if (i <= 0) continue;
    const k = t.slice(0, i).trim();
    if (process.env[k] === undefined) process.env[k] = t.slice(i + 1).trim();
  }
}
loadDotEnv(resolve(agentdRoot, ".env")); // real process env vars win

const FAUX = process.env.YTL_FAUX_MODEL === "1";
const PORT = Number(process.env.YTL_AGENT_PORT ?? "8472") || 8472;
const KEY_CONFIGURED = FAUX || Boolean(process.env.KIMI_API_KEY);
const dbPath = resolve(process.env.YTL_AGENT_DB ?? resolve(appRoot, "data", "agent", "harness.sqlite"));
const dataDir = dirname(dbPath);
mkdirSync(dataDir, { recursive: true });
const logPath = resolve(dataDir, "agentd.log");
const mapPath = resolve(dataDir, "conversations.json");

function log(...parts: unknown[]): void {
  const line = `${new Date().toISOString()} ${parts.map((p) => (typeof p === "string" ? p : JSON.stringify(p))).join(" ")}\n`;
  try {
    appendFileSync(logPath, line);
  } catch {
    /* logging must never kill the server */
  }
  process.stdout.write(line);
}

// ---------- models & registry ----------
const models = createModels();

/** Text of a message content that may be a plain string or a block array. */
function contentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((b): b is { type: "text"; text: string } => Boolean(b) && (b as { type?: string }).type === "text")
      .map((b) => b.text)
      .join(" ");
  }
  return "";
}

if (FAUX) {
  const tps = Number(process.env.YTL_FAUX_TPS ?? "") || undefined; // test knob: slow the stream
  const faux = fauxProvider({ tokensPerSecond: tps });
  // Infinite echo supply: every invocation queues its own replacement.
  const echo: FauxResponseFactory = (ctx) => {
    faux.appendResponses([echo]);
    const lastUser = [...ctx.messages].reverse().find((m) => m.role === "user");
    const text = lastUser ? contentText(lastUser.content) : "";
    return fauxAssistantMessage(fauxText(`faux reply: ${text}`));
  };
  faux.setResponses([echo]);
  models.setProvider(faux.provider);
} else {
  models.setProvider(kimiCodingProvider()); // reads KIMI_API_KEY
  // The same key also works on the Moonshot platform endpoint (pay-as-you-go);
  // allow YTL_AGENT_MODEL=moonshotai/kimi-k2.6 etc. as an escape hatch.
  if (process.env.MOONSHOT_API_KEY === undefined && process.env.KIMI_API_KEY) {
    process.env.MOONSHOT_API_KEY = process.env.KIMI_API_KEY;
  }
  try {
    models.setProvider(moonshotaiProvider()); // reads MOONSHOT_API_KEY
  } catch (e) {
    log("moonshotai provider not registered:", String(e));
  }
}

function resolveModel(): { provider: string; modelId: string } {
  const override = process.env.YTL_AGENT_MODEL?.trim();
  if (override) {
    const i = override.indexOf("/");
    if (i > 0) return { provider: override.slice(0, i), modelId: override.slice(i + 1) };
    return { provider: FAUX ? "faux" : "kimi-coding", modelId: override };
  }
  return FAUX ? { provider: "faux", modelId: "faux-1" } : { provider: "kimi-coding", modelId: "kimi-for-coding" };
}
const MODEL = resolveModel();

const registry = createRegistry();
registry.install(CodingTools); // read, write, edit, bash

const context: Context = BACKGROUND_CONTEXT;
const harness = await Harness.open(await openNodeSqliteStorage(dbPath), {
  models,
  registry,
  env: ({ cwd }) => new NodeExecutionEnv({ cwd: cwd ?? appRoot }),
}, context);
harness.resume(); // finish runs a previous process left unfinished
log(`harness open: db=${dbPath} model=${MODEL.provider}/${MODEL.modelId} faux=${FAUX}`);

harness.closed.then((end) => {
  log("harness closed:", end.reason, end.reason === "failed" ? String(end.error) : "");
});

// ---------- conversation key mapping ----------
type ConvMap = Record<string, { id: string; cwd: string }>;
function loadConvMap(): ConvMap {
  try {
    if (existsSync(mapPath)) return JSON.parse(readFileSync(mapPath, "utf8"));
  } catch (e) {
    log("conversations.json unreadable, starting empty:", String(e));
  }
  return {};
}
let convMap: ConvMap = loadConvMap();
function saveConvMap(): void {
  try {
    writeFileSync(mapPath, JSON.stringify(convMap, null, 2));
  } catch (e) {
    log("failed to save conversations.json:", String(e));
  }
}

async function conversationForKey(key: string, cwd: string): Promise<Conversation> {
  const relCwd = cwd ? resolve(appRoot, cwd) : appRoot;
  const existing = convMap[key];
  if (existing) {
    const conv = await harness.conversation(existing.id as never, context);
    if (conv) return conv;
    log(`conversation ${existing.id} for key "${key}" missing from storage; recreating`);
  }
  const conv = await harness.createConversation(
    {
      ownership: { kind: "ownerless" },
      agent: { model: { provider: MODEL.provider, modelId: MODEL.modelId }, cwd: relCwd },
    },
    context,
  );
  convMap[key] = { id: String(conv.id), cwd: relCwd };
  saveConvMap();
  log(`created conversation ${conv.id} for key "${key}" cwd=${relCwd}`);
  return conv;
}

// ---------- jobs ----------
type Line = { t: "out" | "err"; x: string };
type JobStatus = "running" | "done" | "error" | "cancelled";
type Job = {
  id: string;
  key: string;
  conversationId: string;
  status: JobStatus;
  lines: Line[];
  exitCode: number | null;
  finalText: string | null;
  cancelRequested: boolean;
  settle?: (() => void) | undefined;
};
const jobs = new Map<string, Job>();
const runningByKey = new Map<string, string>(); // key -> jobId

const MAX_LINES = 800;
const MAX_LINE = 1000;

function pushLine(job: Job, t: Line["t"], x: string): void {
  // split into <=1000 char lines, keep at most MAX_LINES (newest survive)
  for (let i = 0; i < x.length; i += MAX_LINE) {
    job.lines.push({ t, x: x.slice(i, i + MAX_LINE) });
  }
  while (job.lines.length > MAX_LINES) job.lines.shift();
}

function preview(value: unknown, max = 300): string {
  let s: string;
  try {
    s = typeof value === "string" ? value : JSON.stringify(value);
  } catch {
    s = String(value);
  }
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

async function runJob(job: Job, conv: Conversation, submissionId: string): Promise<void> {
  let textAcc = "";
  let sawTextDelta = false;
  const listener = async (events: readonly AgentEvent[]): Promise<void> => {
    for (const ev of events) {
      try {
        switch (ev.type) {
          case "message_update":
            for (const ch of ev.changes) {
              if (ch.type === "text_delta") {
                sawTextDelta = true;
                textAcc += ch.delta;
                pushLine(job, "out", ch.delta);
              } else if (ch.type === "block" || ch.type === "message") {
                const blocks = ch.type === "block" ? [ch.block] : ch.message.content;
                for (const b of blocks) {
                  if (b.type === "text") {
                    sawTextDelta = true;
                    textAcc += b.text;
                    pushLine(job, "out", b.text);
                  }
                }
              }
            }
            break;
          case "tool_execution_start":
            pushLine(job, "out", `$ ${ev.toolName} ${preview(ev.args)}`);
            break;
          case "tool_execution_end": {
            const entry = ev.entry;
            const text = entry?.model?.[0] ? contentText(entry.model[0].content) : "";
            if (text) pushLine(job, "out", preview(text, MAX_LINE));
            break;
          }
          case "task_failed":
            pushLine(job, "err", `task failed (${ev.kind}): ${ev.message}`);
            break;
          case "auto_retry_start":
            pushLine(job, "err", `retry attempt ${ev.attempt}: ${preview(ev.errorMessage, 500)}`);
            break;
          case "run_end":
            break;
          default:
            break;
        }
      } catch (e) {
        log("event handler error:", String(e));
      }
    }
  };

  let events: Awaited<ReturnType<typeof watchEvents>> | undefined;
  try {
    events = await watchEvents(harness, conv.id, context);
    events.start(listener);
  } catch (e) {
    log("watchEvents failed:", String(e));
  }

  try {
    const submission = await harness.submission(submissionId as never, context);
    if (!submission) throw new Error(`submission ${submissionId} not found`);
    const settled = await submission.wait(context);
    if (settled.status === "done" && settled.type === "input") {
      if (!textAcc) {
        const entry = await conv.commit((tx) => tx.entry(AssistantEntry, settled.answer), context);
        textAcc = entry?.model?.[0] ? contentText(entry.model[0].content) : "";
      }
      job.status = "done";
      job.finalText = textAcc || null;
      // Runs that finished before the event stream attached have no delta
      // lines; seed the stream from the answer so the job is never line-less.
      if (!sawTextDelta && textAcc) pushLine(job, "out", preview(textAcc, MAX_LINE));
    } else {
      const detail = settled.detail !== undefined ? ` ${preview(settled.detail, 500)}` : "";
      pushLine(job, "err", `unanswered: ${settled.reason}${detail}`);
      job.status = job.cancelRequested ? "cancelled" : "error";
      job.finalText = textAcc || null;
    }
  } catch (e) {
    pushLine(job, "err", e instanceof Error ? e.message : String(e));
    job.status = job.cancelRequested ? "cancelled" : "error";
    job.finalText = textAcc || null;
  } finally {
    try {
      await events?.stop();
    } catch {
      /* ignore */
    }
    if (runningByKey.get(job.key) === job.id) runningByKey.delete(job.key);
    log(`job ${job.id} (${job.key}) settled: ${job.status}`);
    job.settle?.();
  }
}

// ---------- http ----------
function send(res: ServerResponse, code: number, body: unknown): void {
  const data = JSON.stringify(body);
  res.writeHead(code, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(data) });
  res.end(data);
}

async function readBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const b = chunk as Buffer;
    size += b.length;
    if (size > 1_000_000) throw new Error("body too large");
    chunks.push(b);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const method = req.method ?? "GET";

    if (method === "GET" && path === "/health") {
      send(res, 200, { ok: true, keyConfigured: KEY_CONFIGURED, model: `${MODEL.provider}/${MODEL.modelId}` });
      return;
    }

    if (method === "POST" && path === "/jobs") {
      const body = (await readBody(req)) as Record<string, unknown>;
      const key = typeof body.key === "string" ? body.key : "";
      const message = typeof body.message === "string" ? body.message : "";
      const cwd = typeof body.cwd === "string" && body.cwd ? body.cwd : "";
      if (!key || !message) {
        send(res, 400, { error: "key and message are required strings" });
        return;
      }
      if (runningByKey.has(key)) {
        send(res, 409, { error: "busy" });
        return;
      }
      const conv = await conversationForKey(key, cwd);
      if (body.newSession === true) {
        await conv.reset(undefined, context);
        log(`conversation ${conv.id} reset for key "${key}"`);
      }
      const requestId =
        typeof body.requestId === "string" && body.requestId
          ? body.requestId
          : createHash("sha256").update(`${key}\n${message}`).digest("hex");
      let submission;
      try {
        submission = await conv.submit(
          { type: "input", content: message, requestId, whenBusy: "reject" },
          context,
        );
      } catch (e) {
        if (e instanceof ConversationBusy) {
          send(res, 409, { error: "busy" });
          return;
        }
        throw e;
      }
      const job: Job = {
        id: String(submission.id),
        key,
        conversationId: String(conv.id),
        status: "running",
        lines: [],
        exitCode: null,
        finalText: null,
        cancelRequested: false,
      };
      jobs.set(job.id, job);
      runningByKey.set(key, job.id);
      log(`job ${job.id} key="${key}" requestId=${requestId} submitted`);
      void runJob(job, conv, job.id).catch((e) => {
        log("runJob crashed:", String(e));
        job.status = job.cancelRequested ? "cancelled" : "error";
        pushLine(job, "err", String(e));
        if (runningByKey.get(key) === job.id) runningByKey.delete(key);
        job.settle?.();
      });
      send(res, 200, { jobId: job.id });
      return;
    }

    const jobMatch = path.match(/^\/jobs\/([^/]+)$/);
    if (method === "GET" && jobMatch) {
      const job = jobs.get(jobMatch[1]);
      if (!job) {
        send(res, 404, { error: "not found" });
        return;
      }
      send(res, 200, {
        status: job.status,
        lines: job.lines,
        exitCode: job.exitCode,
        finalText: job.finalText,
      });
      return;
    }

    const cancelMatch = path.match(/^\/jobs\/([^/]+)\/cancel$/);
    if (method === "POST" && cancelMatch) {
      const job = jobs.get(cancelMatch[1]);
      if (!job) {
        send(res, 404, { error: "not found" });
        return;
      }
      if (job.status !== "running") {
        send(res, 200, { ok: true });
        return;
      }
      job.cancelRequested = true;
      try {
        const conv = await harness.conversation(job.conversationId as never, context);
        const submission = await harness.submission(job.id as never, context);
        const result = submission ? await submission.abort(context) : "settled";
        if (result !== "aborted" && conv) await conv.abort(context);
        log(`job ${job.id} cancel requested (abort=${result})`);
      } catch (e) {
        log("cancel failed:", String(e));
      }
      send(res, 200, { ok: true });
      return;
    }

    send(res, 404, { error: "not found" });
  } catch (e) {
    log("request error:", e instanceof Error ? e.message : String(e));
    try {
      send(res, 500, { error: "internal error" });
    } catch {
      /* ignore */
    }
  }
});

server.listen(PORT, "127.0.0.1", () => {
  log(`agentd listening on http://127.0.0.1:${PORT}`);
});
