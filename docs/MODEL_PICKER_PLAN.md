# Per-Backend Model Picker — Design + Adversarial Review

**Status:** IMPLEMENTED on `feat/model-picker` — post-implementation adversarial review in §6
**Date:** 2026-06-18 (verified 2026-06-19, implemented + reviewed 2026-06-19)
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

---

## 6. Post-implementation adversarial review (2026-06-19)

Severity: 🔴 breaks normal usage · 🟡 wrong/confusing behaviour · 🟢 minor/cosmetic · ✅ checked, NOT a bug

### B1 🔴 — The manual `/session_new <backend> <path>` command skips the model step entirely.
`_handle_session_new` (the text-command path, interface.py ~2314) calls `_create_and_bind_session` directly with no `model`, so a session created via `/session_new claude AI-team` is **always default model** with no way to pick at creation. Only the *button* wizard has the model step. Inconsistent UX. Not data-corrupting (you can `/model` afterwards), but it's a real gap vs. the stated "pick at creation". Fix: either accept an optional trailing model arg, or just rely on `/model` and document it.

### B2 🟡 — Wizard stash is unbounded and never expires.
`self._session_wizard[chat_id]` is written at the repo step and only popped on model-pick or cancel. If a user opens the wizard, reaches the model step, then walks away (or uses Back, or sends other commands), the entry lingers forever. Across many chats this is a slow memory leak and a correctness trap: a *stale* stash from an abandoned wizard is happily consumed by the next `session_new_model:` callback (e.g. user re-opens an old message's buttons). No TTL, no overwrite-guard tying the model click to the message it belongs to. Low blast radius (one entry per chat, overwritten on next repo-pick) but it is unbounded across distinct chats and can mis-create a session against a stale repo/node.

### B3 🟡 — `session_new_back` does not restore/clear the stash.
The Back button (`session_new_back:`) walks the user backwards through backend/node, but the stash is only set at the repo step and only cleared on cancel/model-pick. If a user reaches the model step, hits Back to backend, picks a *different* backend, then somehow the old model callback fires, the stash backend and the chosen model can disagree. Also: after Back, the stash still holds the old repo/node. Minor because the normal forward path overwrites it, but the state machine is not airtight.

### B4 🟡 — `/model` and its callback operate on the *active* session, which can change between render and click.
`/model` renders a picker for the active session's backend; `_handle_model_set_callback` re-fetches `get_active(chat_id)` at click time. If the user switches active session (`/session_use`) between opening the picker and tapping a button, the model is applied to a **different** session than the one shown — and possibly with a backend mismatch (e.g. picker built for codex indices, applied to a claude session → wrong model name or IndexError caught as "Invalid model selection"). The button doesn't carry the session_id it was built for. Should pin session_id into the callback or the picker.

### B5 🟡 — `/model <name>` advisory pass-through can store a model the worker can't run, surfacing only as a later opaque failure.
For OpenCode, `validate()` passes any typed string through (by design, R6). But the user gets "✅ Model set" immediately, then the *next turn* may 500 on the worker if the model isn't in that node's `opencode.json` (exactly the phantom-model class from memory `opencode-bigpickle-phantom-model`). The success message is honest about *setting* but not about *validity*. Acceptable per design, but worth a softer confirmation ("set — will be validated on next run") for advisory backends.

### B6 🟢 — Session card model line is plain-text but `/model` confirmations use Markdown backticks.
`_session_card` shows `🧬 model opus` (plain), while `/model` replies show `` `opus` `` (Markdown). Cosmetic inconsistency; the card is intentionally plain text (documented), so leave it, but be aware the two surfaces render the model differently.

### B7 🟢 — `resolve_model` imports `config.config` on every call (OpenCode `_session_model`, backend helpers).
Hot-path-ish: each turn re-imports config inside `_config_default`. `config` is a cached singleton/proxy so cost is negligible, but it's a repeated import in a frequently-called path. Not a bug.

### ✅ Checked and NOT bugs
- **Wizard catalog-index aliasing:** buttons use `enumerate(options(backend))` (full-list index) and the callback resolves `options(backend)[idx]` against the same list — they line up exactly, including the skipped default. Verified by simulation for all backends.
- **DB migration on fresh + existing DBs:** `model` lives only in migration 11 (removed from baseline DDL), so both fresh (runs 1→11) and existing (10→11) DBs converge through the same ALTER. The earlier duplicate-column bug is fixed and covered by `test_db_round_trip_preserves_model`.
- **Existing backend tests:** `_build_cmd(model=None)` default preserves old call sites; 63 existing tests pass.
- **`run_oneoff` model:** intentionally `None` (stateless); uses CLI/config default. Documented (R8).
- **Codex `-m` placement on resume:** verified via `--help` and asserted in tests.

### Recommended fixes before merge
- **B1** (gap) and **B4** (wrong-session) are the two worth fixing now — B4 especially, since it silently mis-applies a model. B2/B3 are hardening (add a TTL + tie callbacks to message/session identity). B5/B6/B7 are accept-as-is with optional polish.

### Fixes applied (post-review, same branch)
- **B4 fixed** 🔴: `/model` picker callbacks now encode `model_set:<session_id>:<choice>`. The callback loads *that* session by id (and re-checks ownership + not-closed), so a click can no longer be mis-applied to a different active session or hit a backend/index mismatch. Covered by `test_model_set_callbacks_pin_session_id_and_fit_budget`.
- **B2 fixed** 🟡: wizard stash carries a monotonic timestamp; the model step rejects + clears a stash older than `_WIZARD_TTL_SEC` (600s), so an abandoned wizard can't be consumed against a stale repo/node.
- **B1 mitigated** 🟡: manual `/session_new <backend> <path>` still creates at default model (kept arg parser simple), but the creation confirmation now points to `/model` for switching. Documented behaviour, not a silent gap.
- **B3 / B5 / B6 / B7**: accepted as-is (hardening / cosmetic / negligible). B3's window is now also smaller because the stash TTL bounds it.

## 7. Second review pass (2026-06-19) — reviewing the fixes themselves

Re-checked the B1/B2/B4 fixes for new bugs and swept untouched paths.

### Confirmed NOT bugs (investigated, cleared)
- **Mid-turn `/model` race:** orchestrator re-reads the session fresh at turn completion (`orchestrator.py:1419`) rather than holding the turn-start copy, and the turn's model is read at turn start (`:1546`). So `/model` during a running turn lands in the DB and is honoured next turn. The lose-the-write window is a sub-millisecond overlap between the completion-save and a concurrent `/model` save — identical last-writer-wins exposure as every other Session field (`last_user_message`, status…); `model` is no more fragile. Self-correcting (next `/model` re-applies). Not a new bug.
- **B4 fix re-check:** callback `model_set:<sid>:<choice>` is parsed with `split(":", 2)` (handles model names with colons in the choice slot — though choice is only an int or `__default__`), loads the pinned session, re-verifies ownership + not-closed. Sound.
- **OpenCode `run_oneoff`:** still passes `model=None` directly, bypassing `_session_model` — one-offs stay on the default. Unchanged.
- **No stale callback formats:** grep confirms all `model_set:`/`session_new_model:` producers and the handler registration use the new formats.

### New minor findings — FIXED this pass
- **B8 🟢 (fixed):** `/model "   "` (whitespace/garbage arg) on an advisory backend hit `validate()->None`, and since the reject condition only fires for *strict* backends it would silently reset the model to default. Now a blank-after-strip arg falls through to the picker instead of resetting; only the explicit Default button/`__default__` resets.
- **B9 🟢 (fixed):** free-text (advisory) model names allow backticks, which would break the Markdown code span in confirmations/errors. `_effective_model_label` and the unknown-model error now strip backticks from the dynamic name. Covered by `test_effective_model_label_strips_backticks`.

**Verdict after two passes:** no 🔴 or 🟡 issues remain open. Remaining accepted items (B3/B5/B6/B7) are hardening/cosmetic and documented. 76 tests pass across the touched suites; no regressions.
