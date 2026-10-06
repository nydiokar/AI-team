#!/usr/bin/env python3
"""PreToolUse(Bash) guard — TEST COST GUARD from CLAUDE.md, enforced.

Blocks (exit 2, reason on stderr → shown to the agent):
  * any pytest run with `--run-e2e` or `AI_TEAM_ALLOW_OPENCODE_E2E` (real e2e is opt-in, operator-only)
  * any pytest run with no explicit test-file/node target, or targeting the whole `tests` dir
    (full suite) — run the touched modules only.
Everything else passes through untouched. Operators can still run anything via `! <cmd>`.
"""
import json
import re
import shlex
import sys

# pytest options that consume the following token as their value
VALUE_OPTS: frozenset[str] = frozenset({
    "-k", "-m", "-c", "-p", "-o", "-n", "-W", "--deselect", "--ignore", "--ignore-glob",
    "--rootdir", "--maxfail", "--tb", "--junitxml", "--basetemp", "--durations",
    "--confcutdir", "--override-ini", "--log-level", "--timeout",
})
WRAPPERS: frozenset[str] = frozenset({"env", "time", "timeout", "nice", "export", "exec", "uv", "run"})
WHOLE_SUITE: frozenset[str] = frozenset({"tests", "tests/", ".", "./", "./tests", "./tests/"})


def pytest_args(segment: str) -> list[str] | None:
    """Return the argv after `pytest` if this shell segment invokes pytest, else None."""
    try:
        tokens: list[str] = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    # skip env assignments and transparent wrappers to reach the command word
    i: int = 0
    while i < len(tokens) and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])
                               or tokens[i] in WRAPPERS or tokens[i].lstrip("-").isdigit()):
        i += 1
    if i >= len(tokens):
        return None
    cmd: str = tokens[i].rsplit("/", 1)[-1]
    if cmd in ("pytest", "py.test"):
        return tokens[i + 1:]
    if cmd.startswith("python") and tokens[i + 1:i + 3] == ["-m", "pytest"]:
        return tokens[i + 3:]
    return None


def violation(command: str) -> str | None:
    if "pytest" not in command:
        return None
    for segment in re.split(r"&&|\|\||;|\||\n|\(|\)", command):
        args: list[str] | None = pytest_args(segment)
        if args is None:
            continue
        if re.search(r"\bAI_TEAM_ALLOW_OPENCODE_E2E=", command):
            return "AI_TEAM_ALLOW_OPENCODE_E2E enables real paid e2e runs — operator-only."
        if any(a == "--run-e2e" or a.startswith("--run-e2e=") for a in args):
            return "--run-e2e runs real (paid) e2e tests — operator-only."
        targets: list[str] = []
        skip_next: bool = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in VALUE_OPTS:
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            targets.append(a)
        if not targets:
            return "pytest with no explicit targets runs the FULL suite."
        if any(t in WHOLE_SUITE for t in targets):
            return f"pytest {' '.join(targets)} runs the FULL suite."
    return None


def main() -> int:
    try:
        payload: dict = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    command: str = str(payload.get("tool_input", {}).get("command", ""))
    reason: str | None = violation(command)
    if reason is None:
        return 0
    print(
        f"BLOCKED by TEST COST GUARD (CLAUDE.md): {reason} "
        "Run plain pytest on the touched modules only, e.g. "
        "`.venv/bin/pytest tests/test_<module>.py`. See the `safe-pytest` skill for target selection.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
