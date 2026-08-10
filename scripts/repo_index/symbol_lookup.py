#!/usr/bin/env python3
"""
Repo symbol-index — resolve `symbol -> file:line` so an agent reads only the span
it needs instead of loading whole files. Portable drop-in (any repo, any language
universal-ctags understands).

Usage
-----
  python scripts/repo_index/symbol_lookup.py --build                 # (re)build the index
  python scripts/repo_index/symbol_lookup.py SessionService          # look up a symbol
  python scripts/repo_index/symbol_lookup.py _dispatch_worker --defs-only
  python scripts/repo_index/symbol_lookup.py --build TaskResult      # rebuild + look up
  python scripts/repo_index/symbol_lookup.py --stale                 # just report freshness

How it works
------------
Wraps universal-ctags (OS package: `apt install universal-ctags`) and its companion
`readtags`. The index is a standard ctags file at `.ctags_index` in the repo root.
No resident daemon: every call is a short-lived subprocess (zero idle RSS).

Flawless-by-default behaviour
-----------------------------
- **Auto-staleness rebuild.** Before a lookup, if any source file is newer than the
  index, it is rebuilt automatically so results are never silently stale. Disable with
  `--no-auto` (e.g. inside a tight loop where you know the tree is unchanged).
- **Fuzzy suggestions on a miss.** An exact-name miss (the classic `dispatch_worker`
  vs `_dispatch_worker` trap) prints the closest real symbol names instead of a bare
  "(no matches)", so you rarely need a follow-up Grep.

Portability
-----------
- Repo root is found via `git rev-parse --show-toplevel`, else the cwd.
- Directories to index are auto-detected from a common set, OR taken verbatim from a
  `.ctags_dirs` file at the repo root (one path per line) if it exists.

Requirements
------------
  apt install universal-ctags        # provides `ctags` and `readtags`
  No Python packages beyond stdlib.
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except Exception:
        return Path.cwd()


REPO_ROOT: Path = _repo_root()
INDEX_FILE: Path = REPO_ROOT / ".ctags_index"
DIRS_FILE: Path = REPO_ROOT / ".ctags_dirs"

# Common source roots across languages; only those that exist are indexed.
_CANDIDATE_DIRS: list[str] = [
    "src", "lib", "app", "apps", "pkg", "cmd", "internal",
    "scripts", "packages", "server", "backend", "web/src", "frontend/src",
]

# Directories never worth walking for staleness (heavy, generated, or VCS).
_PRUNE_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
     ".pytest_cache", "dist", "build", ".next", ".ctags_index"}
)

# ctags kinds treated as "definitions" (class/function/method/variable/interface/…)
# rather than imports/re-exports. See `ctags --list-kinds=<lang>`.
DEFINITION_KINDS: frozenset[str] = frozenset(
    ["c", "f", "m", "v", "F", "M", "C", "i", "s", "t", "g", "e", "n", "I"]
)


def _check_tools() -> None:
    missing: list[str] = [t for t in ("ctags", "readtags") if not shutil.which(t)]
    if missing:
        print(
            f"ERROR: missing tools: {', '.join(missing)}.\n"
            "Install with:  sudo apt install universal-ctags",
            file=sys.stderr,
        )
        sys.exit(1)


def _source_dirs() -> list[str]:
    if DIRS_FILE.is_file():
        pinned: list[str] = [
            ln.strip() for ln in DIRS_FILE.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        dirs = [d for d in pinned if (REPO_ROOT / d).is_dir()]
        if dirs:
            return dirs
    return [d for d in _CANDIDATE_DIRS if (REPO_ROOT / d).is_dir()]


def _is_stale() -> bool:
    """True if any indexed source file is newer than the index (short-circuits)."""
    if not INDEX_FILE.exists():
        return True
    index_mtime: float = INDEX_FILE.stat().st_mtime
    for d in _source_dirs():
        for root, dirnames, filenames in os.walk(REPO_ROOT / d):
            dirnames[:] = [dn for dn in dirnames if dn not in _PRUNE_DIRS]
            for fn in filenames:
                try:
                    if os.stat(os.path.join(root, fn)).st_mtime > index_mtime:
                        return True
                except OSError:
                    continue
    return False


def build_index(verbose: bool = True) -> None:
    """Build (or rebuild) the ctags index over the detected/pinned source dirs."""
    _check_tools()
    dirs: list[str] = _source_dirs()
    if not dirs:
        print(
            "ERROR: no source directories found. Create a `.ctags_dirs` file at the "
            "repo root listing your source folders (one per line).",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd: list[str] = [
        "ctags",
        "--fields=+n",                 # add line: field to every tag
        "--extras=+r",                 # include reference tags
        "-R",                          # recursive
        "--output-format=u-ctags",
        "-f", str(INDEX_FILE),
        *dirs,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f"ctags error:\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)
    if verbose:
        size_kb: float = INDEX_FILE.stat().st_size / 1024
        print(
            f"Index built: {INDEX_FILE} ({size_kb:.0f} KB) over: {', '.join(dirs)}",
            file=sys.stderr,
        )


def _all_names() -> list[str]:
    """Every distinct tag name in the index (for fuzzy suggestions)."""
    result = subprocess.run(
        ["readtags", "-t", str(INDEX_FILE), "-l"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        return []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        if not line or line.startswith("!_TAG"):
            continue
        name: str = line.split("\t", 1)[0]
        if name:
            seen.add(name)
    return list(seen)


def suggest(symbol: str, limit: int = 10) -> list[str]:
    """Closest real symbol names to *symbol* — substring hits first, then fuzzy."""
    names: list[str] = _all_names()
    low: str = symbol.lower()
    substr: list[str] = sorted(
        (n for n in names if low in n.lower()),
        key=lambda n: (len(n), n),
    )
    out: list[str] = substr[:limit]
    if len(out) < limit:
        fuzzy: list[str] = difflib.get_close_matches(
            symbol, names, n=limit, cutoff=0.6
        )
        for n in fuzzy:
            if n not in out:
                out.append(n)
            if len(out) >= limit:
                break
    return out[:limit]


def lookup(symbol: str, defs_only: bool = False) -> list[dict[str, str]]:
    """Return matches for *symbol* from the tags index (definitions sorted first)."""
    _check_tools()
    cmd: list[str] = ["readtags", "-t", str(INDEX_FILE), "-e", "-n", symbol]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    if result.returncode != 0:
        return []

    matches: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line or line.startswith("!_TAG"):
            continue
        parts: list[str] = line.split("\t")
        if len(parts) < 3:
            continue
        name: str = parts[0]
        file_path: str = parts[1]
        extra: dict[str, str] = {}
        for field in parts[3:]:
            if ":" in field:
                k, _, v = field.partition(":")
                extra[k] = v
        kind: str = extra.get("kind", "?")
        line_no: str = extra.get("line", "?")
        module: str = extra.get("module", "")
        if defs_only and kind not in DEFINITION_KINDS:
            continue
        rec: dict[str, str] = {"name": name, "file": file_path, "line": line_no, "kind": kind}
        if module:
            rec["module"] = module
        matches.append(rec)

    matches.sort(key=lambda m: (m["kind"] not in DEFINITION_KINDS, m["file"]))
    return matches


def _format(matches: list[dict[str, str]], symbol: str) -> str:
    if matches:
        return "\n".join(
            f"  {m['file']}:{m['line']}  ({m['kind']})"
            + (f"  [{m['module']}]" if m.get("module") else "")
            for m in matches
        )
    hints: list[str] = suggest(symbol)
    if hints:
        return (
            "  (no exact match — did you mean:)\n"
            + "\n".join(f"    {h}" for h in hints)
        )
    return "  (no matches — try Grep for a concept, or --build if the tree changed)"


def _ensure_fresh(auto: bool) -> None:
    if not INDEX_FILE.exists():
        build_index(verbose=True)
    elif auto and _is_stale():
        print("(index stale — rebuilding)", file=sys.stderr)
        build_index(verbose=True)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Resolve symbol -> file:line from the repo ctags index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("symbol", nargs="?", help="Symbol name to look up.")
    p.add_argument("--build", action="store_true", help="(Re)build the index before lookup.")
    p.add_argument(
        "--defs-only", action="store_true",
        help="Return only definition kinds, omitting import re-exports.",
    )
    p.add_argument(
        "--no-auto", action="store_true",
        help="Skip the auto-staleness check (assume the index is current).",
    )
    p.add_argument(
        "--stale", action="store_true",
        help="Report whether the index is stale, then exit (no lookup).",
    )
    args = p.parse_args()

    if args.stale:
        _check_tools()
        stale: bool = _is_stale()
        print("STALE" if stale else "FRESH")
        sys.exit(1 if stale else 0)

    if args.build:
        build_index(verbose=True)
    if args.symbol:
        if not args.build:
            _ensure_fresh(auto=not args.no_auto)
        print(f"{args.symbol}:")
        print(_format(lookup(args.symbol, defs_only=args.defs_only), args.symbol))
    elif not args.build:
        p.print_help()


if __name__ == "__main__":
    main()
