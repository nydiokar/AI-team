# Unified Backend Auth (login / logout / status) — Design

**Status:** design, not yet built — 2026-10-22
**Owner surface:** `src/backends/*`, `src/core/interfaces.py`, `src/worker/agent.py`, gateway control API, Web UI
**Scope:** per-(node, backend) interactive login + logout + status, **all nodes including Horse**,
surfaced as ONE unified capability in the app. Login is the priority (an expired login today leaves
the operator with no in-app recovery); logout is included because every CLI already ships it cheaply.

---

## 1. Problem

Auth is **ambient** today. There is no login surface anywhere in the gateway:

- **Claude** — `claude-agent-sdk` spawns the local `claude` binary, which inherits an OAuth session
  from `~/.claude.json` (or `CLAUDE_CODE_OAUTH_TOKEN`). Whoever ran `claude login` once on that box
  is who we bill. No `CLAUDE_CONFIG_DIR` is set in `src/`.
- **Codex** — `codex app-server --stdio` reads `CODEX_HOME` (`/app/.codex`, a dedicated persistent
  Docker volume); auth lives in `$CODEX_HOME/auth.json`.
- **opencode-server** — authenticates via a cached session / `auth.json`.

The CLIs execute on the **worker/carrier** (`src/worker/agent.py`), not the controller
(`GATEWAY_LOCAL_EXECUTION_ENABLED=false` in the Docker split). Credentials are **node-local files**.
Nodes today: `kanebra-worker` (local Linux container) and `Horse` (remote, Windows, `claude.exe`).

**Consequence:** when a login expires, the only fix is shelling into the node and re-running a CLI by
hand. We want the operator to see per-node auth state and re-login from the UI.

---

## 2. Verified CLI auth flows (probed on this host, 2026-10-22)

| Backend | Login (headless-capable) | Completion mode | Status | Logout | Credential sink |
|---|---|---|---|---|---|
| **claude** `2.1.302` | `claude auth login` (subscription default) | **paste-code back** — browser shows a code, user pastes it into our UI → worker writes it to the subprocess stdin | `claude auth status` | `claude auth logout` | `~/.claude.json` (node-local) |
| | `claude setup-token` *(fallback)* | **token-capture** — browser approval → a 1-year `sk-ant-oat01-…` token **prints to stdout**; never auto-saved | `claude auth status` | unset env | `CLAUDE_CODE_OAUTH_TOKEN` env (transits gateway — see §8) |
| **codex** `0.157.1` | `codex login --device-auth` | **device-code (RFC 8628)** — prints a chatgpt.com URL + one-time code (15-min TTL); user enters the code *at the URL*, nothing comes back to the terminal | `codex login status` | `codex logout` | `$CODEX_HOME/auth.json` (node-local) |
| | `codex login --with-api-key` / `--with-access-token` (stdin) *(fallback)* | non-interactive | `codex login status` | `codex logout` | `$CODEX_HOME/auth.json` |
| **opencode-server** | `opencode auth login [url]` | provider-interactive | `opencode auth list` | `opencode auth logout [provider]` | `auth.json` (node-local) |

**Two completion shapes the abstraction must cover:**
- **device** (codex): show URL+code → poll for completion. **No input comes back to us.**
- **paste-code** (claude `auth login`): show URL → user authorizes → browser shows a code → user
  pastes it into our UI → we feed it to the subprocess stdin.
- **token-capture** (claude `setup-token`, fallback only): show URL → authorize → we capture a secret
  token from stdout and must store it centrally. **Carries a secret-at-rest burden — not the default.**

**Design preference:** prefer flows where the **credential is written to the node's own store and
never transits the gateway** (`codex --device-auth`, `claude auth login`, `opencode auth login`). Keep
`setup-token` / `--with-api-key` as explicit fallbacks only, behind the same interface.

**Known external prerequisites (not ours to fix, must be surfaced to the operator):**
- codex device-auth requires *"device code login"* enabled in ChatGPT security settings (beta); a
  rejected code usually means that toggle is off.
- `claude setup-token` requires a Claude Pro/Max subscription; the token never refreshes (1-year hard
  expiry) — a reason to prefer `claude auth login`.

---

## 3. The unified capability (the "one SDK-specific function, surfaced")

Extend the backend protocol in `src/core/interfaces.py` with a small, optional **`BackendAuth`**
capability. Each backend implements it with its own CLI command; everything above the backend speaks
one vocabulary.

```python
class LoginMode(str, Enum):
    device = "device"            # show url + user_code; poll for completion (codex)
    paste_code = "paste_code"    # show url; user returns a code we feed to stdin (claude auth login)
    token_capture = "token_capture"  # fallback: capture a secret from stdout (claude setup-token)

class AuthState(BaseModel):
    backend: str
    logged_in: bool
    account: str | None = None       # e.g. email / plan, if the CLI reports it (never a token)
    expires_at: datetime | None = None
    detail: str = ""                 # raw-ish status line, scrubbed of secrets

class LoginChallenge(BaseModel):
    challenge_id: str
    mode: LoginMode
    verification_url: str
    user_code: str | None = None     # present for device mode
    expires_at: datetime | None = None
    needs_code_submit: bool = False  # true for paste_code

class LoginResult(BaseModel):
    challenge_id: str
    state: Literal["pending", "succeeded", "failed", "expired"]
    detail: str = ""                 # scrubbed; NEVER a token

class BackendAuth(Protocol):
    def auth_status(self, env: Mapping[str, str]) -> AuthState: ...
    def login_start(self, env: Mapping[str, str]) -> LoginChallenge: ...
    def login_poll(self, challenge_id: str) -> LoginResult: ...
    def login_submit_code(self, challenge_id: str, code: str) -> LoginResult: ...  # paste_code only
    def logout(self, env: Mapping[str, str]) -> None: ...
```

Per-backend implementation is a thin shell over the CLI:

| Method | claude | codex | opencode-server |
|---|---|---|---|
| `auth_status` | `claude auth status` | `codex login status` | `opencode auth list` |
| `login_start` | spawn `claude auth login`, parse URL from stdout, `mode=paste_code` | spawn `codex login --device-auth`, parse URL+code, `mode=device` | spawn `opencode auth login`, parse URL, provider-specific |
| `login_submit_code` | write code to subprocess stdin | n/a (device) | provider-specific |
| `login_poll` | subprocess exited 0 and `claude auth status` logged-in? | subprocess exited 0 and `codex login status` logged-in? | `opencode auth list` shows provider |
| `logout` | `claude auth logout` | `codex logout` | `opencode auth logout <provider>` |

`env` is the backend's real run environment (notably `CODEX_HOME`, `HOME`, `CLAUDE_CODE_OAUTH_TOKEN`)
so status/login/logout operate on **exactly** the store the session driver uses — no drift.

---

## 4. Transport: control actions + a worker login-process registry

Login is **interactive, long-lived, human-in-the-loop** — it does **not** fit the existing one-shot
`claim → do → result` control task. The subprocess must stay alive after we hand the URL back and
wait (up to the code TTL, ~15 min) for the operator to authorize.

So add a **worker-side persistent login-process registry** keyed by `challenge_id`, driven by small
control actions (same claim/result channel the worker already uses for `create_session` etc.):

- `backend_auth_status` → run `auth_status`, return `AuthState`. Cheap, idempotent.
- `backend_login_start` → spawn the login subprocess, register it, return `LoginChallenge` (pending).
- `backend_login_poll` → return `LoginResult` for a live challenge; reap on terminal.
- `backend_login_submit_code` → feed `code` to the registered subprocess stdin (paste_code only).
- `backend_logout` → run `logout`; **defer until the session's in-flight turn finishes** (reuse the
  existing `close_session` deferral in `agent.py:3347`) so logout never kills a running turn.

Gateway side: an **auth-challenge record** (modeled like the existing approval/pause records) tracks
`{challenge_id, node, backend, mode, state, created_at, expires_at}` so a poll survives a page reload
and the UI is stateless.

---

## 5. End-to-end flow

```
Web UI                     Gateway API              Worker (target node)         CLI
──────                     ───────────              ────────────────────         ───
"Login codex on Horse" ──▶ POST .../login      ──▶  backend_login_start      ──▶  codex login --device-auth
                                                      capture URL+code             (subprocess stays alive)
   URL + code  ◀──────────  LoginChallenge     ◀──  LoginChallenge (pending)
   (operator opens URL on phone, enters code, authorizes)
   auto-poll   ──────────▶ GET .../login/{id}  ──▶  backend_login_poll       ──▶  (status check)
                                                      subprocess exits 0 → auth.json written
   "Logged in" ◀──────────  LoginResult succeeded
```

- **Device (codex):** no code comes back to us — the operator enters the code at the URL; we just poll.
- **Paste-code (claude):** the UI shows a second "enter code" box; its value → `POST .../login/{id}/code`
  → `backend_login_submit_code` → subprocess stdin → completion.
- **Horse:** device-auth / paste-code need **no localhost callback**, so the remote Windows node works
  through the identical path; only the worker-side process spawn differs by OS.

---

## 6. Gateway API + read-model

- `GET  /api/nodes/{node}/auth` → `[AuthState]` per active backend (drives the UI dashboard).
- `POST /api/nodes/{node}/backends/{backend}/login` → `LoginChallenge`.
- `GET  /api/nodes/{node}/backends/{backend}/login/{challenge_id}` → `LoginResult` (UI polls).
- `POST /api/nodes/{node}/backends/{backend}/login/{challenge_id}/code` → submit paste-code.
- `POST /api/nodes/{node}/backends/{backend}/logout`.

Status read-model is refreshed lazily (on panel open) and opportunistically (piggy-backed on node
heartbeat, bounded) so expiry is visible **before** it blocks work — the whole point of the feature.

---

## 7. UI surface

A per-node **Auth** panel (Nodes screen): one row per active backend showing
`logged in / expired / logged out` + account/expiry, a **Login** button, and a **Logout** button.
Login opens a modal: verification URL (click-to-open + copy), the `user_code` for device mode, an
optional "enter code" field for paste-code mode, and a live status that resolves to ✅/❌ by polling.

---

## 8. Credential storage & hygiene

- **Default (node-local):** codex device-auth, claude `auth login`, opencode `auth login` all write
  the credential to the node's own store (`auth.json` / `~/.claude.json`). **No secret transits the
  gateway.** This is the elegant, safe default.
- **Fallback (central secret):** `claude setup-token` yields a token on stdout; if used, the gateway
  must store it encrypted-at-rest and inject it as `CLAUDE_CODE_OAUTH_TOKEN` into that node's backend
  env. This is a real secret-management burden — gate it behind an explicit operator choice, do not
  make it the default.
- **Never log the credential.** Device URLs and `user_code` are safe to surface. Subprocess stdout can
  contain a token (`setup-token`) or a paste-code — **scrub before it reaches logs / telemetry / DB /
  the `detail` fields.** Collides with the repo "minimal logging" rule — enforce it in the worker shell.

---

## 9. Service-boundary checklist (§7) — new external-input handlers

- **Concurrency:** one live login per (node, backend) — a second `login_start` refuses or replaces the
  prior challenge. Guard against two logins writing the same store concurrently.
- **`CODEX_HOME` single-writer:** `codex_ownership.py` warns that mutating `CODEX_HOME` while a
  `codex app-server` is alive is unsafe. A codex login may need to **briefly pause** live codex
  sessions on that node, or at minimum serialize against the ownership lock. **Must be sized before
  build** — this is the main coupling.
- **Memory/size:** subprocess output is bounded-read; reject oversized/garbage CLI output early.
- **Timeout:** challenges expire with the code TTL (~15 min); the worker reaps the subprocess on
  expiry — **no orphaned login processes** (CLAUDE.md §0).
- **Malformed input:** a `code` submit is length/charset-validated before hitting stdin.
- **Backing-resource failure:** external prereq failures (device-code toggle off, no subscription,
  provider down) surface as a structured `LoginResult.failed` with an operator-facing reason, not a
  crash.

---

## 10. Authorization

Login **rebinds who gets billed** — the single highest-privilege operation in the system. It must be
**admin/dashboard-token gated**, and is noted against the unlanded per-node credential work (A71):
until A71 lands, the worker/dashboard token is full-admin, so a login endpoint under that model lets
any token holder rebind spend identity. Flag-gate the whole feature (default OFF) and require the
admin token.

---

## 11. Open items (resolve during build, not blockers)

1. **codex-login vs a live `app-server` on the same `CODEX_HOME`** — confirm whether `codex login`
   can safely write `auth.json` while `app-server` runs, or whether we must pause/stop it first (§9).
2. **claude `auth login` exact stdout shape** — confirm the URL line and the paste-code prompt text we
   parse against (versions drift); add a small parser with a tested fixture.
3. **Horse/Windows** — second worker-side implementation for process spawn + stdin handling; device /
   paste-code remove the networking blocker but not the OS-specific plumbing.
4. **opencode-server** — provider model is multi-provider (`auth list` can show several); decide whether
   the UI exposes per-provider rows or a single "logged in" rollup.

---

## 12. Build plan (phased, flag-gated OFF)

1. **S1 — read-model only.** `BackendAuth.auth_status` for all three + `backend_auth_status` action +
   `GET /api/nodes/{node}/auth` + a read-only UI panel. Immediately useful (see expiry), zero write
   risk. Proves the per-node, per-backend plumbing end to end.
2. **S2 — codex device-auth login** (cleanest: node-local write, no code-back) + challenge record +
   poll + UI modal. First real login.
3. **S3 — claude `auth login`** (adds paste-code submit) + **logout** for both (turn-deferred).
4. **S4 — opencode-server** + the `setup-token` / `--with-api-key` fallbacks behind the same interface.

Each phase is a small PR on `feat/<slug>`, targeted tests only, behind the feature flag.
