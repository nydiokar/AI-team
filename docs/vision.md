Complete plan: 3 layers, duties, and setup
Layer A — Control plane (VPS preferred; Pi only if you must)

Role
Ingress, normalization, redaction, artifact storage, egress proxy to the internet for the runner VM.

Components

ingress-api (HTTP) + Telegram bot

router/parser → produce TaskSpec

artifact-api → store/retrieve workpacks/results

queue (Redis)

egress-proxy (domain allowlist; logs) — mitmproxy or Squid

Data flow

Telegram /task … → TaskSpec (project, goals, allow_paths, budgets).

Build workpack (pinned repo rev, sparse snapshot of allowed files, stubs for secrets, constraints manifest).

Push workpack to artifact store.

Enqueue job for Executor (PC VM).

Receive result.tar (diff/tests/logs) back from Executor; persist; notify.

Minimal setup (VPS, Docker Compose)

Services: api, router, redis, artifacts (MinIO or filesystem), proxy.

Proxy allowlist seed:
pypi.org, files.pythonhosted.org, npmjs.org, registry.npmjs.org, docs.python.org, nodejs.org, github.com, raw.githubusercontent.com

Proxy caps: max body 25MB, per-host rate limit, 60s connect timeout, 300s total.

Policies

Strip .env, **/*.key, config/secrets/**, .ssh/**, .git/** from workpack.

Enforce allow_paths in spec; reject if request widens scope.

Annotate each workpack with sha256 and expected base git rev.

Layer B — Isolated runner VM (on your Windows PC)

Role
Untrusted execution zone. Run Codex/Claude Code “as-is” with full freedom inside VM; force all egress through your proxy; produce a binary git diff and logs.

Topology

Windows Host: Worker daemon + SSH to VM

Ubuntu VM (Hyper-V): snapshot “golden” image; revert per task

One-time VM setup (Hyper-V)

Create Ubuntu VM (20–60 GB, 2–4 vCPU, 4–8 GB RAM).

Install: git, curl, build-essential, python3, pipx, nodejs/npm as needed by your stacks.

Add CLOUD_AGENT_HOME (where you’ll install Claude/Codex CLIs).

Configure system-wide proxy env (but default off):

in /etc/environment keep placeholders https_proxy, http_proxy commented.

Create two directories: /runner/in and /runner/out.

Create snapshot “golden”.

Per-task host script (PowerShell, outline)

# Inputs: $TaskId, $WorkpackUrl, $VM = 'DevRunner'
$in = "/runner/in/$TaskId"
$out = "/runner/out/$TaskId"

# 1) Ensure clean VM
Checkpoint-VM -VMName $VM -SnapshotName "golden" | Out-Null
Restore-VMSnapshot -VMName $VM -Name "golden" -Confirm:$false

# 2) Fetch workpack to host temp and scp into VM
Invoke-WebRequest $WorkpackUrl -OutFile "$env:TEMP\$TaskId.tar.gz"
scp "$env:TEMP\$TaskId.tar.gz" ubuntu@vm-ip:$in.tar.gz

# 3) Run inside VM (via SSH)
$cmd = @"
set -euo pipefail
mkdir -p $in $out /project
tar -C $in -xzf $in.tar.gz

# Proxy ON for the run (force via control-plane proxy)
export https_proxy="http://PROXY_IP:PROXY_PORT"
export http_proxy="http://PROXY_IP:PROXY_PORT"
export NO_PROXY="localhost,127.0.0.1"

# Unpack workpack to /project (sparse snapshot provided)
tar -C /project -xzf $in/workpack.tgz

# Execute cloud agent CLI with its own sandbox/settings as needed
# Examples (illustrative):
# claude task --project /project --spec $in/spec.json --max-time 1800
# OR codex run --cwd /project --spec $in/spec.json

# After agent finishes, capture changes
cd /project
git config user.email ci@local && git config user.name CI
git add -A
git diff --staged --binary > $out/changes.patch
git status --porcelain=v1 > $out/changed_files.txt

# Collect logs/artifacts the agent wrote (normalize to $out)
# e.g., mv /project/.agent_logs $out/logs || true
"@
ssh ubuntu@vm-ip "$cmd"

# 4) Copy results back
scp ubuntu@vm-ip:$out/changes.patch .
scp ubuntu@vm-ip:$out/changed_files.txt .
# also copy any $out/logs/*.log if present


VM guardrails

No host mounts; only scp in/out.

All outbound via proxy env vars; unset proxy after run.

Wall-clock kill: use timeout (Linux) or Hyper-V job timeout from host.

CPU/mem caps: set VM size; rely on Hyper-V limits.

Snapshot revert after each job.

Inside-VM agent call

Run the official Claude Code/Codex CLI exactly as documented.

Don’t disable their security; add your proxy on top. Their sandbox is extra defense.

Layer C — Apply-gate (on PC, outside the runner VM)

Role
Re-create a clean repo context, apply the patch, test, and only then persist.

Flow

Verify provenance: match workpack.sha256, ensure patch touches only allow_paths.

Safety scans: deny if changes include .env, keys, .git, config/secrets/**.

“CI in a box”:

git checkout -B cloud-task <base_rev>

git apply --index --3way --whitespace=fix changes.patch

ruff/black/mypy or equivalents

pytest -q (unit) and selected integration/smoke

optional: gitleaks/trufflehog on the diff

Success → git commit -m "cloud:$TaskId" → push or open PR (per flag).

Failure → emit logs + summary; do not touch main.

Windows-friendly CI shell (run in WSL/second VM if needed)

set -euo pipefail
REPO="$HOME/repos/Higgs"
PATCH="$PWD/changes.patch"
BASE_REV="$(cat workpack_meta.json | jq -r .base_rev)"

cd "$REPO"
git fetch --all
git checkout -B cloud-task "$BASE_REV"
git apply --index --3way --whitespace=fix "$PATCH"

# formatting + lint
ruff --select I --fix .
black --quiet .
ruff .

# tests
pytest -q

# security (cheap wins)
gitleaks protect --staged || { echo "Secrets found"; exit 1; }

git commit -m "cloud:$TASK_ID apply"
# choose one:
# git push origin cloud-task
# or merge locally per policy:
# git checkout main && git merge --no-ff cloud-task


Policy flags (env)

AUTO_APPLY={off|pr|on}

WRITE_SCOPE=src/**,tests/**

MAX_PATCH_SIZE_KB=200

MAX_FILE_TOUCH=50

NET_ALLOW=comma,separated,hosts (forwarded to proxy)

COST_BUDGET_TOKENS=… (monitor; stop at cap)

Cross-layer artifacts and telemetry

Artifacts per task

workpack.tgz + spec.json + sha256

changes.patch, changed_files.txt, agent_logs/*, ci_stdout.log, ci_stderr.log

run.json (model id, proxy log id, VM image digest, durations, token usage)

Events

queued | started | agent_finished | patch_validated | applied | failed

Include absolute timestamps and durations.

Failure modes and hard brakes

Agent hangs → host kills VM after TASK_TIMEOUT and collects partial logs.

Patch touches denylist → reject immediately.

Tests fail → reject; attach failing test output.

Proxy denies domain → surface domain in summary; extend allowlist only intentionally.

Minimal bring-up order

Control plane (VPS)

Deploy api, router, redis, artifacts, proxy.

Confirm /workpack POST/GET works; proxy logs requests.

Runner VM (PC)

Create Hyper-V Ubuntu VM; install CLIs; snapshot “golden”.

Test one dry run: unpack tiny workpack, write a dummy file, produce changes.patch.

Apply-gate

Run the CI script on a throwaway repo; verify reject/accept paths.

Telegram glue

/task → workpack → VM run → patch back → CI → summary to Telegram.

What each layer guarantees

Control plane: never ships secrets; constrains scope; logs egress; is replaceable.

Runner VM: all untrusted work; no host writes; network through my proxy only; clean slate per job.

Apply-gate: only diffs that pass my checks persist; everything else is a logged artifact.