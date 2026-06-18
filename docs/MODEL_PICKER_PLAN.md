# Per-Backend Model Picker — Design + Adversarial Review

**Status:** READY TO IMPLEMENT — all open blockers verified (see §5)
**Date:** 2026-06-18 (verified 2026-06-19)
**Goal:** Pick a default *or* on-demand model per backend (Claude / Codex / OpenCode) through the Telegram gateway.

---

## 0. Verified facts (probed, not assumed)

| Backend | Flag | Accepted value | Source |
|---|---|---|---|
| Claude Code (`2.1.181`) | `--model <model>` | alias (`sonnet`, `opus`, `haiku`, `fable`) **or** full name (`claude-fable-5`) | `claude --help` |
| Codex (`codex-cli 0.139.0`) | `-m, --model <MODEL>` | model name (`gpt-5.5` is the live default); reasoning effort is a *separate* knob `-c model_reasoning_effort=` | `codex --help`, `~/.codex/config.toml` |
| OpenCode | `--model provider/model` (CLI) / `body.model={providerID,modelID}` (server) | `opencode/big-pickle`, `opencode/deepseek-v4-flash-free`, … | `opencode models` |

Key truth: **a "model" here is just a name on a flag.** No opaque IDs. Claude aliases auto-track the latest version, so storing the alias never goes stale. Codex effort stays in `config.toml` (decision: name-only picker). OpenCode genuinely requires the `provider/model` form — that's its CLI syntax.

---

## 1. Design

### 1.1 Model catalog — `config/models.py`
Single source of truth. Claude/Codex are tiny static name lists; OpenCode mirrors `opencode models`.

```python
@dataclass(frozen=True)
class ModelOption:
    name: str            # value passed to the flag AND shown on the button
    is_default: bool = False

BACKEND_MODELS = {
    "claude":          [sonnet*, opus, haiku, fable],
    "codex":           [gpt-5.5*, gpt-5.2-codex],
    "opencode":        [opencode/big-pickle*, opencode/deepseek-v4-flash-free, ...],
    "opencode-server": <alias of "opencode">,
}
```
Helpers: `default_model(backend)`, `is_valid(backend, name)`, `options(backend)`.
Unknown/invalid model on a session → fall back to default + log warning (guards the phantom-model HTTP 500 class of bug).

### 1.2 Config defaults
- `ClaudeConfig.default_model` ← `CLAUDE_DEFAULT_MODEL`
- new `CodexConfig.default_model` ← `CODEX_DEFAULT_MODEL`
- `OpenCodeConfig.default_model` — already exists, keep

### 1.3 Persist on the Session
- `Session.model: Optional[str] = None` (`interfaces.py`)
- round-trip in `SessionStore._to_dict/_from_dict`
- DB migration `(11, "ALTER TABLE sessions ADD COLUMN model TEXT")` + add to `sessions` DDL + `upsert_session` INSERT/UPDATE
- resolution order: `session.model` → `config.<backend>.default_model` → catalog default. `None` == "use default".

### 1.4 Backends pass it — one shared `resolve_model(session)`
- Claude `_build_cmd`: append `["--model", name]` when resolved
- Codex `_build_cmd`: append `["-m", name]` (fresh **and** `exec resume`)
- OpenCode: point `_session_model` at `session.model` (drop the `task_history` read)

### 1.5 Telegram UX
- **Wizard:** `backend → node → repo → model → create`. First button = "⚡ Default (<name>)". Callback carries the **catalog index**, not the name (64-byte limit).
- **`/model`:** show current + picker for an existing session; `/model <name>` direct. Applies on next turn.
- Show `Model:` line on session card / `/status`.

### 1.6 Mesh dispatch — **see review item R1; this is NOT free**

---

## 2. Adversarial review (issues found in the design above)

### R1 — **WRONG ASSUMPTION (critical): "model rides in the payload for free."**
The mesh payload is **not** a generic Session serialization. There are **two hand-maintained allowlists** that both silently drop unknown fields:

1. `orchestrator.py:2956` — `payload["session"]` is a hardcoded dict of 9 fields. `model` is not one of them.
2. `worker/agent.py:399` `_make_session_from_payload` — rebuilds Session from a fixed constructor + a hardcoded copy-loop (`telegram_chat_id, telegram_thread_id, owner_user_id, last_user_message`). `model` is not copied.

**Consequence:** without editing *both* spots, a remote worker silently runs the **default** model regardless of what the user picked — a silent correctness bug, not a crash. **Fix:** add `model` to both the dispatch payload dict and the worker's copy-loop. This must be in the build plan, not an afterthought.

### R2 — **WRONG ASSUMPTION: OpenCode already reads a session model that someone sets.**
`OpenCodeBackend._session_model` reads `task_history[-1]["opencode_model"]`, but **nothing in the codebase ever writes that key** (grep confirms: no producer in orchestrator/telegram). So today OpenCode *always* uses the config default. My plan framed this as "just repoint it" — fine — but the framing that it was a working feature was wrong. It's dead code. Also: `task_history[-1]` is the *last* turn's dict; reading model from it is semantically broken (model is a session property, not a per-turn artifact). Replacing with `session.model` is the correct fix, and the old read should be deleted, not kept as a "temporary fallback" (keeping it would resurrect the broken semantics).

### R3 — Codex `-m` on the resume subcommand — **RESOLVED ✅**
Verified via `codex exec resume --help` (codex-cli 0.139.0): `-m, --model <MODEL>` is listed as a valid option of the `resume` subcommand itself. So `codex exec resume <id> -m <model> --json --dangerously-bypass-approvals-and-sandbox -` parses cleanly — `-m` goes **after** `resume <id>`, alongside the other flags. No hard-failure risk.

### R4 — mid-session `/model` override honored? — **RESOLVED ✅ (favorable for all three)**
- **Claude:** `claude --help` documents `--model` as *"Model for the current session"* and lists it for use *with* `--resume`/`--continue`. It is a **per-invocation** setting, not pinned at creation — passing `--model` on a resume turn sets that turn's model.
- **OpenCode:** `opencode run --help` shows `-m/--model` and `-s/--session` as **independent flags on the same command** — both can be passed together, so model is per-invocation on resume.
- **Codex:** `-m` valid on `exec resume` (R3).

**Conclusion:** mid-session model switching works on all three backends. The `/model` "applies next turn" promise is honest; **no per-backend "new sessions only" caveat is needed.**

### R5 — **OpenCode catalog will go stale / is environment-specific.**
`opencode models` output depends on the local `opencode.json` providers (per the bigpickle memory, a project can redefine providers). A static catalog seeded from *my* machine's `opencode models` will be wrong on the Pi5/other workers, which may have different providers (e.g. `ollama-local/*`). Claude/Codex don't have this problem (global). **Implication:** the OpenCode model list is the one place a static catalog is genuinely fragile. Options: (a) accept staleness + always allow a free-text `/model <name>` escape hatch; (b) make the OpenCode list per-node. v1: accept (a), and **`is_valid` must not hard-reject** an unknown OpenCode model the user typed — only warn — because the worker's provider set is unknown to the gateway.

### R6 — **`is_valid` fallback contradicts the free-text escape hatch.**
1.1 says "invalid model → fall back to default + warn." But `/model <name>` is meant to let power users pass anything (esp. OpenCode, see R5). These conflict: silently rewriting a deliberately-typed model to the default would be infuriating. **Resolution:** picker buttons are always catalog-valid; free-text `/model <name>` is passed through **as-is with a warning** if not in the catalog, never silently replaced. Only a *stored* model that's structurally empty/garbage falls back.

### R7 — Telegram callback-data budget — **RESOLVED ✅ (confirmed a real risk → decision made)**
Measured worst case: `session_new_model:opencode-server:DESKTOP-ABCDEFG-worker-01:9:9` = **63 bytes**, one under the 64-byte hard limit. A longer node_id or wider indices overflows it. The existing repo step is already at 60 bytes.
**Decision:** do **not** pack the model index into callback_data. Stash the in-progress wizard selection (backend, node_id, repo_path) **server-side keyed by chat_id**; the model button carries only the small `modelIdx`. This also future-proofs the earlier steps. Build step 6 reflects this.

### R8 — **`run_oneoff` has no Session, so no model — is that intended?**
`run_oneoff(cwd, message)` takes no Session (all backends). It will always use the config/catalog default. That's defensible (one-offs are stateless), but the plan never stated it. **Decision to record:** one-offs use the gateway default model; not user-selectable. Fine for v1.

### R9 — Migration ordering / multi-process race — **RESOLVED ✅**
Verified: single migrator at `db.py:273` `_run_migrations`, version-gated via `schema_version`. It splits multi-statement SQL on `;` and runs each (migration 10 already does this). Adding `(11, "ALTER TABLE sessions ADD COLUMN model TEXT")` + bumping `_CURRENT_VERSION` from 10 → 11 is correct; the version gate makes it idempotent across processes. `_CURRENT_VERSION` lives at `db.py:54`.

### R10 — **`default_model` env vs catalog default: which wins, and is it validated?**
If `CLAUDE_DEFAULT_MODEL=bogus`, the plan's precedence makes *every* session use `bogus`. Env defaults should be validated through `is_valid` at config load (warn + ignore if unknown for Claude/Codex; pass-through for OpenCode per R5). Otherwise one typo in `.env` silently breaks every session on that backend.

### R11 — **Minor: `CodexConfig` doesn't exist yet.**
Codex currently has *no* config dataclass (only Claude/OpenCode do). Adding `default_model` for Codex means creating `CodexConfig` + wiring it into `Config.__init__` + env overrides. Small, but it's net-new surface the plan glossed as "add a field."

---

## 3. Revised build order (incorporating the review)

1. `config/models.py` — catalog + `default_model/is_valid/options`. `is_valid` is **advisory** (warn) for OpenCode, strict for Claude/Codex.
2. Config: `ClaudeConfig.default_model`, new `CodexConfig`, env overrides, validate env defaults through `is_valid` (R10, R11).
3. `Session.model` + `SessionStore` round-trip + DB migration 11 + bump `_CURRENT_VERSION` + `upsert_session` (R9).
4. **Mesh payload + worker reconstruction** — add `model` to `orchestrator.py` `payload["session"]` and `worker/agent.py` `_make_session_from_payload` copy-loop (R1). *Do not skip.*
5. Shared `resolve_model(session)`; wire Claude/Codex `_build_cmd` (verify `-m`/`--model` placement on resume, R3/R4) and repoint OpenCode `_session_model`, deleting the dead `task_history` read (R2).
6. Telegram: wizard model step (measure callback size; server-side stash if needed, R7) + `/model` command (free-text pass-through, R6) + card/status line. Per-backend UX copy honest about whether mid-session switch takes effect (R4).
7. Offline tests: resolution precedence, catalog validation/fallback semantics (incl. OpenCode pass-through), DB round-trip, **mesh payload round-trips `model`**. No live CLI calls (test-cost-guard).

---

## 4. Open decisions resolved (not behavioral — settled by design)
- **R1** mesh payload: add `model` to `orchestrator.py` `payload["session"]` **and** `worker/agent.py` `_make_session_from_payload` copy-loop. Non-skippable (build step 4).
- **R2** OpenCode: delete the dead `task_history["opencode_model"]` read; repoint to `session.model`.
- **R5/R6** validation: strict (reject→default) for Claude/Codex catalog; **advisory pass-through** for OpenCode and for free-text `/model <name>` (warn, never silently rewrite a user-typed model).
- **R8** one-offs: use gateway default model, not user-selectable. Documented.
- **R10/R11** env defaults validated through `is_valid`; new `CodexConfig` created.

## 5. Verification results — all blockers cleared (2026-06-19)
| Item | Question | Result |
|---|---|---|
| **R3** | `-m` valid on `codex exec resume`? | ✅ Yes — listed in `codex exec resume --help`; place `-m` after `resume <id>`. |
| **R4** | Mid-session `--model` override honored (Claude / OpenCode / Codex)? | ✅ All three — per-invocation flag, works on resume. `/model` "applies next turn" is honest; no per-backend caveat. |
| **R7** | Wizard callback fits 64 bytes with model index? | ⚠️→✅ Measured 63 bytes worst-case (overflow risk). **Decision:** server-side wizard stash keyed by chat_id; callback carries only `modelIdx`. |
| **R9** | Single migrator path, safe ALTER? | ✅ `db.py:273`, version-gated, multi-statement safe. Add migration 11, bump `_CURRENT_VERSION` (db.py:54) 10→11. |

**Spec is ready to implement.** Build order in §3 stands, with the R7 decision folded into step 6.
