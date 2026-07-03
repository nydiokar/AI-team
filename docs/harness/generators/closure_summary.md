# CLOSE — closure summary generator

**Role:** Manager (spec §4 / §14 step 7). Runs after execution + checkpoint review.
Produces the record of what changed and what follows, and the doc-update stub.

**Input:** the finished work + the milestone file + the review's F-tags.
**Output:** a closure summary + a `.ai/CONTEXT.md` / `.ai/dispatch/DISPATCH_LOG.md`
update stub. The Level-3 wiki is **optional and never a gate**.

---

## Prompt

> You are closing a task. Write an honest summary — if tests failed, say so with
> the output; if a step was skipped, say that. State what is done and verified
> plainly, without hedging.
>
> Produce, in this shape (the house Implementation-log style from
> `.ai/dispatch/AGENT_8_OPERATOR_SIGNAL.md`):
>
> ```
> ### <TASK> — <SHIPPED | PARTIAL | BLOCKED> (<date>)
>
> **What changed:** per-file, one line each — path → what it does now.
> **Verification:** the exact non-paid commands run + their results
>                   (targeted pytest / tsc -b / curl /health / --collect-only).
> **F-tag outcomes:** each [Fn] → fixed | accepted | no change needed.
> **What follows:** the next task(s) / open items / operator follow-ups (not code).
> ```
>
> Then produce the **doc-update stub**:
> - `.ai/CONTEXT.md` — a one-line Shipped-Ledger entry (or a Priorities-table
>   status change).
> - `.ai/dispatch/DISPATCH_LOG.md` — move this dispatch's row to
>   `built` / `reviewed` / `merged` and shrink "What's left".
>
> Set the milestone file `Current Status: closed`.

---

## Guardrails

- **No parser, no gate (F6).** Markdown is source of truth. The Level-3 wiki
  (HTML/Mermaid/decision table) is optional and **never blocks closure**; do not
  automate it in v1.
- **Honesty over polish.** A partial result reported honestly is worth more than a
  "done" that hid a skipped step. This is the recorded false-success scar
  (`false-success-intent-only`) — closure is where it gets caught.
- **File-memory, if anything durable was learned.** Write a `<memory_entry>`-shaped
  fact to `MEMORY.md` + `memory/*.md` (spec §7) — a write format, not a new store.
- **Note `continues:` for a follow-up.** If the next task should resume this one's
  context, tell the drafter to set `continues: <this_task_id>` in its frontmatter
  (spec §7/§14) — the runtime injects the prior context opt-in.
