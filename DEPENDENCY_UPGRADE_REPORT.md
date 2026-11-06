# Dependency upgrade report

Audit date: 2026-11-06 (Europe/Vilnius). Baseline: `4dff0d2`. ARM64 Linux, Python 3.11.2.

## Dependency decisions

Versions came from independent `pip list --outdated` and PyPI version metadata checks. “After” is the selected pin, not the newest candidate. TEST FIRST means deferred until the named additional coverage exists.

| Dependency | Before | After | Decision | Reason |
|---|---|---|---|---|
| [aiohttp](https://docs.aiohttp.org/en/stable/changes.html) | 3.14.3 | 3.14.3 | TEST FIRST | Candidate 3.14.5. Push-only transitive client; 3.14.4 changes parsing/retry behavior. Add real push-client error/timeout coverage first. |
| [anyio](https://anyio.readthedocs.io/en/stable/versionhistory.html) | 4.15.1 | 4.15.1 | TEST FIRST | Candidate 4.16.0. 4.16 changes cancellation and socket-close semantics used by SDK/ASGI; keep independent from SDK cleanup diagnosis. |
| [ast_serialize](https://github.com/mypyc/ast_serialize/releases) | 0.11.2 | 0.11.2 | DEFER | Candidate 0.13.1. Mypy implementation dependency; official release page has no candidate notes. Keep toolchain together. |
| [black](https://github.com/psf/black/blob/main/CHANGES.md) | 26.5.1 | 26.5.1 | DEFER | Candidate 26.10.1. Formatting changes unnecessary; security fix is in bundled GitHub Action, which this repo does not use. |
| [certifi](https://github.com/certifi/python-certifi/releases) | 2026.7.22 | 2026.7.22 | DEFER | Candidate 2026.10.23. CA bundle refresh; official release page has no candidate notes. Requires trust-store diff and downstream TLS checks. |
| [charset-normalizer](https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md) | 3.5.1 | 3.5.1 | DEFER | Candidate 3.5.2. Requests transitive; UTF-8 Chinese detection fix has no demonstrated impact on current push boundary. |
| [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python/releases) | 0.2.110 | 0.2.111 | UPDATE | Candidate 0.2.178. Use intermediate 0.2.111: reproduced AnyIO-scope child leak fixed upstream. Defer 0.2.178 and its MCP transport rewrite pending real protocol coverage. |
| [cryptography](https://cryptography.io/en/latest/changelog/) | 50.0.1 | 50.0.1 | TEST FIRST | Candidate 50.0.2. 50.0.2 changes bundled OpenSSL 4.0.2 to 4.0.3; test actual VAPID signing/encryption first. |
| [execnet](https://execnet.readthedocs.io/en/latest/changelog.html) | 2.1.1 | 2.1.1 | DEFER | Candidate 2.1.2. 2.1.2 artifact-build fix; current xdist workers pass. |
| [fastapi](https://fastapi.tiangolo.com/release-notes/) | 0.141.1 | 0.141.1 | TEST FIRST | Candidate 0.162.0. 0.160 parameter-model extra handling and 0.162 omitted form booleans change validation; needs auth/form/OpenAPI matrix. |
| [httptools](https://github.com/MagicStack/httptools/releases) | 0.8.0 | 0.8.0 | TEST FIRST | Candidate 0.9.0. Updates llhttp parser to 9.4.3; requires real malformed-HTTP/keepalive regression coverage. |
| [iniconfig](https://github.com/pytest-dev/iniconfig/releases) | 2.3.0 | 2.3.0 | DEFER | Candidate 2.3.1. BOM and parsing-allocation fixes; no failing project INI contract. |
| [librt](https://github.com/mypyc/librt/releases) | 0.15.0 | 0.15.0 | DEFER | Candidate 0.16.0. Mypyc toolchain dependency; candidate notes absent from official release page. |
| [mcp](https://github.com/modelcontextprotocol/python-sdk/releases) | 1.28.1 | 1.28.1 | TEST FIRST | Candidate 2.3.0. 2.3 requires httpx2 and mcp-types; selected SDK requires mcp<2.0. Major migration blocked until SDK/protocol work. |
| [multidict](https://multidict.aio-libs.org/en/latest/changes/) | 6.9.0 | 6.9.0 | TEST FIRST | Candidate 7.2.1. Major upgrade; proxy initialization and native implementation changes. Pair with aiohttp contract tests. |
| [mypy](https://github.com/python/mypy/blob/master/CHANGELOG.md) | 2.3.1 | 2.3.1 | DEFER | Candidate 2.4.1. Existing 898 errors; keep diagnostic baseline stable. Candidate absent from fetched changelog. |
| [mypy_extensions](https://github.com/python/mypy_extensions/releases) | 1.1.0 | 1.1.0 | DEFER | Candidate 1.2.0. No candidate notes on official releases page; keep with mypy. |
| [pandas](https://pandas.pydata.org/docs/whatsnew/index.html) | 3.0.6 | 3.0.6 | DEFER | Candidate 3.1.1. Optional dispatch Parquet rendering works; no demonstrated benefit worth a data-stack migration. |
| [pip](https://pip.pypa.io/en/stable/news/) | 26.2.1 | 26.2.1 | DEFER | Candidate 26.3. Environment tool, not a project pin; no live/environment-tool upgrade authorized. |
| [platformdirs](https://platformdirs.readthedocs.io/en/latest/changelog.html) | 4.11.10 | 4.11.10 | DEFER | Candidate 4.14.5. Build/formatter transitive; XDG/runtime fallback changes do not address an observed project defect. |
| [pluggy](https://pluggy.readthedocs.io/en/latest/changelog.html) | 1.6.0 | 1.6.0 | DEFER | Candidate 1.7.0. Changes hook defaults/precedence and deprecations; preserve pytest execution baseline. |
| [py-spy](https://github.com/benfred/py-spy/releases) | 0.4.2 | 0.4.2 | DEFER | Candidate 0.4.3. Unmanaged host profiler, absent from manifests; leave operator tooling untouched. |
| [py-vapid](https://github.com/mozilla-services/vapid/releases) | 1.9.4 | 1.9.4 | TEST FIRST | Candidate 1.9.6. Optional push signing dependency; candidate not in fetched release listing. Verify authoritative tag and signing round trip first. |
| [pyarrow](https://arrow.apache.org/release/26.0.0.html) | 25.0.1 | 25.0.1 | DEFER | Candidate 26.0.1. Optional Parquet engine loaded indirectly by pandas; major Arrow migration unnecessary for this task. |
| [pycparser](https://github.com/eliben/pycparser/releases) | 3.0 | 3.0 | DEFER | Candidate 3.12. CFFI transitive; newer C syntax/code generation fixes not exercised by project code. |
| [pydantic](https://pydantic.dev/docs/validation/latest/get-started/changelog/) | 2.13.5 | 2.13.5 | TEST FIRST | Candidate 2.14.3. 2.14 changes enum construction, config propagation and JSON serialization; API/persistence round-trip matrix needed. |
| [pydantic_core](https://docs.pydantic.dev/latest/changelog/) | 2.46.5 | 2.46.5 | DEFER | Candidate 2.50.4. Must move with Pydantic, which requires exact core version; never upgrade independently. |
| [pydantic-settings](https://github.com/pydantic/pydantic-settings/releases) | 2.15.0 | 2.15.0 | DEFER | Candidate 2.16.2. MCP transitive; project config uses dataclasses plus dotenv. No app-level Settings migration benefit established. |
| [PyJWT](https://github.com/jpadilla/pyjwt/releases) | 2.14.0 | 2.14.0 | TEST FIRST | Candidate 2.15.1. 2.15 changes nested-token errors and JWKS cache behavior; no direct app JWT use, MCP/push boundary needs tests. |
| [pytest](https://docs.pytest.org/en/stable/changelog.html) | 9.1.1 | 9.1.1 | DEFER | Candidate 9.2.0. Test runner changes would confound runtime regression comparison; separate maintenance batch. |
| [pytest-asyncio](https://pytest-asyncio.readthedocs.io/en/stable/reference/changelog.html) | 1.4.0 | 1.4.0 | DEFER | Candidate 1.4.1.post1. Candidate loosens pytest upper bound and fixes fixture typing; neither is needed with pinned pytest. |
| [python-dateutil](https://github.com/dateutil/dateutil/releases) | 2.9.0.post0 | 2.9.0.post0 | DEFER | Candidate 4.0.0. 4.0 requires 3.11 and tightens isoparse numeric fields; pandas transitive, no direct dateutil usage or demonstrated benefit. |
| [python-dotenv](https://github.com/theskumar/python-dotenv/releases) | 1.0.1 | 1.2.4 | UPDATE | Candidate 1.2.4. Fix reproduced empty-value inline-comment parsing; preserve explicit-file precedence/interpolation. |
| [python-multipart](https://github.com/Kludex/python-multipart/blob/master/CHANGELOG.md) | 0.0.32 | 0.0.32 | TEST FIRST | Candidate 0.0.33. 0.0.33 changes content type/transfer decoding and limits; needs upload/form corpus first. |
| [rpds-py](https://github.com/crate-py/rpds/releases) | 2026.6.3 | 2026.6.3 | DEFER | Candidate 2026.9.1. JSON Schema transitive Rust container; no demonstrated project bug; keep schema stack stable. |
| [ruff](https://github.com/astral-sh/ruff/blob/main/CHANGELOG.md) | 0.16.8 | 0.16.8 | DEFER | Candidate 0.17.4. New rule behavior; existing 5891 diagnostics. Keep linter version stable to compare changes. |
| [setuptools](https://setuptools.pypa.io/en/stable/history.html) | 66.1.1 | 66.1.1 | DEFER | Candidate 84.2.1. Build requirement already >=68 with isolated builds; installed 66.1.1 is host tooling. No runtime pin to change. |
| [sse-starlette](https://github.com/sysid/sse-starlette/releases) | 3.4.11 | 3.4.11 | TEST FIRST | Candidate 3.6.0. MCP transitive; 3.5.1 closes interrupted generators, 3.6 changes HEAD behavior. Gateway uses StreamingResponse. |
| [starlette](https://starlette.dev/release-notes/) | 1.6.0 | 1.6.0 | TEST FIRST | Candidate 1.10.0. 1.9 changes TestClient streaming/cancellation; need real ASGI disconnect/reconnect checks, not generator-only tests. |
| [typing-inspection](https://github.com/pydantic/typing-inspection/blob/main/HISTORY.md) | 0.4.4 | 0.4.4 | DEFER | Candidate 0.4.5. Literal alias/performance fixes; no demonstrated project defect, keep validation stack together. |
| [uvicorn](https://uvicorn.dev/release-notes) | 0.53.0 | 0.53.0 | DEFER | Candidate 0.54.0. 0.54 adds experimental HTTP/2 trailers/early hints; project uses current HTTP/1 behavior. |
| [uvloop](https://github.com/MagicStack/uvloop/releases) | 0.22.1 | 0.22.1 | TEST FIRST | Candidate 0.23.0. Native libuv update and eager-task scheduling; verify worker loop/process shutdown under uvloop first. |
| [watchdog](https://github.com/gorakhargosh/watchdog/blob/master/changelog.rst) | 3.0.0 | 7.1.0 | UPDATE | Candidate 7.1.0. Linux polling/descriptor/thread cleanup and lazy inotify reliability fixes; real inotify contract now covered. No PatternMatchingEventHandler usage. |
| [watchfiles](https://github.com/samuelcolvin/watchfiles/releases) | 1.2.0 | 1.2.0 | DEFER | Candidate 1.3.0. Uvicorn reload extra, not application watcher; abi3 wheel/build changes do not justify runtime movement. |
| [websockets](https://websockets.readthedocs.io/en/stable/project/changelog.html) | 17.1 | 17.4 | UPDATE | Candidate 17.4. 17.4 fragment-count memory-exhaustion fix; real loopback test reproduces missing bound at baseline. |

No REMOVE decision: absence of direct imports does not establish that a transitive package is unused. `mcp` comes from the SDK; `sse-starlette`, `pydantic-settings` and PyJWT from MCP; `aiohttp` from pywebpush; dateutil from pandas; websockets/watchfiles/uvloop/httptools from Uvicorn standard. PyArrow supplies pandas’ Parquet engine in `scripts/dispatch/dispatch_state.py`. Toolchain-only packages remain pinned.

The direct runtime declarations are watchdog, Pydantic, python-dotenv, PyYAML, FastAPI, Uvicorn standard, python-multipart, Claude SDK, AnyIO and psutil. Optional groups remain dev/test, llama, telegram, push and dispatch. The unused optional llama group was not installed or removed. No evidence establishes it is safe to delete.

## Workflow and baseline

`pyproject.toml` declares ranges (exact watchdog/dotenv); `constraints.txt` pins the runtime/dev/optional closure. CI installs `.[dev,test,telegram]`; Docker extracts runtime + push dependencies and uses the same constraints. This is a constraints workflow, not a hash-locked resolver workflow. Only selected pins and matching exact declarations change. All other constraints stay byte-for-byte intact. Build-system setuptools/wheel remain unpinned, as before.

Running gateway health was read-only and healthy (`bacf90f`); checkout was `4dff0d2`. Gateway/task-server run in separate Compose boundaries; workers are separate host/carrier processes with active sessions. No service command, restart, deployment, worker call, direct live DB access, credentialed agent invocation, live venv installation or system package change was performed. Unrelated untracked files/directories were preserved.

Baseline installed environment: `pip check` clean; original 15-file regression selection: **331 passed, 8 warnings**. Clean isolated installation of `.[dev,test,telegram,push,dispatch]` also succeeds; same selection with the two newly added dotenv tests gives **332 passed, 1 expected failure, 8 warnings**. The expected failure is the deliberately reproduced empty-token bug.

New contract baseline (12 cases): **9 passed, 3 failed**. Failures: `test_sdk_transport_reaps_child_in_cancelled_scope`, `test_websocket_fragment_limit[4097]`, `test_explicit_env_file_empty_token_with_comment`. Each is reproduced before its upgrade. Watcher delivery/filtering/shutdown and quoted/interpolated dotenv values pass before upgrading.

Pre-existing quality failures: Ruff **5891 errors**; default `mypy src config` exits with `AI-team contains __init__.py but is not a valid Python package name`. `mypy --explicit-package-bases src config` reports **898 errors in 42 files (110 checked)**. No blanket fixes, ignores or error suppression added. A broader 11-file baseline adds **136 passed, 1 pre-existing failure, 8 warnings**: `test_every_api_route_requires_auth_except_allowlist` sees built SPA routes `/` and `/{full_path:path}` that its allowlist omits. Test and API source were unchanged; open PR #281 independently addresses this asset-sensitive assertion. It is retained and reported, not weakened. Baseline warnings include deprecated httpx TestClient and AnyIO BlockingPortal aliases. Initial clean installation also warns about the existing http-ece legacy setup.py path under bootstrap pip 23.0.1.

## Behavioral contracts

| Previous contract | Upstream change | Potential regression | Verification |
|---|---|---|---|
| Explicit dotenv file overrides stale managed values; quoted values/interpolation survive | [1.2.4 parsing fixes](https://github.com/theskumar/python-dotenv/blob/main/CHANGELOG.md) | Empty token becomes comment text; precedence changes | Real subprocess config imports, old and new tests |
| Non-recursive task watcher delivers create/modify/atomic move/existing files; stops threads | [3→7.1 observer/inotify changes](https://github.com/gorakhargosh/watchdog/blob/master/changelog.rst) | Missed tasks, broken schedule signature or leaked threads | Real temporary-directory inotify observer; bounded callback wait; captured emitters checked after stop |
| SDK close cleans up transport child even during cancellation | [0.2.111 shielded cleanup](https://github.com/anthropics/claude-agent-sdk-python/releases/tag/v0.2.111) | Orphaned child; changed cleanup timing | Actual SDK close with harmless Python child inside a cancelled AnyIO scope; unconditional test cleanup |
| Normal fragmented WebSocket messages round-trip | [17.4 security limit](https://websockets.readthedocs.io/en/stable/project/changelog.html) | Valid fragments rejected, or hostile fragments retain unbounded objects | Actual loopback server/client: 3 fragments succeed; 4097 close with 1009 |
| Claude create/resume, options/tool grants, streaming and ownership | SDK parser/stderr changes; app retains its private stdout-resync patch | Lost whitespace, changed messages or dead continuation | Existing Claude driver/ownership/sender tests; no real Claude CLI |
| Codex subprocess/carrier behavior | No Codex or asyncio-loop dependency upgrade | Lifecycle/recovery affected indirectly | Existing Codex app-server and carrier tests using controlled boundaries |
| MCP discovery/invocation | MCP remains 1.28.1; SDK MCP transport rewrite not selected | Tool schema/routing drift | Existing jobs/manager/sender tests; no claim of real external MCP transport acceptance |
| Durable queue, retries, recovery; HTTP validation and SSE frames | API/validation stack unchanged | Indirect SDK/runtime effects | Queue/retry/carrier-recovery/worker/API-write/SSE regression selection |

The watcher initially revealed an existing duplicate-callback race within the first event burst. The test explicitly preserves this observed behavior and checks suppression after settling; no exactly-once initial-delivery claim is made. No production watcher fix is included. Metadata-only filesystem changes become separate attribute events in watchdog 7; AI-team reads file content, uses FileSystemEventHandler rather than pattern handlers, and passes `recursive=False` by keyword. Python minimum 3.11 is preserved.

## Regression evidence

| Area | Baseline | Final | New coverage | Verdict |
|---|---|---|---|---|
| Core Claude/Codex/MCP/API/SSE/queue/recovery/worker selection | 331 passed | 333 passed (includes 2 config additions) | Quoted/interpolated values; empty commented token | No new failure |
| Linux file watcher | 4 passed | 4 passed | Actual inotify create/modify/rename/existing delivery and thread cleanup | PASS |
| SDK cancelled cleanup | 1 reproduced failure | 1 passed | Real harmless child, actual SDK transport teardown | PASS within AnyIO-scope boundary |
| WebSocket fragmentation | 1 passed, 1 reproduced failure | 2 passed | Actual loopback normal/4097-fragment messages and close code | PASS |
| Gateway/task-server ASGI startup | 2 passed | 2 passed | Application construction, task-server real lifespan, health and auth rejection, shutdown | PASS in isolation |
| Broader integration selection | 136 passed, 1 failure | 136 passed, same failure | Existing tests retained unchanged | Pre-existing SPA/auth audit assertion |
| Combined final 30-file selection | Original 468 cases: 467 passed, 1 failure | **478 passed, 1 failure, 8 warnings** | **11 added cases** | No newly introduced test failure |
| Dependency integrity / clean install | Clean | Clean | Fresh final environment; all declared extras except llama | PASS |
| Ruff, whole Python tree | 5891 errors | 5891 errors; output byte-identical | Four new test modules lint clean | Pre-existing failures |
| Mypy, explicit package bases | 898 errors / 42 files | 898 errors / 42 files; diagnostic multiset identical | Four new test modules separately type-check clean | Pre-existing failures |

Batch results: dotenv **103 passed**; watchdog **4 passed**; SDK **166 passed, 8 warnings**; websockets **2 passed**. Every batch installed through the existing editable-install + constraints workflow and passed `pip check`. The final environment was created from scratch after all batches. Distribution inventory comparison confirms exactly four version changes and no removed runtime/dev distributions.

Changed files: `constraints.txt`, `pyproject.toml`, `tests/test_settings_env_file.py`, new `tests/test_dependency_{watcher,sdk,websockets,startup}.py`, and this report. No application source, DB schema, input handler, deployment configuration or service-boundary policy changed. The incidental generated dispatch-index change from the commit hook was restored to baseline; later commits skipped only that dispatch-render hook.

Final selected versions: **python-dotenv 1.2.4, watchdog 7.1.0, claude-agent-sdk 0.2.111, websockets 17.4**. All other application/development pins remain in `constraints.txt`. No freeze of host-only tools was used to regenerate constraints.

### Reproduction commands

Run from the repository root. Evidence logs for this session are under `/tmp/ai-team-dependency-audit/` (temporary, not committed). `baseline` was installed before pin edits, `candidate` received sequential batches, and `final` is the fresh final install. To reproduce the old environment, use the manifests at `4dff0d2` in a separate checkout with the added regression tests.

```bash
pwd
python3 -m venv /tmp/ai-team-dependency-audit/final
PIP_CONFIG_FILE=/dev/null PIP_EXTRA_INDEX_URL='' /tmp/ai-team-dependency-audit/final/bin/python -m pip install -e '.[dev,test,telegram,push,dispatch]' -c constraints.txt
/tmp/ai-team-dependency-audit/final/bin/python -m pip check
/tmp/ai-team-dependency-audit/final/bin/python -m pip list --outdated --format=json
/tmp/ai-team-dependency-audit/final/bin/pytest --tb=short tests/test_settings_env_file.py tests/test_claude_driver.py tests/test_claude_driver_manager_tools.py tests/test_turn_queue_sdk_ownership.py tests/test_turn_queue_sender.py tests/test_codex_app_server.py tests/test_codex_managed_carrier_integration.py tests/test_mcp_jobs.py tests/test_mcp_manager.py tests/test_control_api_stream.py tests/test_control_api_write.py tests/test_queue_persistence.py tests/test_retry_transient.py tests/test_turn_queue_carrier_recovery.py tests/test_worker_managed_turns_default.py tests/test_dependency_watcher.py tests/test_dependency_sdk.py tests/test_dependency_websockets.py tests/test_dependency_startup.py tests/test_claude_usage_control.py tests/test_claude_auth_session.py tests/test_claude_session_backend.py tests/test_sdk_driver_proactive.py tests/test_codex_native.py tests/test_codex_ownership.py tests/test_a102_carrier_contract.py tests/test_turn_queue_worker.py tests/test_task_server_client.py tests/test_task_server_upload_safety.py tests/test_control_api_auth_coverage.py -n 4 --junitxml=/tmp/ai-team-dependency-audit/final-tests.xml
/tmp/ai-team-dependency-audit/final/bin/ruff check src config scripts tests --output-format concise
/tmp/ai-team-dependency-audit/final/bin/mypy --explicit-package-bases src config
/tmp/ai-team-dependency-audit/final/bin/mypy --explicit-package-bases --follow-imports=silent tests/test_dependency_watcher.py tests/test_dependency_sdk.py tests/test_dependency_websockets.py tests/test_dependency_startup.py
```

The original baseline used `.venv/bin/pytest --tb=short` with the first 15 files from the long command, before adding tests. The separate broader baseline used the last 11 files. New tests were run before their upgrades in the baseline environment. Logs: `baseline-tests.txt`, `baseline-isolated-tests.txt`, `baseline-broader.txt`, `baseline-final-contracts.txt`, `baseline-startup.txt`, `batch{1,2,3,4}-{install,tests}.txt`, `final-install.txt`, `final-tests.{txt,xml}`, `final-contracts.txt`, `clean-final-{ruff,mypy}.txt`, `new-tests-mypy-final.txt`. Final combined summary: `1 failed, 478 passed, 8 warnings in 70.36s`. After final test-only type annotations, focused rerun: `14 passed, 2 warnings in 13.55s`; the same final 14 cases on old dependencies reproduce 3 failures (11 pass).

Rechecked outdated packages in the final dependency closure: **41 remain**, deliberately retained per the table (including SDK 0.2.111 versus 0.2.178). The initial host had 45: three selected packages reach latest and host-only py-spy is absent from isolated installs. The host environment remains unchanged, including py-spy, pip and setuptools.

## Outstanding risks

- Full-suite/e2e verification is not claimed: project hard rules and `.claude/skills/running-targeted-tests/SKILL.md` prohibit it because tests can reach paid CLIs. Only explicit relevant targets run. No credentials or paid integrations were used.
- Claude 0.2.111 bundles CLI 2.1.202 (was 2.1.191). Explicit external CLI paths still follow existing configuration. Actual bundled/external CLI session creation, continuation, tool execution and long-turn cleanup remain unverified; offline tests cannot certify production.
- The cancellation regression covers AnyIO cancellation scopes. Upstream documents that raw asyncio task cancellation can still interrupt shielded cleanup; this is not claimed fixed. Later SDK resume/session argv hardening (0.2.121), skill allowlist hardening (0.2.129) and MCP transport rewrite (0.2.140) remain outside the selected intermediate batch.
- WebSocket tests exercise the library asyncio recv path, not Uvicorn’s independent SansIO frame accumulation. No application WebSocket routes were found. No claim is made that upgrading websockets alone hardens every Uvicorn path.
- SSE tests cover authentication and frame generation, not a real TCP disconnect/reconnect. Terminal streaming/reconnect acceptance, MCP cancellation, paid agents, Telegram and push delivery require separately controlled integration verification.
- Watchdog kernel overflow/resource-exhaustion recovery and long-duration observer behavior are not fault-injected. Initial-burst duplicate delivery is pre-existing.
- Existing Ruff/mypy failures and deprecation warnings remain. Optional llama/Ollama is not installed in the audited environments. No vulnerability scanner was run; release-note review is not a complete security audit.
- Candidate metadata provides Python-compatible pure-Python or ARM64 wheels for the outdated packages; only selected upgrades are installed/tested. Availability is not proof of runtime compatibility.

## Final verdict

**PARTIAL** — four justified upgrades implemented and verified in clean isolation; all selected upgrade regressions pass. No known new failures in the tested scope. Major/insufficiently covered upgrades are deferred, one pre-existing broader test failure and existing lint/type failures remain, and production compatibility is unproven. No merge, deployment or restart performed.
