"""Architecture contract checker: does the code at a revision match the end state a job declared?

A job (dispatch packet, design doc) states its intended end state as a fenced ```arch-contract YAML block:

    scope: [src, scripts]            # path prefixes checked (default: whole repo)
    exclude: [src/legacy_tests]      # path prefixes/globs skipped
    retired: [old_send, arm_group]   # names that must be gone: no def, reference, import or string use
    retired_patterns: ['^run_managed_']  # same, as regexes over identifier tokens
    forbidden_in:                    # layering: tokens that must not appear under one area (prefix or glob)
      src/backends: ['^timeout_seconds$', {pattern: '^managed$', kinds: [def, ref]}]   # str = every kind
    stores:                          # who may touch a table (file globs or 'path::qualname' globs)
      outbox: {writers: [src/db.py], readers: [src/db.py]}   # [] = nobody; key absent = unconstrained
    waivers:                         # explicit, reasoned exceptions; always printed
      - {match: 'retired:arm_group', path: 'src/db.py', reason: 'down-migration only, removed by JOB-12'}

The check is a pure function of the files at the revision: Python via AST tokens (definitions, references,
imports, string literals; docstrings are reported as notes, comments ignored), any other text file by identifier
tokens per line, and SQL table access via `pyast_adapter.data_access`. No LLM, no graph service.

Exit codes: 0 contract met, 1 violations, 2 contract or input error.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pyast_adapter import data_access  # noqa: E402

TOKEN: re.Pattern[str] = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
BLOCK: re.Pattern[str] = re.compile(r'^```arch-contract[ \t]*\n(.*?)^```', re.M | re.S)
MAX_BYTES: int = 2_000_000
LIMITS: list[str] = [
    'Static only: names built at runtime (string concatenation, computed getattr) are not seen.',
    'SQL is read from string literals inside a def or at module level; SQL assembled across functions is not seen.',
    'Python comments are ignored; docstring mentions are listed as notes, not violations.',
]


class StoreRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    writers: Optional[list[str]] = None
    readers: Optional[list[str]] = None


class Waiver(BaseModel):
    model_config = ConfigDict(extra='forbid')
    match: str
    path: str = '*'
    reason: str = Field(min_length=1)


KINDS: frozenset[str] = frozenset({'def', 'ref', 'import', 'string', 'text'})


class Forbidden(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pattern: str
    kinds: list[str] = sorted(KINDS)

    @field_validator('pattern')
    @classmethod
    def _compiles(cls, v: str) -> str:
        re.compile(v)
        return v

    @field_validator('kinds')
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        if not v or set(v) - KINDS:
            raise ValueError(f'kinds must be a non-empty subset of {sorted(KINDS)}')
        return v


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: list[str] = ['.']
    exclude: list[str] = []
    retired: list[str] = []
    retired_patterns: list[str] = []
    forbidden_in: dict[str, list[Forbidden]] = {}
    stores: dict[str, StoreRule] = {}
    waivers: list[Waiver] = []

    @field_validator('retired_patterns')
    @classmethod
    def _compiles(cls, v: list[str]) -> list[str]:
        for p in v:
            re.compile(p)
        return v

    @field_validator('forbidden_in', mode='before')
    @classmethod
    def _plain_patterns(cls, v: Any) -> Any:
        """A plain string entry means the pattern applies to every kind."""
        return {a: [{'pattern': p} if isinstance(p, str) else p for p in ps] for a, ps in v.items()} \
            if isinstance(v, dict) else v

    @model_validator(mode='after')
    def _asserts_something(self) -> 'Contract':
        if not (self.retired or self.retired_patterns or self.forbidden_in or self.stores):
            raise ValueError('contract asserts nothing (no retired, retired_patterns, forbidden_in or stores)')
        return self


def load_contract(path: Path) -> Contract:
    """Contract from a .md packet (its single ```arch-contract block) or a plain YAML file."""
    text: str = path.read_text(encoding='utf-8')
    if path.suffix.lower() == '.md':
        blocks: list[str] = BLOCK.findall(text)
        if len(blocks) != 1:
            raise ValueError(f'{path}: expected exactly one ```arch-contract block, found {len(blocks)}')
        text = blocks[0]
    return Contract.model_validate(yaml.safe_load(text) or {})


def _occ(token: str, kind: str, rel: str, line: int) -> dict[str, Any]:
    return {'token': token, 'kind': kind, 'file': rel, 'line': line}


def scan_text(text: str, rel: str) -> list[dict[str, Any]]:
    return [_occ(t, 'text', rel, i) for i, ln in enumerate(text.splitlines(), 1) for t in sorted(set(TOKEN.findall(ln)))]


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            first: ast.stmt = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def _accesses(tree: ast.Module, rel: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def visit(node: ast.AST, qual: str) -> None:
        for a in data_access(node):
            out.append({'where': f'{rel}::{qual}', 'file': rel, 'table': a['table'], 'mode': a['mode'], 'line': a['line']})
        for child in _child_defs(node):
            visit(child, child.name if qual == '<module>' else f'{qual}.{child.name}')
    visit(tree, '<module>')
    return out


def _child_defs(node: ast.AST) -> list[ast.AST]:
    """Defs directly owned by node (through any non-def statements, not through nested defs)."""
    found: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        n: ast.AST = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(n)
        else:
            stack.extend(ast.iter_child_nodes(n))
    return sorted(found, key=lambda d: d.lineno)


def scan_python(source: str, rel: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(identifier occurrences, SQL table accesses). Unparseable files degrade to a text scan."""
    try:
        tree: ast.Module = ast.parse(source)
    except SyntaxError:
        return scan_text(source, rel), []
    docs: set[int] = _docstring_ids(tree)
    occ: list[dict[str, Any]] = []
    for n in ast.walk(tree):
        line: int = getattr(n, 'lineno', 0)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            occ.append(_occ(n.name, 'def', rel, line))
        elif isinstance(n, ast.arg):
            occ.append(_occ(n.arg, 'def', rel, line))
        elif isinstance(n, ast.Name):
            occ.append(_occ(n.id, 'ref', rel, line))
        elif isinstance(n, ast.Attribute):
            occ.append(_occ(n.attr, 'ref', rel, line))
        elif isinstance(n, ast.keyword) and n.arg:
            occ.append(_occ(n.arg, 'ref', rel, line))
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            mods: list[str] = [getattr(n, 'module', None) or ''] + [x for a in n.names for x in (a.name, a.asname or '')]
            occ += [_occ(t, 'import', rel, line) for m in mods for t in TOKEN.findall(m)]
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            kind: str = 'doc' if id(n) in docs else 'string'
            occ += [_occ(t, kind, rel, line) for t in sorted(set(TOKEN.findall(n.value)))]
    uniq: dict[tuple[str, str, int], dict[str, Any]] = {(o['token'], o['kind'], o['line']): o for o in occ}
    return list(uniq.values()), _accesses(tree, rel)


def _git(repo: Path, *args: str, data: Optional[bytes] = None) -> bytes:
    return subprocess.run(['git', '-C', str(repo), *args], input=data, check=True, capture_output=True).stdout


def _files(repo: Path, rev: Optional[str], scope: list[str]) -> dict[str, bytes]:
    """{path: content} for every tracked file under scope at rev (or in the working tree when rev is None)."""
    spec: list[str] = [s for s in scope if s not in ('.', '')]
    if rev is None:
        names: list[str] = [p for p in _git(repo, 'ls-files', '-z', '-co', '--exclude-standard', '--', *spec)
                            .decode('utf-8').split('\0') if p]
        return {p: (repo / p).read_bytes() for p in names if (repo / p).is_file()}
    names = [p for p in _git(repo, 'ls-tree', '-r', '-z', '--name-only', rev, '--', *spec).decode('utf-8').split('\0') if p]
    out: dict[str, bytes] = {}
    raw: bytes = _git(repo, 'cat-file', '--batch', data=''.join(f'{rev}:{p}\n' for p in names).encode('utf-8'))
    pos: int = 0
    for p in names:
        nl: int = raw.index(b'\n', pos)
        head: list[bytes] = raw[pos:nl].split()
        if len(head) != 3:  # '<obj> missing' (e.g. a submodule entry)
            pos = nl + 1
            continue
        size: int = int(head[2])
        out[p] = raw[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return out


def _excluded(path: str, exclude: list[str]) -> bool:
    return any(fnmatch(path, e) or path == e.rstrip('/') or path.startswith(e.rstrip('/') + '/') for e in exclude)


def _allowed(acc: dict[str, Any], globs: list[str]) -> bool:
    return any(fnmatch(acc['file'], g) or fnmatch(acc['where'], g) for g in globs)


def check(contract: Contract, repo: Path, rev: Optional[str]) -> dict[str, Any]:
    files: dict[str, bytes] = _files(repo, rev, contract.scope)
    cov: dict[str, Any] = {'files': 0, 'python': 0, 'text': 0, 'skipped': [], 'parse_errors': []}
    occ: list[dict[str, Any]] = []
    acc: list[dict[str, Any]] = []
    for path in sorted(files):
        if _excluded(path, contract.exclude):
            continue
        blob: bytes = files[path]
        if len(blob) > MAX_BYTES or b'\0' in blob[:8192]:
            cov['skipped'].append(path)
            continue
        text: str = blob.decode('utf-8-sig', errors='replace')  # a BOM is legal Python source but not ast.parse input
        cov['files'] += 1
        if path.endswith('.py'):
            cov['python'] += 1
            o, a = scan_python(text, path)
            if o and all(x['kind'] == 'text' for x in o):
                cov['parse_errors'].append(path)
            occ += o
            acc += a
        else:
            cov['text'] += 1
            occ += scan_text(text, path)

    retired: set[str] = set(contract.retired)
    pats: list[tuple[str, re.Pattern[str]]] = [(p, re.compile(p)) for p in contract.retired_patterns]
    areas: list[tuple[str, str, re.Pattern[str], set[str]]] = [
        (a, f.pattern, re.compile(f.pattern), set(f.kinds)) for a, fs in contract.forbidden_in.items() for f in fs]
    hits: list[dict[str, Any]] = []
    for o in occ:
        rules: list[str] = ([f'retired:{o["token"]}'] if o['token'] in retired else []) + \
                           [f'retired_pattern:{p}' for p, rx in pats if rx.search(o['token'])] + \
                           [f'forbidden_in:{a}:{p}' for a, p, rx, ks in areas
                            if o['kind'] in ks and _excluded(o['file'], [a]) and rx.search(o['token'])]
        hits += [{'rule': r, **o} for r in rules]
    stores: dict[str, StoreRule] = {k.lower(): v for k, v in contract.stores.items()}
    for a in acc:
        rule: Optional[StoreRule] = stores.get(a['table'])
        globs: Optional[list[str]] = None if rule is None else (rule.writers if a['mode'] == 'write' else rule.readers)
        if globs is not None and not _allowed(a, globs):
            role: str = 'writers' if a['mode'] == 'write' else 'readers'
            hits.append({'rule': f'store:{a["table"]}:{role}', 'token': a['table'], 'kind': a['mode'],
                         'file': a['file'], 'line': a['line'], 'where': a['where']})

    violations: list[dict[str, Any]] = []
    waived: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for h in sorted(hits, key=lambda h: (h['rule'], h['file'], h['line'], h['kind'])):
        w: Optional[Waiver] = next((w for w in contract.waivers
                                    if fnmatch(h['rule'], w.match) and fnmatch(h['file'], w.path)), None)
        if h['kind'] == 'doc':
            notes.append(h)
        elif w is not None:
            waived.append({**h, 'reason': w.reason})
        else:
            violations.append(h)
    return {'ok': not violations, 'rev': rev, 'violations': violations, 'waived': waived, 'notes': notes,
            'coverage': cov, 'limits': LIMITS}


def _report(r: dict[str, Any], contract_path: str, per_rule: int) -> str:
    cov: dict[str, Any] = r['coverage']
    lines: list[str] = [
        f'contract: {contract_path}',
        f'revision: {r["rev_sha"]}',
        f'scanned:  {cov["files"]} files ({cov["python"]} python, {cov["text"]} text); '
        f'skipped {len(cov["skipped"])} binary/oversize; {len(cov["parse_errors"])} python files unparseable (text-scanned)',
        '',
        ('CONTRACT MET' if r['ok'] else f'CONTRACT NOT MET: {len(r["violations"])} violation(s)')
        + f'; {len(r["waived"])} waived; {len(r["notes"])} docstring note(s)',
    ]
    by_rule: dict[str, list[dict[str, Any]]] = {}
    for v in r['violations']:
        by_rule.setdefault(v['rule'], []).append(v)
    for rule, vs in by_rule.items():
        files: list[str] = sorted({v['file'] for v in vs})
        lines += ['', f'{rule}  ({len(vs)} in {len(files)} file(s))']
        lines += [f'  {v["file"]}:{v["line"]}  {v["kind"]}' + (f'  {v["where"]}' if 'where' in v else '') for v in vs[:per_rule]]
        if len(vs) > per_rule:
            lines.append(f'  ... +{len(vs) - per_rule} more (see --json)')
    if r['waived']:
        lines += ['', 'waived:']
        reasons: dict[tuple[str, str], int] = {}
        for w in r['waived']:
            reasons[(w['rule'], w['reason'])] = reasons.get((w['rule'], w['reason']), 0) + 1
        lines += [f'  {rule}: {n} occurrence(s) - {reason}' for (rule, reason), n in sorted(reasons.items())]
    lines += ['', 'limits:'] + [f'  - {x}' for x in r['limits']]
    return '\n'.join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--contract', required=True, help='packet .md with one ```arch-contract block, or a .yaml file')
    ap.add_argument('--repo', default='.')
    ap.add_argument('--rev', default=None, help='git revision to check (default: working tree)')
    ap.add_argument('--json', default=None, help='write the full result as JSON here')
    ap.add_argument('--per-rule', type=int, default=25, help='max occurrences printed per rule')
    args = ap.parse_args(argv)
    repo: Path = Path(args.repo).resolve()
    try:
        contract: Contract = load_contract(Path(args.contract))
        r: dict[str, Any] = check(contract, repo, args.rev)
        r['rev_sha'] = (_git(repo, 'rev-parse', args.rev).decode().strip() if args.rev else 'working tree')
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(f'contract check error: {e}', file=sys.stderr)
        return 2
    print(_report(r, args.contract, args.per_rule))
    if args.json:
        Path(args.json).write_text(json.dumps(r, indent=1), encoding='utf-8')
    return 0 if r['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
