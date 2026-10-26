---
name: ai-prompting
description: "Distilled prompting practice: write, diagnose, and compress prompts (chat or coding agent). Use when drafting a prompt from scratch, cutting a bloated prompt, fixing a bad AI output by naming the failed prompt part, steering coding agents, or teaching prompting to the learner."
source_workspace: teach/free-i-want-to-learn-prompting
---

# ai-prompting

Token-efficient prompting as practiced rules, distilled from the finished teaching
workspace `teach/free-i-want-to-learn-prompting`. The learner's mission: prompting
with ease (fluency) and token efficiency — more result per token, and knowing why
the short prompt works. Learner profile (Java/backend dev, AI beginner, goal
"AI does the work, I review") lives in the workspace's `MISSION.md`; read it before
teaching.

## Core model

An LLM predicts the next token given the entire conversation, every step. The
prompt is not a form — it is the machine's entire working memory, re-read for
each token. Bloat therefore costs twice: tokens spent on words that change
nothing, and attention diluted across the noise.

**Removal test** (the one rule behind everything below): *would removing this
cause a worse answer? If not, cut it.* Applies to every word of a prompt, every
part of a prompt, and every line of an agent instruction file.

## Prompt anatomy

Four parts; most tasks need only task + one more.

| Part | Job | Include when… |
|---|---|---|
| Task | the verb — what to do | always |
| Context | only what prevents a wrong guess (stack, data) | the model would otherwise guess |
| Format | shape of the answer | one line usually suffices: "as a diff", "a table" |
| Constraints | what to avoid | the default output would fail |

Add parts on evidence, never out of habit. When instructions and data mix, mark
the seam with XML-style tags (`<code>…</code>`) — the boundary becomes explicit,
and tagged data is cheaper to swap and edit.

## Technique selection

| Technique | Use for | Cost |
|---|---|---|
| Zero-shot | the default — plain instructions | minimal; start here |
| Few-shot (1–3 input→output examples) | output *shape* matters more than words | cheaper than a prose description |
| Chain-of-thought | multi-step math, logic, migration planning, debugging | high — spend only when one guess fails |
| Self-consistency | wrong answer is expensive; sample + majority vote | very high — advanced only |
| Effort/thinking setting | dial reasoning to task difficulty | as set; skip for lookups/transforms |

Test for wasted reasoning: could you nearly do the task in your head? Then no
chain-of-thought. Keep the full taxonomy and primary papers in the workspace's
`RESOURCES.md`.

## Steering coding agents

An agent runs a loop (reason → act → observe, per ReAct), so you prompt the
**goal and the rules**, not the keystrokes. Three moves:

1. **Verify loop.** Give the agent a pass/fail check (`mvn -q test must pass
   before you finish`) — self-verification is what makes "AI does the work" safe.
2. **Explore → plan → code.** Have the agent read the codebase first and present
   a plan on anything non-trivial; a plan takes seconds to review, a wrecked diff
   takes hours.
3. **Lean standing brief** (`CLAUDE.md` / `AGENTS.md`, loaded every session).
   Skeleton:

   ```
   # Project
   Stack, build tool, database, layout. One or two lines.

   # Verify
   The command that proves a change works.

   # Conventions
   Only non-obvious rules the agent would otherwise break.

   # Out of scope
   What the agent must never touch.
   ```

   Every line must survive the removal test — "write clean code" fails it.

## When teaching

The learner is a prompting beginner; teach vocabulary from zero and model short
prompts as the ideal. The four completed lessons are self-contained HTML pages —
work through them in order, then check understanding with each lesson's "Check
yourself" quiz and "Do it yourself" exercise:

1. [How a Model Reads Your Prompt](../../teach/free-i-want-to-learn-prompting/lessons/0001-how-a-model-reads-your-prompt.html)
2. [Anatomy of a Good Prompt](../../teach/free-i-want-to-learn-prompting/lessons/0002-anatomy-of-a-good-prompt.html)
3. [Pay for Reasoning When It Pays](../../teach/free-i-want-to-learn-prompting/lessons/0003-pay-for-reasoning-when-it-pays.html)
4. [Prompting Coding Agents](../../teach/free-i-want-to-learn-prompting/lessons/0004-prompting-coding-agents.html)

Quick-reference card: [core-patterns.html](../../teach/free-i-want-to-learn-prompting/reference/core-patterns.html).
Living source (drifts as models improve — a feature): Anthropic's prompting best
practices guide, linked from `RESOURCES.md`.
