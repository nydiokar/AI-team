#!/usr/bin/env python
"""Dispatch state manager — honest, folder-is-the-registry job tracking.

This is a MANAGEMENT tool, not research. It imports nothing from research_os/.
The source of truth is the .ai/dispatch/ folder itself: every *.md dispatch
brief carries a small ```yaml state block at its top, and this script scans
them into two GENERATED views:

  _DISPATCH_STATE.md   — human-eyeball table (git-diffable)
  _dispatch.parquet    — machine-queryable table

State that an agent hand-edits lives in ONE place: the yaml block at the top of
each job's OWN file. Two agents on two jobs edit two files → no shared-table
contention. `created_at` is CANONICAL (written once at dispatch, never derived
from git). Only `updated_at` refreshes on edits.

Because the folder IS the registry, gaps are structurally detectable:
  - a *.md with no yaml block          → MISSING_STATE
  - status=done with no evidence or an evidence path that doesn't exist → CLAIMED_DONE_NO_PROOF
  - explicitly auto-unblockable job whose dependencies are done → READY_TO_UNBLOCK
  - a row in the parquet with no file   → shows up as a removed row in git diff

Commands:
  --new ID    create a new dispatch job stub (canonical created_at stamped NOW), then render
  --set ID F V  safely edit one yaml field (the ONLY sanctioned state edit), then render
  --audit --strict  scan + return nonzero for actionable gaps
  --resolve-blocks  manually flip explicitly opted-in blocked jobs whose dependencies are done
  --render    (re)write _DISPATCH_STATE.md + _dispatch.parquet from the yaml blocks
  --stop-hook auto-render + print gaps ONLY (quiet if clean); for the Stop hook, fail-open
  --migrate   one-time: infer + inject a yaml block into every bare *.md, seeding
              created_at ONCE from the file's first git-commit date, then --render
  --selftest  teeth: round-trip, gap detection, canonical created_at immutability
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# R1 (AI-Team port): the parquet query view needs pandas+pyarrow, which are NOT in this
# Raspberry-Pi .venv (KISS/YAGNI + grep-first ethos — see AGENT_51 packet). Degrade to
# markdown-view-only instead of crashing when they are absent. The .parquet query view is
# explicitly deferred; the _DISPATCH_STATE.md eyeball table + --audit remain fully live.
try:
    import pandas as pd  # noqa: F401
    _HAVE_PANDAS = True
except ImportError:
    pd = None  # type: ignore[assignment]
    _HAVE_PANDAS = False

# Windows consoles default to cp1252 and choke on ✓/⚠/emoji — force UTF-8 stdout.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

# PORTABLE: paths are relative to this script's location. Keep the script at
# <repo>/scripts/dispatch/dispatch_state.py and `.ai/dispatch/` resolves with
# ZERO config in any project that shares the layout. To relocate the dispatch
# folder, set DISPATCH_DIR env var (absolute or repo-relative).
REPO_ROOT = Path(__file__).resolve().parents[2]
_env_dir = __import__("os").environ.get("DISPATCH_DIR")
DISPATCH_DIR = (Path(_env_dir) if _env_dir and Path(_env_dir).is_absolute()
                else (REPO_ROOT / _env_dir) if _env_dir
                else REPO_ROOT / ".ai" / "dispatch")
STATE_MD = DISPATCH_DIR / "_DISPATCH_STATE.md"
STATE_PARQUET = DISPATCH_DIR / "_dispatch.parquet"

# files in the folder that are NOT dispatch jobs (logs, indices, protocol docs).
# The universal ones are hardcoded; project-specific exclusions go in a sidecar
# `.dispatch_not_a_job` (one filename per line) so this file stays portable.
NOT_A_JOB = {
    "DISPATCH_LOG.md",
    "_DISPATCH_STATE.md",
    "CLAUDE.md",             # the dispatch protocol doc (auto-loaded), not a job
    "AGENTS.md", "AGENT.md", "README.md",  # common non-job docs that may live here
}

def _load_sidecar_exclusions() -> set[str]:
    """Project-specific non-job filenames, one per line, in `.dispatch_not_a_job`
    inside the dispatch dir. Keeps per-project quirks OUT of this portable file."""
    sidecar = DISPATCH_DIR / ".dispatch_not_a_job"
    if not sidecar.exists():
        return set()
    return {ln.strip() for ln in sidecar.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


NOT_A_JOB |= _load_sidecar_exclusions()

VALID_STATUS = {"ready", "active", "blocked", "done", "dead"}
FENCE = "```"
YAML_BLOCK_RE = re.compile(r"^```yaml\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class JobState:
    job_id: str
    file: str
    status: str
    created_at: str
    updated_at: str
    owner: str = ""
    depends_on: list[str] = field(default_factory=list)
    results_ref: str | None = None
    evidence: list[str] = field(default_factory=list)
    auto_unblock: bool = False
    # derived (not stored in yaml):
    flags: list[str] = field(default_factory=list)


# ─────────────────────────────── scan ────────────────────────────────────────

def _job_files() -> list[Path]:
    return sorted(p for p in DISPATCH_DIR.glob("*.md") if p.name not in NOT_A_JOB)


def _as_list(v) -> list[str]:
    """Coerce a yaml value to a list of strings. A bare scalar (e.g. a single
    evidence path set via --set) becomes a one-element list, NOT char-split."""
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


def _iso(v) -> str:
    """Normalize a yaml scalar (str or auto-parsed datetime) to ISO-with-T string."""
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return str(v or "")


def _read_yaml_block(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    m = YAML_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1)) or {}
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def scan() -> list[JobState]:
    """Read every job file's yaml block into a JobState, deriving flags."""
    jobs: list[JobState] = []
    for path in _job_files():
        data = _read_yaml_block(path)
        if data is None:
            jobs.append(JobState(
                job_id=path.stem, file=path.name, status="unknown",
                created_at="", updated_at="", flags=["MISSING_STATE"],
            ))
            continue
        js = JobState(
            job_id=str(data.get("job_id") or path.stem),
            file=path.name,
            status=str(data.get("status", "unknown")),
            created_at=_iso(data.get("created_at")),
            updated_at=_iso(data.get("updated_at")),
            owner=str(data.get("owner", "")),
            depends_on=_as_list(data.get("depends_on")),
            results_ref=data.get("results_ref"),
            evidence=_as_list(data.get("evidence")),
            auto_unblock=data.get("auto_unblock") is True,
        )
        jobs.append(js)
    for js in jobs:
        js.flags = _derive_flags(js, jobs)
    return jobs


def _derive_flags(js: JobState, all_jobs: list[JobState] | None = None) -> list[str]:
    flags: list[str] = []
    if js.status not in VALID_STATUS:
        flags.append("BAD_STATUS")
    if js.status == "done":
        if not js.evidence or any(not (REPO_ROOT / e).exists() for e in js.evidence):
            flags.append("CLAIMED_DONE_NO_PROOF")
    if js.status == "blocked" and js.auto_unblock and js.depends_on and all_jobs is not None:
        jobs_by_id = {j.job_id: j for j in all_jobs}
        if all(
            (dependency := jobs_by_id.get(dep)) is not None
            and dependency.status == "done"
            and bool(dependency.evidence)
            and all((REPO_ROOT / evidence).exists() for evidence in dependency.evidence)
            for dep in js.depends_on
        ):
            flags.append("READY_TO_UNBLOCK")
    if js.status in {"active", "ready", "blocked"} and js.created_at:
        age = _age_days(js.updated_at or js.created_at)
        if js.status == "active" and age is not None and age > 14:
            flags.append(f"STALE_{age}d")
    return flags


def _age_days(iso: str) -> int | None:
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    return (now - d).days


# ─────────────────────────────── render ──────────────────────────────────────

def to_frame(jobs: list[JobState]) -> pd.DataFrame:
    rows = [{
        "job_id": j.job_id, "file": j.file, "status": j.status,
        "created_at": j.created_at, "updated_at": j.updated_at, "owner": j.owner,
        "depends_on": ",".join(j.depends_on), "results_ref": j.results_ref or "",
        "evidence": ",".join(j.evidence), "flags": ",".join(j.flags),
    } for j in jobs]
    df = pd.DataFrame(rows).sort_values(["status", "job_id"]).reset_index(drop=True)
    return df


STATUS_ORDER = {"active": 0, "ready": 1, "blocked": 2, "unknown": 3, "done": 4, "dead": 5}


def render(jobs: list[JobState]) -> None:
    # R1: parquet query view is optional — only written when pandas+pyarrow are present.
    # The markdown eyeball table below is always written (the canonical view for this repo).
    if _HAVE_PANDAS:
        to_frame(jobs).to_parquet(STATE_PARQUET, index=False)

    jobs_sorted = sorted(jobs, key=lambda j: (STATUS_ORDER.get(j.status, 9), j.job_id))
    lines = [
        "# Dispatch State — GENERATED, DO NOT EDIT",
        "",
        f"<!-- regenerated by `python scripts/dispatch/dispatch_state.py --render` "
        f"on {dt.datetime.now(dt.timezone.utc).date()}. Source of truth = the ```yaml "
        "block at the top of each .ai/dispatch/<job>.md. Edit THAT, not this file. -->",
        "",
        "| status | job_id | created | updated | depends_on | proof | flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for j in jobs_sorted:
        proof = "—"
        if j.evidence:
            ok = all((REPO_ROOT / e).exists() for e in j.evidence)
            proof = "✓" if ok else "✗MISSING"
        dep = ",".join(j.depends_on) or "—"
        flags = " ".join(j.flags) or ""
        lines.append(
            f"| {j.status} | {j.job_id} | {j.created_at[:10] or '?'} | "
            f"{j.updated_at[:10] or '?'} | {dep} | {proof} | {flags} |"
        )
    STATE_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─────────────────────────────── audit ───────────────────────────────────────

def audit(jobs: list[JobState], strict: bool = False) -> int:
    n = len(jobs)
    with_state = sum(1 for j in jobs if "MISSING_STATE" not in j.flags)
    orphans = n - with_state
    by_status: dict[str, list[JobState]] = {}
    for j in jobs:
        by_status.setdefault(j.status, []).append(j)

    print(f"{n} files · {with_state} with state · {orphans} orphans (MISSING_STATE)")
    print()
    for st in ["active", "ready", "blocked"]:
        js = by_status.get(st, [])
        print(f"  {st.upper():8} ({len(js)})")
        for j in sorted(js, key=lambda x: x.job_id):
            extra = f"  ⚠ {' '.join(j.flags)}" if j.flags else ""
            dep = f"  dep: {','.join(j.depends_on)}" if j.depends_on else ""
            print(f"      {j.job_id:34} created {j.created_at[:10] or '?'}{dep}{extra}")
    print(f"  DONE ({len(by_status.get('done', []))}) · "
          f"DEAD ({len(by_status.get('dead', []))})")
    print()

    problems = [(j, f) for j in jobs for f in j.flags
                if f in {"MISSING_STATE", "CLAIMED_DONE_NO_PROOF", "BAD_STATUS", "READY_TO_UNBLOCK"}
                or f.startswith("STALE_")]
    if problems:
        print("  ⚠ ATTENTION:")
        for j, f in problems:
            print(f"      {f:24} {j.job_id} ({j.file})")
        return 1 if strict else 0
    print("  ✓ no gaps, no unproven-done, no stale-active")
    return 0


def resolve_blocks(jobs: list[JobState]) -> list[str]:
    """Manually unblock only packets that explicitly opt in via auto_unblock."""
    ready = [j for j in jobs if "READY_TO_UNBLOCK" in j.flags]
    for job in ready:
        set_field(job.job_id, "status", "ready")
    return [job.job_id for job in ready]


# ─────────────────────────────── migrate ─────────────────────────────────────

def _first_commit_iso(path: Path) -> str:
    """Canonical created_at seed: the file's FIRST commit date (once)."""
    try:
        out = subprocess.run(
            ["git", "log", "--diff-filter=A", "--follow", "--format=%aI", "--", str(path)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=15,
        ).stdout.strip().splitlines()
        if out:
            return out[-1]  # oldest add
    except (subprocess.SubprocessError, OSError):
        pass
    ts = path.stat().st_mtime
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def _infer_status(path: Path, log_section: dict[str, str]) -> tuple[str, str]:
    """Layered, most-authoritative-first. Returns (status, reason)."""
    text = path.read_text(encoding="utf-8")
    head = text[:1500]
    # 1) explicit marker inside the job's OWN file
    if re.search(r"❌|RETIRED|SUPERSEDED|KILLED|DEAD", head):
        return "dead", "own-file marker"
    if re.search(r"✅|DONE|COMPLETED|DELIVERED", head):
        return "done", "own-file marker"
    if re.search(r"🕐|BLOCKED|blocked on", head):
        return "blocked", "own-file marker"
    if re.search(r"🔵|READY|AUTHORED, not dispatched", head):
        return "ready", "own-file marker"
    if re.search(r"🟡|🟢|ACTIVE|LIVE|accruing|in progress", head):
        return "active", "own-file marker"
    # 2) weak fallback: DISPATCH_LOG section membership
    sec = log_section.get(path.name)
    if sec == "completed":
        return "done", "log-section (weak)"
    if sec == "dead":
        return "dead", "log-section (weak)"
    if sec == "active":
        return "active", "log-section (weak)"
    return "unknown", "NO SIGNAL — spot-check"


def _log_sections() -> dict[str, str]:
    t = (DISPATCH_DIR / "DISPATCH_LOG.md").read_text(encoding="utf-8")
    def pos(name: str) -> int:
        m = re.search(r"## .*?" + re.escape(name), t)
        return m.start() if m else -1
    bounds = [(n, pos(k)) for n, k in
              [("active", "Active + ready"), ("completed", "Completed"),
               ("dead", "Dead"), ("notopen", "Not-yet-open"), ("how", "How to update")]]
    bounds = sorted([b for b in bounds if b[1] >= 0], key=lambda x: x[1])
    out: dict[str, str] = {}
    for m in re.finditer(r"`([A-Za-z0-9_]+\.md)`", t):
        fn, p = m.group(1), m.start()
        for i, (name, b) in enumerate(bounds):
            nb = bounds[i + 1][1] if i + 1 < len(bounds) else len(t)
            if b <= p < nb:
                out.setdefault(fn, name)
                break
    return out


def set_field(job_id: str, field_name: str, value: str) -> None:
    """Safely edit ONE field in a job's yaml block via string replace on the value
    line (preserves comments + formatting); auto-bumps updated_at. This is the ONLY
    sanctioned way to mutate state — never hand-regex the blocks (that corrupts them)."""
    matches = [p for p in _job_files() if p.stem == job_id or p.name == job_id]
    if not matches:
        raise SystemExit(f"no dispatch file for job_id={job_id}")
    path = matches[0]
    text = path.read_text(encoding="utf-8")
    m = YAML_BLOCK_RE.search(text)
    if not m:
        raise SystemExit(f"{path.name} has no yaml block — run --migrate first")
    block = m.group(1)

    def replace_line(blk: str, key: str, val: str) -> str:
        # keep any trailing "  # comment" on the line
        pat = re.compile(rf"^({re.escape(key)}:\s*)(\S[^#\n]*?)(\s*#.*)?$", re.MULTILINE)
        if not pat.search(blk):
            return blk.rstrip("\n") + f"\n{key}: {val}"
        return pat.sub(lambda mm: f"{mm.group(1)}{val}{mm.group(3) or ''}", blk, count=1)

    new_block = replace_line(block, field_name, value)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    new_block = replace_line(new_block, "updated_at", f'"{now}"')
    text = text[:m.start(1)] + new_block + text[m.end(1):]
    path.write_text(text, encoding="utf-8")
    # verify it still parses (catch corruption immediately)
    if _read_yaml_block(path) is None:
        raise SystemExit(f"⚠ edit corrupted {path.name}'s yaml — reverted needed")
    print(f"{path.name}: {field_name} = {value} (updated_at bumped)")


def _inject_block(path: Path, block: str) -> None:
    text = path.read_text(encoding="utf-8")
    if YAML_BLOCK_RE.search(text):
        return  # already has one, never clobber
    path.write_text(block + "\n\n" + text, encoding="utf-8")


def _state_block(job_id: str, created: str, status: str = "ready") -> str:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return (
        f"{FENCE}yaml\n"
        f"job_id: {job_id}\n"
        f"created_at: \"{created}\"        # CANONICAL — set once at dispatch, never derive again\n"
        f"status: {status}              # ready | active | blocked | done | dead\n"
        f"owner: \"\"\n"
        f"depends_on: []\n"
        f"results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose\n"
        f"evidence: []                  # artifact paths that PROVE it ran (checked to exist)\n"
        f"updated_at: \"{now}\"\n"
        f"{FENCE}"
    )


def new_job(job_id: str) -> None:
    """Create a new dispatch stub with a CANONICAL created_at (now). One command so
    nobody hand-writes a yaml block (that's how corruption crept in)."""
    job_id = job_id.removesuffix(".md")
    path = DISPATCH_DIR / f"{job_id}.md"
    if path.exists():
        raise SystemExit(f"{path.name} already exists — edit it, or --set its status")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    body = (
        _state_block(job_id, now, status="ready") + "\n\n"
        f"# DISPATCH — {job_id}\n\n"
        "**Goal:** _(what the agent should do — one paragraph)_\n\n"
        "**Depends on:** _(blocker, or none)_\n\n"
        "## Task\n\n_(the concrete steps)_\n\n"
        "## Done when\n\n_(the deliverable + how it's proven — set `evidence:` to those paths)_\n"
    )
    path.write_text(body, encoding="utf-8")
    print(f"created {path.name} (status: ready, created_at stamped now).")
    print("  → fill the brief, then work it. When done: pnpm dispatch:set "
          f"{job_id} status done  (and add evidence: paths).")


GIT_HOOK_MARKER = "# >>> dispatch-state pre-commit (managed, idempotent) >>>"
GIT_HOOK_END = "# <<< dispatch-state pre-commit <<<"


def install_git_hook() -> int:
    """Idempotently append a WARN-ONLY dispatch check to .git/hooks/pre-commit.
    Backend-agnostic (works in any git repo). Never blocks the commit; only
    renders the views + prints gaps. Re-running replaces the managed block."""
    hooks_dir = REPO_ROOT / ".git" / "hooks"
    if not hooks_dir.exists():
        raise SystemExit("no .git/hooks — is this a git repo?")
    hook = hooks_dir / "pre-commit"
    block = (
        f"{GIT_HOOK_MARKER}\n"
        "# Renders the dispatch state views + warns on gaps. Warn-only: never blocks.\n"
        "python \"$(git rev-parse --show-toplevel)/scripts/dispatch/dispatch_state.py\" --stop-hook || true\n"
        "git add \"$(git rev-parse --show-toplevel)/.ai/dispatch/_DISPATCH_STATE.md\" "
        "\"$(git rev-parse --show-toplevel)/.ai/dispatch/_dispatch.parquet\" 2>/dev/null || true\n"
        f"{GIT_HOOK_END}\n"
    )
    if hook.exists():
        text = hook.read_text(encoding="utf-8")
        if GIT_HOOK_MARKER in text:  # replace the managed block (idempotent)
            text = re.sub(re.escape(GIT_HOOK_MARKER) + r".*?" + re.escape(GIT_HOOK_END) + r"\n?",
                          block, text, flags=re.DOTALL)
        else:  # append after the existing hook body
            text = text.rstrip("\n") + "\n\n" + block
    else:
        text = "#!/bin/sh\n" + block
    hook.write_text(text, encoding="utf-8")
    hook.chmod(0o755)
    print(f"installed dispatch pre-commit block into {hook} (warn-only, idempotent).")
    return 0


def stop_hook() -> int:
    """Session-end hook: keep the views fresh + surface gaps, but NEVER block the
    session. Fail-open on any error (a broken hook must not trap a handoff)."""
    try:
        jobs = scan()
        render(jobs)
    except Exception as e:  # noqa: BLE001 — fail-open is the whole point
        print(f"[dispatch] render skipped ({type(e).__name__}) — state views may be stale.")
        return 0
    gaps = [(j, f) for j in jobs for f in j.flags
            if f in {"MISSING_STATE", "CLAIMED_DONE_NO_PROOF", "BAD_STATUS", "READY_TO_UNBLOCK"}
            or f.startswith("STALE_")]
    if not gaps:
        return 0
    print("⚠ [dispatch] state has gaps — resolve on the go (does NOT block this session):")
    for j, f in gaps:
        fix = {
            "MISSING_STATE": f"add a yaml block:  pnpm dispatch:set {j.job_id} status <s>",
            "CLAIMED_DONE_NO_PROOF": f"add real evidence: paths in {j.file}, or set status back",
            "BAD_STATUS": f"pnpm dispatch:set {j.job_id} status ready|active|blocked|done|dead",
            "READY_TO_UNBLOCK": f"review then run: python scripts/dispatch/dispatch_state.py --resolve-blocks",
        }.get(f, f"review {j.file} — {f}")
        print(f"    {f:22} {j.job_id:34} → {fix}")
    return 0


def migrate() -> None:
    log_section = _log_sections()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    report: list[tuple[str, str, str]] = []
    for path in _job_files():
        if YAML_BLOCK_RE.search(path.read_text(encoding="utf-8")):
            report.append((path.stem, "(has block)", ""))
            continue
        status, reason = _infer_status(path, log_section)
        created = _first_commit_iso(path)
        block = (
            f"{FENCE}yaml\n"
            f"job_id: {path.stem}\n"
            f"created_at: \"{created}\"        # CANONICAL — set once at dispatch, never derive again\n"
            f"status: {status}              # ready | active | blocked | done | dead\n"
            f"owner: \"\"\n"
            f"depends_on: []\n"
            f"results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose\n"
            f"evidence: []                  # artifact paths that PROVE it ran (checked to exist)\n"
            f"updated_at: \"{now}\"\n"
            f"{FENCE}"
        )
        _inject_block(path, block)
        report.append((path.stem, status, reason))
    print(f"migrated {len(report)} files:")
    for jid, status, reason in report:
        mark = "  " if reason in ("", "own-file marker") else "⚠ "
        print(f"  {mark}{status:9} {jid:38} {reason}")
    print("\nrendering views…")
    render(scan())
    _views = STATE_MD.name + (f" + {STATE_PARQUET.name}" if _HAVE_PANDAS
                              else " (md-view-only: pandas absent, parquet skipped)")
    print(f"wrote {_views}. SPOT-CHECK the 'unknown'/⚠ rows.")


# ─────────────────────────────── selftest ────────────────────────────────────

def selftest() -> int:
    import tempfile
    ok = True

    # 1) round-trip: a yaml block scans back to identical values
    blk = ("```yaml\njob_id: TEST_1\ncreated_at: 2026-01-01T00:00:00Z\n"
           "status: active\nowner: me\ndepends_on: [X]\nresults_ref: null\n"
           "evidence: []\nupdated_at: 2026-01-02T00:00:00Z\n```\n# brief\n")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "TEST_1.md"
        p.write_text(blk, encoding="utf-8")
        data = _read_yaml_block(p)
        assert data and data["status"] == "active" and data["depends_on"] == ["X"], data
    print("  ✓ round-trip: yaml block parses back exactly")

    # 2) gap detection: bare file → MISSING_STATE
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "BARE.md"
        p.write_text("# no yaml here\n", encoding="utf-8")
        assert _read_yaml_block(p) is None
    print("  ✓ gap detection: bare file yields no block (→ MISSING_STATE)")

    # 3) CLAIMED_DONE_NO_PROOF fires unless a done job names existing proof
    js = JobState("J", "J.md", "done", "2026-01-01", "2026-01-01",
                  evidence=["does/not/exist.parquet"])
    assert "CLAIMED_DONE_NO_PROOF" in _derive_flags(js)
    js2 = JobState("J", "J.md", "done", "2026-01-01", "2026-01-01", evidence=[])
    assert "CLAIMED_DONE_NO_PROOF" in _derive_flags(js2)
    print("  ✓ proof guard: every done job requires existing evidence")

    # 4) canonical created_at immutability: injecting never clobbers an existing block
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "KEEP.md"
        p.write_text(blk, encoding="utf-8")
        _inject_block(p, "```yaml\ncreated_at: 2099-09-09T00:00:00Z\n```")
        assert _iso(_read_yaml_block(p)["created_at"]) == "2026-01-01T00:00:00+00:00"
    print("  ✓ canonical created_at: existing block never overwritten")

    # 5) bad status flagged
    assert "BAD_STATUS" in _derive_flags(JobState("J", "J.md", "nonsense", "", ""))
    print("  ✓ bad status flagged")

    # 6) set_field edits one value, keeps the comment, bumps updated_at, stays parseable
    global DISPATCH_DIR
    saved = DISPATCH_DIR
    with tempfile.TemporaryDirectory() as d:
        DISPATCH_DIR = Path(d)
        p = Path(d) / "SETME.md"
        p.write_text("```yaml\njob_id: SETME\ncreated_at: \"2026-01-01T00:00:00Z\"\n"
                     "status: ready              # ready | active | ...\nowner: \"\"\n"
                     "depends_on: []\nresults_ref: null\nevidence: []\n"
                     "updated_at: \"2026-01-01T00:00:00Z\"\n```\n# brief\n", encoding="utf-8")
        set_field("SETME", "status", "done")
        d2 = _read_yaml_block(p)
        assert d2 and d2["status"] == "done", d2
        assert _iso(d2["created_at"]).startswith("2026-01-01"), "created_at must be untouched"
        assert _iso(d2["updated_at"]) != "2026-01-01T00:00:00Z", "updated_at must bump"
        assert "# ready | active" in p.read_text(encoding="utf-8"), "comment must survive"
    DISPATCH_DIR = saved
    print("  ✓ set_field: edits value, keeps comment, bumps updated_at, stays parseable")

    # 7) a scalar evidence value coerces to a 1-element list (not char-split)
    assert _as_list("a/b.parquet") == ["a/b.parquet"]
    assert _as_list(["x", "y"]) == ["x", "y"]
    assert _as_list(None) == [] and _as_list("") == []
    print("  ✓ scalar evidence coerces to [path], not char-split")

    # 8) legacy naive timestamps must not crash age checks
    assert _age_days("2026-01-01T00:00:00") is not None
    print("  ✓ naive timestamp is normalized before age calculation")

    # 9) dependency completion alone never changes an operator-gated job
    prerequisite = JobState("PRE", "PRE.md", "done", "", "", evidence=["pyproject.toml"])
    gated = JobState("GATED", "GATED.md", "blocked", "", "", depends_on=["PRE"])
    opted_in = JobState("OPTED", "OPTED.md", "blocked", "", "", depends_on=["PRE"], auto_unblock=True)
    unproven = JobState("UNPROVEN", "UNPROVEN.md", "done", "", "", evidence=[])
    unsafe = JobState("UNSAFE", "UNSAFE.md", "blocked", "", "", depends_on=["UNPROVEN"], auto_unblock=True)
    jobs = [prerequisite, gated, opted_in]
    assert "READY_TO_UNBLOCK" not in _derive_flags(gated, jobs)
    assert "READY_TO_UNBLOCK" in _derive_flags(opted_in, jobs)
    unproven.flags = _derive_flags(unproven, [unproven, unsafe])
    assert "READY_TO_UNBLOCK" not in _derive_flags(unsafe, [unproven, unsafe])
    print("  ✓ auto-unblock requires explicit per-packet opt-in")

    print("\nSELFTEST PASS" if ok else "\nSELFTEST FAIL")
    return 0 if ok else 1


# ─────────────────────────────── cli ─────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Dispatch state manager (folder-is-the-registry).")
    ap.add_argument("--audit", action="store_true", help="scan + print the honest board (default)")
    ap.add_argument("--strict", action="store_true", help="with --audit, fail when gaps exist")
    ap.add_argument("--resolve-blocks", action="store_true", help="manually unblock explicit auto_unblock jobs")
    ap.add_argument("--render", action="store_true", help="(re)write _DISPATCH_STATE.md + _dispatch.parquet")
    ap.add_argument("--migrate", action="store_true", help="one-time: inject yaml blocks + seed created_at, then render")
    ap.add_argument("--set", nargs=3, metavar=("JOB_ID", "FIELD", "VALUE"),
                    help="safely edit one field in a job's yaml block (e.g. --set AGENT_15 status done); bumps updated_at + re-renders")
    ap.add_argument("--new", metavar="JOB_ID", help="create a new dispatch stub with a canonical created_at, then render")
    ap.add_argument("--stop-hook", action="store_true", help="session-end: auto-render + warn on gaps (fail-open, never blocks)")
    ap.add_argument("--install-git-hook", action="store_true", help="idempotently add a warn-only dispatch check to .git/hooks/pre-commit (any backend)")
    ap.add_argument("--selftest", action="store_true", help="teeth")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.install_git_hook:
        return install_git_hook()
    if args.stop_hook:
        return stop_hook()
    if args.resolve_blocks:
        flipped = resolve_blocks(scan())
        render(scan())
        print("resolved: " + ", ".join(flipped) if flipped else "no explicitly auto-unblockable jobs")
        return 0
    if args.new:
        new_job(args.new)
        render(scan())
        return 0
    if args.set:
        set_field(*args.set)
        render(scan())
        return 0
    if args.migrate:
        migrate()
        return 0
    if args.render:
        render(scan())
        wrote = STATE_MD.name + (f" + {STATE_PARQUET.name}" if _HAVE_PANDAS
                                 else " (md-view-only: pandas absent, parquet skipped)")
        print(f"wrote {wrote}")
        return 0
    return audit(scan(), strict=args.strict)  # default


if __name__ == "__main__":
    sys.exit(main())
