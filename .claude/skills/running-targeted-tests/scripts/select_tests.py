#!/usr/bin/env python3
"""Map changed files to the pytest / vitest targets that cover them.

Usage (from the repo root):
    python3 .claude/skills/running-targeted-tests/scripts/select_tests.py            # diff vs origin/main + worktree
    python3 .claude/skills/running-targeted-tests/scripts/select_tests.py FILE...    # explicit files

Prints the matched test files grouped by reason, then a ready-to-run command.
Never runs anything itself. e2e-marked files are excluded (they bill the paid CLI).
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[4]
TESTS: Path = ROOT / "tests"
# Above this many files a "targeted" run stops being targeted; narrow with -k or pick direct matches.
MAX_TARGETS: int = 30
E2E_MARK: re.Pattern[str] = re.compile(r"pytestmark\s*=\s*pytest\.mark\.e2e|@pytest\.mark\.e2e")


def git_lines(*args: str) -> list[str]:
    out: subprocess.CompletedProcess[str] = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def changed_files() -> list[str]:
    base: list[str] = git_lines("merge-base", "origin/main", "HEAD")
    files: set[str] = set(git_lines("diff", "--name-only", base[0])) if base else set()
    files |= set(git_lines("diff", "--name-only"))
    files |= set(git_lines("ls-files", "--others", "--exclude-standard"))
    return sorted(files)


def is_e2e(path: Path) -> bool:
    try:
        return bool(E2E_MARK.search(path.read_text(errors="ignore")))
    except OSError:
        return False


def importers(module: str) -> set[Path]:
    """Test files that reference the dotted module path (imports or monkeypatch strings)."""
    pattern: str = re.escape(module) + r"\b"
    out: subprocess.CompletedProcess[str] = subprocess.run(
        ["grep", "-rlE", pattern, str(TESTS), "--include=test_*.py"], text=True, capture_output=True
    )
    return {Path(p) for p in out.stdout.splitlines()}


def targets_for(rel: str) -> dict[str, set[Path]]:
    path: Path = Path(rel)
    found: dict[str, set[Path]] = {}
    if path.suffix != ".py" or path.name == "__init__.py":
        return found
    if path.parts[0] == "tests" and path.name.startswith("test_"):
        found["changed test"] = {ROOT / path}
        return found
    if path.parts[0] not in ("src", "scripts") and len(path.parts) > 1:
        return found
    stem: str = path.stem
    named: set[Path] = set(TESTS.glob(f"test_{stem}.py")) | set(TESTS.glob(f"test_{stem}_*.py"))
    if named:
        found["name match"] = named
    module: str = ".".join(path.with_suffix("").parts)
    imp: set[Path] = importers(module) - named
    if imp:
        found[f"references {module}"] = imp
    return found


def main(argv: list[str]) -> int:
    files: list[str] = argv or changed_files()
    if not files:
        print("No changed files found (vs origin/main or worktree). Pass files explicitly.")
        return 0
    py_targets: set[Path] = set()
    direct: set[Path] = set()
    web_files: list[str] = []
    excluded: set[Path] = set()
    uncovered: list[str] = []
    for rel in files:
        if rel.startswith("web/src/"):
            web_files.append(rel.removeprefix("web/"))
            continue
        groups: dict[str, set[Path]] = targets_for(rel)
        if not groups and rel.endswith(".py") and rel.split("/")[0] in ("src", "scripts"):
            uncovered.append(rel)
        for reason, paths in groups.items():
            keep: set[Path] = {p for p in paths if p.exists() and not is_e2e(p)}
            excluded |= {p for p in paths if p.exists() and is_e2e(p)}
            if keep:
                print(f"{rel}  [{reason}]")
                for p in sorted(keep):
                    print(f"    {p.relative_to(ROOT)}")
            py_targets |= keep
            if reason in ("name match", "changed test"):
                direct |= keep
    if uncovered:
        print("\nNo test found for (decide: write one, or justify the gap in your hand-back):")
        for rel in uncovered:
            print(f"    {rel}")
    if excluded:
        print("\nExcluded e2e files (paid CLI; operator-only):")
        for p in sorted(excluded):
            print(f"    {p.relative_to(ROOT)}")
    if py_targets:
        rels: list[str] = sorted(str(p.relative_to(ROOT)) for p in py_targets)
        print(f"\n{len(rels)} pytest file(s).")
        if len(rels) > MAX_TARGETS:
            print(f"More than {MAX_TARGETS}: start with the direct matches, widen only if needed.")
            if direct:
                print("Direct:\n    .venv/bin/pytest " + " ".join(sorted(str(p.relative_to(ROOT)) for p in direct)))
        print("All:\n    .venv/bin/pytest " + " ".join(rels))
    if web_files:
        print("\nWeb UI changes — run from web/:\n    pnpm exec vitest related --run " + " ".join(web_files))
        print("    pnpm typecheck")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
