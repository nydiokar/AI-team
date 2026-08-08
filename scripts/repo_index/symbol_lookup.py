#!/usr/bin/env python3
"""
Repo symbol-index PoC — O4 (repo-readability tooling).

Usage
-----
  # Build (or rebuild) the tags index:
  python scripts/repo_index/symbol_lookup.py --build

  # Lookup a symbol (builds on first call if index is absent):
  python scripts/repo_index/symbol_lookup.py SessionService
  python scripts/repo_index/symbol_lookup.py dispatch_worker --defs-only

  # Rebuild + lookup in one call:
  python scripts/repo_index/symbol_lookup.py --build classify_error_text

How it works
------------
Wraps universal-ctags (OS package: `apt install universal-ctags`) and the
companion `readtags` binary.  The index is a standard ctags file written to
`.ctags_index` in the repo root.  No resident daemon — every call is a
short-lived subprocess (<200 ms for this repo).

Intent: an agent can call this script (or its readtags equivalent directly)
to resolve `symbol → file:line` and then read ONLY that span, instead of
reading whole files.  Directly reduces per-session orientation tokens.

Requirements
------------
  apt install universal-ctags   # provides `ctags` and `readtags`
  No Python packages beyond stdlib.

Deployment shape (per O4 recommendation)
-----------------------------------------
  per-project, stateless CLI — zero resident RSS.
  The index file (.ctags_index) sits in the repo root and is gitignored.
  Rebuild it whenever the codebase changes (< 0.5 s for this repo).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Repo root = two levels up from this script
REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
INDEX_FILE: Path = REPO_ROOT / ".ctags_index"

# Directories to index — add more here as the repo grows
SOURCE_DIRS: list[str] = ["src", "scripts", "web/src"]

# ctags kinds considered "definitions" (class, function, method, variable)
# rather than imports/re-exports.  See `ctags --list-kinds=<lang>`.
DEFINITION_KINDS: frozenset[str] = frozenset(
    ["c", "f", "m", "v", "F", "M", "C", "i"]
)


def _check_tools() -> None:
    missing: list[str] = []
    for tool in ("ctags", "readtags"):
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        print(
            f"ERROR: missing tools: {', '.join(missing)}.\n"
            "Install with:  sudo apt install universal-ctags",
            file=sys.stderr,
        )
        sys.exit(1)


def build_index(verbose: bool = True) -> None:
    """Build (or rebuild) the ctags index over SOURCE_DIRS."""
    _check_tools()
    dirs: list[str] = [
        d for d in SOURCE_DIRS if (REPO_ROOT / d).is_dir()
    ]
    if not dirs:
        print("ERROR: no source directories found", file=sys.stderr)
        sys.exit(1)

    cmd: list[str] = [
        "ctags",
        "--fields=+n",       # add line: field to every tag
        "--extras=+r",       # include reference tags
        "-R",                # recursive
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
        print(f"Index built: {INDEX_FILE} ({size_kb:.0f} KB)", file=sys.stderr)


def lookup(
    symbol: str,
    defs_only: bool = False,
) -> list[dict[str, str]]:
    """
    Return a list of matches for *symbol* from the tags index.

    Each match is a dict with keys: name, file, line, kind, (optional) module.
    Sorted so that definition kinds (class, function, …) come before imports.
    """
    _check_tools()
    if not INDEX_FILE.exists():
        build_index(verbose=True)

    cmd: list[str] = [
        "readtags",
        "-t", str(INDEX_FILE),
        "-e",                # extended output (tab-separated fields)
        "-n",                # include line: field in output
        symbol,
    ]
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

        # readtags -e output format:
        # name  file  pattern/address  [field:value ...]
        name: str = parts[0]
        file_path: str = parts[1]
        extra_fields: dict[str, str] = {}
        for field in parts[3:]:
            if ":" in field:
                k, _, v = field.partition(":")
                extra_fields[k] = v

        kind: str = extra_fields.get("kind", "?")
        line_no: str = extra_fields.get("line", "?")
        module: str = extra_fields.get("module", "")

        record: dict[str, str] = {
            "name": name,
            "file": file_path,
            "line": line_no,
            "kind": kind,
        }
        if module:
            record["module"] = module

        if defs_only and kind not in DEFINITION_KINDS:
            continue

        matches.append(record)

    # Put definitions first (class, function) before imports/re-exports
    matches.sort(key=lambda m: (m["kind"] not in DEFINITION_KINDS, m["file"]))
    return matches


def _format_matches(matches: list[dict[str, str]]) -> str:
    if not matches:
        return "(no matches)"
    lines: list[str] = []
    for m in matches:
        mod: str = f"  [{m['module']}]" if m.get("module") else ""
        lines.append(f"  {m['file']}:{m['line']}  ({m['kind']}){mod}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve symbol → file:line from the repo ctags index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "symbol",
        nargs="?",
        help="Symbol name to look up.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="(Re)build the ctags index before lookup.",
    )
    parser.add_argument(
        "--defs-only",
        action="store_true",
        help="Return only definition kinds (class/function/method/variable), "
             "omitting import re-exports.",
    )
    args = parser.parse_args()

    if args.build:
        build_index(verbose=True)

    if args.symbol:
        if not args.build and not INDEX_FILE.exists():
            build_index(verbose=True)
        matches: list[dict[str, str]] = lookup(args.symbol, defs_only=args.defs_only)
        print(f"{args.symbol}:")
        print(_format_matches(matches))
    elif not args.build:
        parser.print_help()


if __name__ == "__main__":
    main()
