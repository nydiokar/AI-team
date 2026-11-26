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
import hashlib
import json
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pyast_adapter import sql_analysis, sql_bindings  # noqa: E402
from run_result import AuditRun, RunError, atomic_json  # noqa: E402

TOKEN: re.Pattern[str] = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
BLOCK: re.Pattern[str] = re.compile(r'^```arch-contract[ \t]*\n(.*?)^```', re.M | re.S)
MAX_BYTES: int = 2_000_000
LIMITS: list[str] = [
    'Static only: names built at runtime (string concatenation, computed getattr) are not seen.',
    'SQL checks cover supported static statements at execution sites; unresolved forms are listed in coverage.',
    'Python comments are ignored; docstring mentions are listed as notes, not violations.',
]


class StoreRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    writers: Optional[list[str]] = None
    readers: Optional[list[str]] = None

    @model_validator(mode='after')
    def _requires_constraint(self) -> StoreRule:
        if self.readers is None and self.writers is None:
            raise ValueError('store rule needs an explicit reader or writer constraint')
        return self


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
        if not (self.retired or self.retired_patterns or any(self.forbidden_in.values()) or self.stores):
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


def _accesses(tree: ast.Module, rel: str, sql_limits: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    bindings = sql_bindings(tree)

    def visit(node: ast.AST, qual: str) -> None:
        accesses, limits = sql_analysis(node, bindings)
        for access in accesses:
            out.append({'where': f'{rel}::{qual}', 'file': rel, **access})
        if sql_limits is not None:
            sql_limits.extend({'file': rel, 'function': qual, **item} for item in limits)
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


def scan_python(
    source: str, rel: str, *, parse_errors: list[str] | None = None,
    sql_limits: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Token and SQL evidence. A text fallback contributes diagnostics only."""
    try:
        tree: ast.Module = ast.parse(source, filename=rel)
    except SyntaxError:
        if parse_errors is not None:
            parse_errors.append(rel)
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
    return list(uniq.values()), _accesses(tree, rel, sql_limits)


def _git(repo: Path, *args: str, data: Optional[bytes] = None) -> bytes:
    return subprocess.run(['git', '-C', str(repo), *args], input=data, check=True, capture_output=True, timeout=60).stdout


def _files(repo: Path, rev: Optional[str], scope: list[str]) -> Iterator[tuple[str, bytes | None]]:
    """Bounded blobs for every selected path. None denotes unsupported input."""
    spec: list[str] = [] if '.' in scope else scope
    if rev is None:
        names: list[str] = [p for p in _git(repo, '--literal-pathspecs', 'ls-files', '-z', '-co', '--exclude-standard', '--', *spec)
                            .decode('utf-8').split('\0') if p]
        for rel in sorted(set(names)):
            path: Path = repo / rel
            if path.is_symlink() or not path.resolve().is_relative_to(repo):
                yield rel, None
            elif path.is_file():
                with path.open('rb') as stream:
                    data: bytes = stream.read(MAX_BYTES + 1)
                yield rel, data if len(data) <= MAX_BYTES else None
            elif path.exists():
                yield rel, None
        return
    records: list[str] = _git(
        repo, '--literal-pathspecs', 'ls-tree', '-r', '-z', '-l', rev, '--', *spec,
    ).decode().split('\0')
    entries: list[tuple[str, str | None]] = []
    for record in filter(None, records):
        meta, path = record.split('\t', 1)
        mode, object_type, oid, size = meta.split()
        allowed = object_type == 'blob' and mode in ('100644', '100755') and int(size) <= MAX_BYTES
        entries.append((path, oid if allowed else None))
    entries.sort()
    for start in range(0, len(entries), 16):
        batch = entries[start:start + 16]
        oids = ''.join(f'{oid}\n' for _, oid in batch if oid is not None).encode('ascii')
        raw: bytes = _git(repo, 'cat-file', '--batch', data=oids) if oids else b''
        pos: int = 0
        for path, oid in batch:
            if oid is None:
                yield path, None
                continue
            nl: int = raw.index(b'\n', pos)
            head: list[bytes] = raw[pos:nl].split()
            if len(head) != 3 or head[:2] != [oid.encode(), b'blob']:
                raise ValueError(f'Git blob unavailable: {path}')
            blob_end: int = nl + 1 + int(head[2])
            yield path, raw[nl + 1:blob_end]
            pos = blob_end + 1


def _excluded(path: str, exclude: list[str]) -> bool:
    return any(fnmatch(path, e) or path == e.rstrip('/') or path.startswith(e.rstrip('/') + '/') for e in exclude)


def _allowed(acc: dict[str, Any], globs: list[str]) -> bool:
    return any(fnmatch(acc['file'], g) or fnmatch(acc['where'], g) for g in globs)


def check(
    contract: Contract, repo: Path, rev: Optional[str], *, run: AuditRun | None = None,
    generated_report: str | None = None,
) -> dict[str, Any]:
    run = run or AuditRun()
    run.requested_scope = contract.scope
    run.stage = 'coverage'
    scope: list[str] = [
        PurePosixPath(path).as_posix() for path in contract.scope
        if path.strip() and not PurePosixPath(path).is_absolute() and '..' not in PurePosixPath(path).parts
    ]
    files = _files(repo, rev, scope) if scope else iter(())
    cov: dict[str, Any] = {'files': 0, 'python': 0, 'text': 0, 'skipped': [], 'parse_errors': [],
                           'excluded': [], 'uncovered_scopes': [], 'uncovered_forbidden_areas': [],
                           'sql_limitations': [], 'generated_report': generated_report}
    occ: list[dict[str, Any]] = []
    acc: list[dict[str, Any]] = []
    scanned: list[str] = []
    source = hashlib.sha256()
    for path, blob in files:
        if path == generated_report or _excluded(path, contract.exclude):
            cov['excluded'].append(path)
            continue
        if blob is None:
            cov['skipped'].append(path)
            continue
        source.update(json.dumps(path).encode() + b'\0' + hashlib.sha256(blob).digest())
        if len(blob) > MAX_BYTES or b'\0' in blob:
            cov['skipped'].append(path)
            continue
        try:
            text: str = blob.decode('utf-8-sig')  # Strip a legal Python BOM before AST parsing.
        except UnicodeError:
            cov['skipped'].append(path)
            continue
        scanned.append(path)
        cov['files'] += 1
        if path.endswith(('.py', '.pyi')):
            cov['python'] += 1
            o, a = scan_python(text, path, parse_errors=cov['parse_errors'], sql_limits=cov['sql_limitations'])
            occ += o
            acc += a
        else:
            cov['text'] += 1
            occ += scan_text(text, path)

    run.source_sha256 = source.hexdigest()
    run.effective_scope = [s for s in scope if any(s == '.' or p == s or p.startswith(s + '/') for p in scanned)]
    cov['uncovered_scopes'] = [
        path for path in contract.scope if PurePosixPath(path).as_posix() not in run.effective_scope or not path.strip()
    ]
    cov['uncovered_forbidden_areas'] = [a for a in contract.forbidden_in if not any(_excluded(p, [a]) for p in scanned)]
    if not scanned:
        run.errors.append(RunError(stage='coverage', detail='no files scanned'))
    for kind in ('uncovered_scopes', 'uncovered_forbidden_areas', 'skipped', 'parse_errors'):
        if cov[kind]:
            run.errors.append(RunError(stage='coverage', detail=f'{kind}: {", ".join(cov[kind])}'))

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
                         'file': a['file'], 'line': a['line'], 'where': a['where'],
                         'declaration_lines': a['declaration_lines'], 'dynamic': a['dynamic']})

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
    run.status = 'incomplete' if run.errors else 'completed'
    run.stage = 'coverage' if run.errors else 'assertions'
    return {'ok': not violations and run.status == 'completed', 'status': run.status, 'run': run.model_dump(mode='json'),
            'rev': rev, 'violations': violations, 'waived': waived, 'notes': notes, 'coverage': cov, 'limits': LIMITS}


def _report(r: dict[str, Any], contract_path: str, per_rule: int) -> str:
    cov: dict[str, Any] = r['coverage']
    lines: list[str] = [
        f'contract: {contract_path}',
        f'revision: {r["rev_sha"]}',
        f'scanned:  {cov["files"]} files ({cov["python"]} python, {cov["text"]} text); '
        f'skipped {len(cov["skipped"])} unsupported input; {len(cov["parse_errors"])} Python parse errors; '
        f'{len(cov["sql_limitations"])} unresolved SQL sites (see --json)',
        '',
        ('ANALYSIS INCOMPLETE' if r['status'] == 'incomplete' else
         'CONTRACT MET' if r['ok'] else f'CONTRACT NOT MET: {len(r["violations"])} violation(s)')
        + f'; {len(r["waived"])} waived; {len(r["notes"])} docstring note(s)',
    ]
    lines += [f'  {error["stage"]}: {error["detail"]}' for error in r['run']['errors']]
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
    run = AuditRun()
    output: Path | None = Path(args.json) if args.json else None
    def publish(result: dict[str, Any]) -> None:
        if output is not None:
            atomic_json(output, result)
    try:
        if output and output.resolve() == Path(args.contract).resolve():
            raise ValueError('report and contract must use distinct paths')
        publish({'ok': False, 'status': run.status, 'run': run.model_dump(mode='json')})
        contract: Contract = load_contract(Path(args.contract))
        run.requested_scope = contract.scope
        revision: str | None = (_git(repo, 'rev-parse', '--verify', '--end-of-options', args.rev)
                                .decode().strip()) if args.rev else None
        generated: str | None = (
            output.resolve().relative_to(repo).as_posix()
            if revision is None and output and output.resolve().is_relative_to(repo) else None
        )
        r: dict[str, Any] = check(contract, repo, revision, run=run, generated_report=generated)
        r['rev'] = args.rev
        r['rev_sha'] = revision or 'working tree'
        publish(r)
    except (ValueError, OSError, subprocess.SubprocessError, yaml.YAMLError) as e:
        run.fail(run.stage, str(e))
        if output and output.resolve() != Path(args.contract).resolve():
            try:
                publish({'ok': False, 'status': run.status, 'run': run.model_dump(mode='json')})
            except OSError as publish_error:
                print(f'contract report error: {publish_error}', file=sys.stderr)
        print(f'contract check error: {e}', file=sys.stderr)
        return 2
    print(_report(r, args.contract, args.per_rule))
    return 0 if r['ok'] else 2 if r['status'] == 'incomplete' else 1


if __name__ == '__main__':
    sys.exit(main())
