#!/usr/bin/env python3
"""Interactive execution-path explorer backed by Codebase Memory MCP (CBM) + a Python AST adapter.

* CBM is queried through ONE long-lived MCP stdio session (warm queries ~30 ms).
* Nodes are identified by CBM `qualified_name` (CBM 0.11 rejects `id()` in WHERE).
* API routes come from a generic FastAPI AST adapter (CBM misses nested handlers and
  include_router prefixes); handlers are linked to CBM symbols by file + definition line.
* Every expansion (node, direction) is cached on disk and invalidated per changed file.
* Structural findings are deterministic graph rules; no LLM decides what exists.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cbm_client import CBMClient, CBMError, cypher_str, find_executable  # noqa: E402
from pyast_adapter import call_sites, compose_routes, find_def, module_name, parse_file, resolve_module  # noqa: E402
from structure import FLOW, LOW_CONFIDENCE, analyze  # noqa: E402

SCHEMA: int = 9
OUT_RELS: str = 'CALLS|ASYNC_CALLS|HTTP_CALLS|SPAWNS|CALL_REFERENCE|HANDLES|CONFIGURES|WRITES'
NODE_FIELDS: str = '{v}.qualified_name AS q, labels({v}) AS l, {v}.name AS n, {v}.file_path AS f, ' \
                   '{v}.start_line AS s, {v}.end_line AS e'
CODE_EXTS: frozenset[str] = frozenset({'.py', '.pyi', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.json', '.yaml',
                                       '.yml', '.toml', '.ini', '.cfg', '.sh', '.ps1', '.sql'})
SKIP_DIRS: frozenset[str] = frozenset({'.git', 'node_modules', '.next', 'dist', 'build', '.venv', 'venv', '__pycache__',
                                       '.arch-audit', '.claude', '.cache', 'coverage', '.pytest_cache', 'target',
                                       '.codebase-memory'})
BUILTIN_TYPE_METHODS: frozenset[str] = frozenset(
    n for t in (dict, list, str, set, tuple, bytes, frozenset, int, float) for n in dir(t) if not n.startswith('_'))
BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))
SPAWN_KINDS: frozenset[str] = frozenset({'spawned_call', 'spawn_ref'})
MAX_CANDIDATE_EDGES: int = 6


def log(msg: str) -> None:
    print('[pathway]', msg, flush=True)


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _labels(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    try:
        parsed: Any = json.loads(v)
        return [str(x) for x in parsed] if isinstance(parsed, list) else [str(parsed)]
    except (TypeError, ValueError):
        return [str(v)] if v else []


def _props(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    try:
        p: Any = json.loads(v) if v else {}
        return p if isinstance(p, dict) else {}
    except (TypeError, ValueError):
        return {}


def _low(props: dict[str, Any]) -> bool:
    """CBM marks name-guessed resolutions with low confidence (suffix_match 0.09/0.21 observed wrong)."""
    try:
        return float(props.get('confidence', 1)) < LOW_CONFIDENCE
    except (TypeError, ValueError):
        return False


def scan_files(scope: Path) -> dict[str, tuple[int, int]]:
    """rel -> (size, mtime_ns) for every code file under scope."""
    out: dict[str, tuple[int, int]] = {}
    for directory, dirs, files in os.walk(scope):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            p: Path = Path(directory) / name
            if p.suffix.lower() not in CODE_EXTS:
                continue
            try:
                st: os.stat_result = p.stat()
            except OSError:
                continue
            out[p.relative_to(scope).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cbm_node(row: dict[str, Any]) -> dict[str, Any]:
    labels: list[str] = _labels(row.get('l'))
    kind: str = labels[-1] if labels else 'Unknown'
    f: str = str(row.get('f') or '')
    qn: str = str(row.get('q') or '')
    if f.startswith('<') or qn.startswith('builtins.'):
        kind = 'External'
    if kind == 'Route':  # CBM Route nodes reached via HTTP_CALLS are client-side URL targets
        m: Optional[re.Match[str]] = re.match(r'__route__([A-Z]+)__(.*)', qn)
        label: str = f'{m.group(1)} {m.group(2)}' if m else qn
    else:
        label = str(row.get('n') or qn.rsplit('.', 1)[-1])
    if kind == 'Module':
        label = f
    comp: str = qn.rsplit('.', 1)[0] if kind in ('Method', 'Function', 'Variable', 'Class') else qn
    return {'id': qn, 'kind': kind, 'label': label, 'qn': qn, 'file': f if not f.startswith('<') else '',
            'line': _int(row.get('s')), 'end_line': _int(row.get('e')), 'component': comp, 'source': 'cbm'}


class Engine:
    """Holds the scoped graph cache and serves bounded on-demand expansions (shared by HTTP threads)."""

    def __init__(self, scope: Path, project: str, cache_dir: Path, out: Path, report: Path,
                 cbm: Optional[CBMClient], page: int = 200) -> None:
        self.scope: Path = scope.resolve()
        self.project: str = project
        self.dir: Path = cache_dir.resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache_path: Path = self.dir / 'pathway_cache.json'
        self.out: Path = out
        self.report: Path = report
        self.cbm: Optional[CBMClient] = cbm
        self.baseline_path: Optional[Path] = None
        self.page: int = page
        self.lock: threading.RLock = threading.RLock()
        self.ready: threading.Event = threading.Event()
        self.ready_error: Optional[str] = None
        self.stats: dict[str, Any] = {'cache_hits': 0, 'cache_misses': 0, 'index_runs': 0, 'index_seconds': 0.0,
                                      'expansions': 0, 'last_expand_ms': None, 'ast_reparsed_files': 0}
        self.trees: dict[str, ast.Module] = {}
        self.nested_index: Optional[dict[str, list[dict[str, Any]]]] = None
        self.version: int = 0
        self._findings_memo: tuple[int, dict[str, Any]] = (-1, {})
        self._medium_memo: Optional[tuple[tuple[int, int], dict[str, dict[str, Any]], list[dict[str, Any]]]] = None
        self.cache: dict[str, Any] = self._load()
        self.changed: set[str] = set()
        self.inventory: dict[str, Any] = {}

    # ------------------------------------------------------------------ cache
    def _load(self) -> dict[str, Any]:
        empty: dict[str, Any] = {'schema': SCHEMA, 'project': self.project, 'scope': str(self.scope), 'files': {},
                                 'facts': {}, 'entries': {}, 'nodes': {}, 'symbols': None, 'fanin': None,
                                 'cbm_routes': None, 'index': None, 'index_token': 0}
        try:
            d: dict[str, Any] = json.loads(self.cache_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return empty
        if d.get('schema') != SCHEMA or d.get('project') != self.project or d.get('scope') != str(self.scope):
            return empty
        return d

    def persist(self) -> None:
        tmp: Path = self.cache_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        tmp.replace(self.cache_path)
        st: dict[str, Any] = self.state()
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.write_text(json.dumps({'project': self.project, 'scope': str(self.scope),
                                           'coverage': st['coverage'], 'analysis_scope': st['analysis_scope'],
                                           'findings': st['findings'], 'stats': st['stats'],
                                           'routes': st['routes']}, ensure_ascii=False, indent=1), encoding='utf-8')
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(self.render_html(st), encoding='utf-8')

    def render_html(self, st: Optional[dict[str, Any]] = None) -> str:
        template: str = Path(__file__).with_name('viewer.html').read_text(encoding='utf-8')
        payload: str = json.dumps(st or self.state(), ensure_ascii=False).replace('<', '\\u003c')
        return template.replace('/*__INITIAL_STATE__*/null', payload)

    # ------------------------------------------------------- source tracking
    def sync_files(self) -> set[str]:
        """Detect added/removed/content-changed files; re-parse only changed Python files."""
        now: dict[str, tuple[int, int]] = scan_files(self.scope)
        old: dict[str, dict[str, Any]] = self.cache['files']
        changed: set[str] = set(old) - set(now)
        new_files: dict[str, dict[str, Any]] = {}
        for rel, (size, mtime) in now.items():
            prev: Optional[dict[str, Any]] = old.get(rel)
            if prev and prev['size'] == size and prev['mtime_ns'] == mtime:
                new_files[rel] = prev
                continue
            digest: str = sha256(self.scope / rel)
            new_files[rel] = {'size': size, 'mtime_ns': mtime, 'sha': digest}
            if not prev or prev['sha'] != digest:
                changed.add(rel)
        self.cache['files'] = new_files
        facts: dict[str, Any] = self.cache['facts']
        for rel in sorted(changed | {r for r in new_files if r.endswith('.py') and r not in facts}):
            facts.pop(rel, None)
            self.trees.pop(rel, None)
            if rel.endswith('.py') and rel in new_files:
                try:
                    facts[rel] = parse_file((self.scope / rel).read_text(encoding='utf-8-sig'), rel)
                except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
                    facts[rel] = {'module': module_name(rel), 'imports': {}, 'defs': [], 'routers': {}, 'routes': [],
                                  'includes': [], 'returns': {}, 'mounts': [], 'router_args': [],
                                  'parse_error': f'{type(exc).__name__}: {exc}'}
                self.stats['ast_reparsed_files'] += 1
        self.changed = changed
        self.inventory = compose_routes({r: f for r, f in facts.items()})
        return changed

    def invalidate(self, changed: set[str]) -> int:
        """Drop cached expansions that may be affected by changed files.

        * an 'out' entry is dropped if its node or any neighbour lives in a changed file, or it had
          unresolved calls (a new definition may now resolve them);
        * every 'in' entry is dropped: a new caller can appear in any changed file;
        * global CBM tables (symbols, fan-in, routes) are re-read lazily.
        """
        if not changed:
            return 0
        entries: dict[str, Any] = self.cache['entries']
        drop: list[str] = [k for k, e in entries.items()
                           if k.startswith('in|') or set(e['files']) & changed or e.get('unresolved')]
        for k in drop:
            del entries[k]
        self.cache['symbols'] = self.cache['fanin'] = self.cache['cbm_routes'] = None
        self.cache['nodes'] = {k: v for k, v in self.cache['nodes'].items() if v.get('file') not in changed}
        self.nested_index = None
        self.version += 1
        return len(drop)

    def ensure_index(self) -> None:
        """Background: (re)index the scoped CBM project only when needed, then mark ready."""
        try:
            assert self.cbm is not None
            self.cbm.ensure_started()
            need: bool = bool(self.changed) or not self.cache.get('index')
            if not need:
                try:
                    st: Any = self.cbm.tool('index_status', {'project': self.project, 'format': 'json'})
                    need = str((st or {}).get('status')) not in ('ready', 'indexed')
                except CBMError:
                    need = True
            if need:
                t0: float = time.perf_counter()
                log(f'Indexing ONLY {self.scope} into CBM project {self.project} '
                    f'({len(self.changed)} changed files)...')
                res: Any = self.cbm.tool('index_repository', {'repo_path': str(self.scope), 'name': self.project},
                                         timeout=1800)
                dt: float = time.perf_counter() - t0
                with self.lock:
                    self.stats['index_runs'] += 1
                    self.stats['index_seconds'] += dt
                    self.cache['index'] = {'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'seconds': round(dt, 2),
                                           'nodes': (res or {}).get('nodes'), 'edges': (res or {}).get('edges')}
                    self.cache['index_token'] = int(self.cache.get('index_token') or 0) + 1
                    self.cache['symbols'] = self.cache['fanin'] = self.cache['cbm_routes'] = None
                    self.persist()
                log(f'CBM index ready in {dt:.1f}s: {(res or {}).get("nodes")} nodes, {(res or {}).get("edges")} edges')
            else:
                log('CBM index reused (no source changes since last run).')
            with self.lock:  # warm the global tables so the first click is not the slow one
                t1: float = time.perf_counter()
                self.ready.set()
                self.symbols()
                self.fanin()
                self.cbm_routes()
                self.nested_callers()
                self.version += 1
                log(f'CBM statistics + nested-call index warmed in {time.perf_counter() - t1:.1f}s')
        except (CBMError, OSError, AssertionError) as exc:
            self.ready_error = f'CBM unavailable: {exc}'
            log('ERROR ' + self.ready_error)
        finally:
            self.ready.set()

    def _need_cbm(self) -> CBMClient:
        if not self.ready.wait(1800):
            raise CBMError('CBM index is still building')
        if self.ready_error or self.cbm is None:
            raise CBMError(self.ready_error or 'CBM disabled')
        return self.cbm

    def _q(self, cypher: str, page: Optional[int] = None) -> tuple[list[dict[str, Any]], bool]:
        return self._need_cbm().query(self.project, cypher, page or self.page)

    # ----------------------------------------------------- global CBM tables
    def symbols(self) -> dict[str, Any]:
        if self.cache.get('symbols') is None:
            rows, ok = self._q('MATCH (n) WHERE n:Function OR n:Method OR n:Class OR n:Module RETURN '
                               + NODE_FIELDS.format(v='n'), page=20000)
            if not ok:
                raise CBMError('symbol table exceeded the paging cap')
            self.cache['symbols'] = [cbm_node(r) for r in rows]
        return self._symbol_index()

    def _symbol_index(self) -> dict[str, Any]:
        memo: Any = getattr(self, '_sym_memo', None)
        if memo and memo[0] is self.cache['symbols']:
            return memo[1]
        by_qn: dict[str, dict[str, Any]] = {}
        by_line: dict[tuple[str, int], str] = {}
        by_name: dict[str, list[str]] = {}
        for n in self.cache['symbols']:
            by_qn[n['qn']] = n
            if n['kind'] == 'External':
                continue
            if n['line'] is not None and n['kind'] != 'Module':
                by_line[(n['file'], n['line'])] = n['qn']
            if n['kind'] in ('Function', 'Method'):
                by_name.setdefault(n['label'], []).append(n['qn'])
            if n['kind'] == 'Module':
                by_line[(n['file'], 0)] = n['qn']
        idx: dict[str, Any] = {'by_qn': by_qn, 'by_line': by_line, 'by_name': by_name}
        self._sym_memo = (self.cache['symbols'], idx)
        return idx

    def fanin(self) -> dict[str, int]:
        if self.cache.get('fanin') is None:
            rows, ok = self._q('MATCH (a)-[r:CALLS]->(b) RETURN b.qualified_name AS q, count(r) AS c', page=20000)
            self.cache['fanin'] = {r['q']: _int(r['c']) or 0 for r in rows}
        return self.cache['fanin']

    def utility_threshold(self) -> int:
        vals: list[int] = sorted(v for k, v in (self.cache.get('fanin') or {}).items() if not k.startswith('builtins.'))
        return max(10, vals[int(len(vals) * 0.97)] if vals else 10)

    def orphan_roots(self) -> list[str]:
        """Public callables with no inbound CALLS edge: process/loop entry points and interface-dispatch targets."""
        fan: dict[str, int] = self.fanin()
        return sorted(n['qn'] for n in self.cache['symbols']
                      if n['kind'] in ('Function', 'Method') and not n['label'].startswith('_') and n['qn'] not in fan)

    def cbm_routes(self) -> list[dict[str, Any]]:
        if self.cache.get('cbm_routes') is None:
            rows, _ = self._q('MATCH (r:Route) RETURN r.qualified_name AS q', page=20000)
            client, _ = self._q('MATCH (a)-[:HTTP_CALLS]->(r:Route) RETURN DISTINCT r.qualified_name AS q', page=20000)
            cs: set[str] = {r['q'] for r in client}
            self.cache['cbm_routes'] = [{'qn': r['q'], 'client_target': r['q'] in cs} for r in rows]
        return self.cache['cbm_routes']

    # ------------------------------------------------------------- AST nodes
    def _tree(self, rel: str) -> Optional[ast.Module]:
        if rel not in self.trees:
            try:
                self.trees[rel] = ast.parse((self.scope / rel).read_text(encoding='utf-8-sig'), filename=rel)
            except (OSError, SyntaxError, ValueError):
                return None
        return self.trees[rel]

    def _facts_by_module(self) -> dict[str, tuple[str, dict[str, Any]]]:
        return {f['module']: (rel, f) for rel, f in self.cache['facts'].items()}

    def def_node(self, rel: str, d: dict[str, Any]) -> dict[str, Any]:
        """Graph node for an AST definition: the CBM symbol when CBM indexed it, else an AST node."""
        sym: dict[str, Any] = self.symbols()
        qn: Optional[str] = None if d['nested'] else sym['by_line'].get((rel, d['line']))
        if qn:
            n: dict[str, Any] = dict(sym['by_qn'][qn])
            n['async'] = d['async']
            return n
        mod: str = self.cache['facts'][rel]['module']
        parent_def: Optional[dict[str, Any]] = next((x for x in self.cache['facts'][rel]['defs']
                                                     if x['qual'] == d['parent']), None)
        comp: str = (self.def_node(rel, parent_def)['id'] if parent_def else f'{self.project}.{mod}')
        return {'id': f'ast::{mod}.{d["qual"]}', 'kind': 'NestedFunction' if d['nested'] else 'Function',
                'label': d['name'], 'qn': f'{mod}.{d["qual"]}', 'file': rel, 'line': d['line'],
                'end_line': d['end_line'], 'async': d['async'], 'component': comp, 'source': 'ast'}

    def module_node(self, rel: str) -> dict[str, Any]:
        sym: dict[str, Any] = self.symbols()
        qn: Optional[str] = sym['by_line'].get((rel, 0))
        if qn:
            return dict(sym['by_qn'][qn])
        return {'id': f'ast::{module_name(rel)}', 'kind': 'Module', 'label': rel, 'qn': module_name(rel), 'file': rel,
                'line': 1, 'end_line': None, 'component': module_name(rel), 'source': 'ast'}

    def scope_node(self, mod: str, qual: str) -> Optional[dict[str, Any]]:
        bm: dict[str, tuple[str, dict[str, Any]]] = self._facts_by_module()
        if mod not in bm:
            return None
        rel, f = bm[mod]
        if not qual:
            return self.module_node(rel)
        d: Optional[dict[str, Any]] = next((x for x in f['defs'] if x['qual'] == qual), None)
        return self.def_node(rel, d) if d else None

    def resolve_call(self, rel: str, scope: str, call: dict[str, Any]) -> dict[str, Any]:
        """Deterministically resolve one AST call site to a graph node, or classify why not."""
        if call['kind'] == 'dynamic':
            return {'status': 'dynamic'}
        facts: dict[str, Any] = self.cache['facts'][rel]
        bm: dict[str, tuple[str, dict[str, Any]]] = self._facts_by_module()
        sym: dict[str, Any] = self.symbols()
        text: str = call['text']
        head, _, rest = text.partition('.')
        defs: dict[str, dict[str, Any]] = {d['qual']: d for d in facts['defs']}
        chain: list[str] = [scope]
        while chain[-1]:
            chain.append(chain[-1].rpartition('.')[0])
        if not rest:
            for s in chain:
                q: str = f'{s}.{head}' if s else head
                if q in defs:
                    return {'status': 'resolved', 'node': self.def_node(rel, defs[q]), 'strategy': 'lexical_scope'}
        if head in ('self', 'cls') and rest and '.' not in rest:
            cls: Optional[str] = next((s for s in chain if s in defs and defs[s]['kind'] == 'class'), None)
            if cls and f'{cls}.{rest}' in defs:
                return {'status': 'resolved', 'node': self.def_node(rel, defs[f'{cls}.{rest}']), 'strategy': 'self_method'}
        target: Optional[str] = facts['imports'].get(head)
        if target:
            hit: Optional[tuple[str, str]] = resolve_module(f'{target}.{rest}' if rest else target, set(bm))
            if hit:
                hrel, hf = bm[hit[0]]
                hd: Optional[dict[str, Any]] = next((x for x in hf['defs'] if x['qual'] == hit[1]), None)
                if hd:
                    return {'status': 'resolved', 'node': self.def_node(hrel, hd), 'strategy': 'import'}
                if not hit[1]:
                    return {'status': 'unresolved', 'reason': 'module object called'}
                attr_name: str = hit[1].split('.')[-1]
                if '.' not in hit[1] and not any(x['name'] == attr_name for x in hf['defs']):
                    return {'status': 'unresolved', 'reason': f'{hit[0]}.{hit[1]} is not a definition (variable/re-export)'}
            else:
                return {'status': 'external', 'reason': f'imported from {target}'}
        if not rest:
            if head in BUILTIN_NAMES:
                return {'status': 'builtin'}
            return {'status': 'unresolved', 'reason': 'local/callback name not bound to a definition'}
        name: str = call['name']
        cands: list[str] = sym['by_name'].get(name, [])
        # Receiver-name narrowing: `orchestrator.x()` -> the only candidate whose owning class name
        # contains the receiver identifier (TaskOrchestrator.x). Deterministic, labelled lower-confidence.
        recv: str = text.split('.')[-2].strip('_').replace('_', '').lower()
        owned: list[str] = [c for c in cands if len(recv) >= 4 and sym['by_qn'][c]['kind'] == 'Method'
                            and recv in c.split('.')[-2].lower()]
        if len(cands) > 1 and len(owned) == 1:
            return {'status': 'resolved', 'node': dict(sym['by_qn'][owned[0]]), 'strategy': 'receiver_name_match',
                    'confidence': 0.6}
        if name in BUILTIN_TYPE_METHODS:
            return {'status': 'ambiguous_builtin_name', 'reason': f'.{name}() also exists on builtin types; receiver type unknown',
                    'candidates': cands[:8]}
        if len(cands) == 1:
            return {'status': 'resolved', 'node': dict(sym['by_qn'][cands[0]]), 'strategy': 'unique_method_name'}
        if cands:
            return {'status': 'ambiguous', 'reason': f'{len(cands)} definitions named {name}', 'candidates': cands[:8]}
        return {'status': 'external', 'reason': 'no in-scope definition with this name'}

    def _ast_out(self, rel: str, scope: str, fn: ast.AST, src_id: str,
                 cbm_lines: Optional[dict[int, list[dict[str, Any]]]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]], Counter[str]]:
        """Edges/unresolved for one def body; with cbm_lines, only calls CBM did not already resolve."""
        edges: list[dict[str, Any]] = []
        nodes: dict[str, dict[str, Any]] = {}
        unresolved: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for c in call_sites(fn):
            if cbm_lines is not None:
                same: list[dict[str, Any]] = cbm_lines.get(c['line'], [])
                hit: Optional[dict[str, Any]] = next((e for e in same if str((e.get('props') or {}).get('callee', ''))
                                                     .split('.')[-1] == c['name']), None)
                if hit:
                    if c['awaited']:
                        hit['props']['awaited'] = True
                    if c['kind'] in SPAWN_KINDS:
                        hit['props']['spawned'] = True
                    counts['cbm'] += 1
                    continue
            r: dict[str, Any] = self.resolve_call(rel, scope, c)
            counts[r['status']] += 1
            if r['status'] == 'resolved':
                tgt: dict[str, Any] = r['node']
                nodes[tgt['id']] = tgt
                etype: str = 'SPAWNS' if c['kind'] in SPAWN_KINDS else 'CALLS'
                edges.append({'from': src_id, 'to': tgt['id'], 'type': etype, 'file': rel, 'line': c['line'],
                              'source': 'ast', 'props': {'callee': c['text'], 'strategy': r['strategy'],
                                                         'awaited': c['awaited'],
                                                         **({'confidence': r['confidence']} if 'confidence' in r else {})}})
            elif r['status'] in ('ambiguous', 'ambiguous_builtin_name', 'unresolved', 'dynamic'):
                unresolved.append({'line': c['line'], 'callee': c['text'], 'status': r['status'],
                                   'reason': r.get('reason', ''), 'candidates': r.get('candidates', [])})
                if r['status'] == 'ambiguous' and len(r.get('candidates', [])) <= MAX_CANDIDATE_EDGES:
                    # Possible targets stay navigable but are never treated as facts by the analyzer.
                    for cq in r['candidates']:
                        cn: Optional[dict[str, Any]] = self._symbol_index()['by_qn'].get(cq)
                        if cn:
                            nodes[cq] = dict(cn)
                            edges.append({'from': src_id, 'to': cq, 'type': 'CANDIDATE_CALL', 'file': rel,
                                          'line': c['line'], 'source': 'ast', 'low_confidence': True,
                                          'props': {'callee': c['text'], 'strategy': 'same_name_candidate',
                                                    'candidates': len(r['candidates'])}})
        return edges, nodes, unresolved, counts

    def nested_callers(self) -> dict[str, list[dict[str, Any]]]:
        """Reverse index target -> edges from nested defs (CBM has no edges out of nested bodies)."""
        if self.nested_index is None:
            idx: dict[str, list[dict[str, Any]]] = {}
            for rel, f in sorted(self.cache['facts'].items()):
                tree: Optional[ast.Module] = None
                for d in f['defs']:
                    if not d['nested'] or d['kind'] == 'class':
                        continue
                    tree = tree or self._tree(rel)
                    fn: Optional[ast.AST] = find_def(tree, d['line']) if tree else None
                    if fn is None:
                        continue
                    src: dict[str, Any] = self.def_node(rel, d)
                    edges, _, _, _ = self._ast_out(rel, d['qual'], fn, src['id'], None)
                    for e in edges:
                        idx.setdefault(e['to'], []).append({**e, '_node': src})
            self.nested_index = idx
        return self.nested_index

    # ------------------------------------------------------------- expansion
    def route(self, rid: str) -> Optional[dict[str, Any]]:
        return next((r for r in self.inventory.get('routes', []) if r['id'] == rid), None)

    def route_node(self, r: dict[str, Any]) -> dict[str, Any]:
        return {'id': r['id'], 'kind': 'Route', 'label': f"{r['method']} {r['path']}", 'qn': r['cbm_route_qn'] or '',
                'file': r['file'], 'line': r['line'], 'end_line': r['line'], 'component': r['app'] or 'unmounted',
                'source': 'ast', 'status': r['status']}

    def handler_node(self, r: dict[str, Any]) -> Optional[dict[str, Any]]:
        h: Optional[dict[str, Any]] = r.get('handler')
        if not h:
            return None
        d: Optional[dict[str, Any]] = next((x for x in self.cache['facts'][h['file']]['defs'] if x['qual'] == h['qual']), None)
        return self.def_node(h['file'], d) if d else None

    def _route_entry(self, rid: str, direction: str) -> dict[str, Any]:
        r: Optional[dict[str, Any]] = self.route(rid)
        if not r:
            raise KeyError(f'unknown route {rid}')
        rn: dict[str, Any] = self.route_node(r)
        nodes: dict[str, dict[str, Any]] = {rid: rn}
        edges: list[dict[str, Any]] = []
        if direction == 'out':
            h: Optional[dict[str, Any]] = self.handler_node(r)
            if h:
                nodes[h['id']] = h
                edges.append({'from': rid, 'to': h['id'], 'type': 'HANDLES', 'file': r['file'], 'line': r['line'],
                              'source': 'ast', 'props': {'methods': r['methods']}})
        else:
            reg: Optional[dict[str, Any]] = self.scope_node(r['registrar_module'], r['registrar'])
            prev: Optional[dict[str, Any]] = reg
            if reg:
                nodes[reg['id']] = reg
                edges.append({'from': reg['id'], 'to': rid, 'type': 'REGISTERS', 'file': r['file'], 'line': r['line'],
                              'source': 'ast', 'props': {'raw_path': r['raw_path']}})
            for link in r['mount_chain']:
                comp: Optional[dict[str, Any]] = self.scope_node(link['composer_module'], link['composer'])
                if comp and prev and comp['id'] != prev['id']:
                    nodes[comp['id']] = comp
                    edges.append({'from': comp['id'], 'to': prev['id'], 'type': 'INCLUDES', 'file': link['file'],
                                  'line': link['line'], 'source': 'ast',
                                  'props': {'prefix': link['prefix'], 'via': link['via']}})
                prev = comp or prev
        return {'nodes': nodes, 'edges': edges, 'unresolved': [], 'complete': True, 'counts': {}}

    def _routes_for_handler(self, handler_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.inventory.get('routes', []):
            h: Optional[dict[str, Any]] = r.get('handler')
            if h and (handler_id == f"ast::{h['module']}.{h['qual']}" or
                      self.symbols()['by_line'].get((h['file'], h['line'])) == handler_id):
                out.append(r)
        return out

    def _map_cbm_route(self, me: str, t: str, props: dict[str, Any], other: dict[str, Any],
                       nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], base: dict[str, Any]) -> bool:
        """Re-target CBM Route edges onto AST routes (CBM merges routes of different apps)."""
        if other['kind'] != 'Route' or t == 'HTTP_CALLS':
            if other['kind'] == 'Route':
                other['kind'] = 'HttpEndpoint'
            return False
        if t == 'HANDLES':
            rs: list[dict[str, Any]] = [r for r in self._routes_for_handler(me) if r['cbm_route_qn'] == other['qn']]
            for r in rs:
                nodes[r['id']] = self.route_node(r)
                edges.append({**base, 'from': r['id'], 'to': me, 'type': 'HANDLES', 'props': props})
            return bool(rs)
        if props.get('via') == 'route_registration':
            rs = [r for r in self.inventory.get('routes', []) if r['cbm_route_qn'] == other['qn']
                  and (self.scope_node(r['registrar_module'], r['registrar']) or {}).get('id') == me]
            for r in rs:
                nodes[r['id']] = self.route_node(r)
                edges.append({**base, 'from': me, 'to': r['id'], 'type': 'REGISTERS', 'props': props})
            return bool(rs)
        return False

    def _cbm_entry(self, qn: str, direction: str) -> dict[str, Any]:
        sym: dict[str, Any] = self.symbols()
        me: dict[str, Any] = sym['by_qn'].get(qn) or self.cache['nodes'].get(qn) or {'id': qn, 'qn': qn, 'file': ''}
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        complete: bool = True
        Q: str = cypher_str(qn)
        if direction == 'out':
            rows, ok = self._q(f'MATCH (a)-[r:{OUT_RELS}]->(b) WHERE a.qualified_name = {Q} RETURN type(r) AS t, '
                               f'properties(r) AS p, ' + NODE_FIELDS.format(v='b'))
            is_method: bool = me.get('kind') in ('Method', None)  # OVERRIDE links methods only
            impl, ok2 = self._q(f'MATCH (b)<-[r:OVERRIDE]-(a) WHERE b.qualified_name = {Q} RETURN '
                                + NODE_FIELDS.format(v='a')) if is_method else ([], True)
            sim, ok3 = self._q(f'MATCH (a)-[r:SIMILAR_TO]-(b) WHERE a.qualified_name = {Q} RETURN properties(r) AS p, '
                               + NODE_FIELDS.format(v='b'))
            complete = ok and ok2 and ok3
            for row in rows:
                b: dict[str, Any] = cbm_node(row)
                props: dict[str, Any] = _props(row.get('p'))
                base: dict[str, Any] = {'file': me.get('file'), 'line': _int(props.get('line')), 'source': 'cbm'}
                if self._map_cbm_route(qn, row['t'], props, b, nodes, edges, base):
                    continue
                nodes[b['id']] = b
                edges.append({**base, 'from': qn, 'to': b['id'], 'type': row['t'], 'props': props,
                              **({'low_confidence': True} if _low(props) else {})})
            for row in impl:
                b = cbm_node(row)
                nodes[b['id']] = b
                edges.append({'from': qn, 'to': b['id'], 'type': 'OVERRIDDEN_BY', 'file': b['file'], 'line': b['line'],
                              'source': 'cbm', 'props': {'derived_from': 'OVERRIDE (implementation -> base)'}})
            for row in sim:
                b = cbm_node(row)
                nodes[b['id']] = b
                a, z = sorted([qn, b['id']])
                edges.append({'from': a, 'to': z, 'type': 'SIMILAR_TO', 'file': None, 'line': None, 'source': 'cbm',
                              'props': _props(row.get('p'))})
            unresolved: list[dict[str, Any]] = []
            counts: Counter[str] = Counter()
            rel: str = me.get('file') or ''
            tree: Optional[ast.Module] = self._tree(rel) if rel.endswith('.py') and me.get('line') else None
            fn: Optional[ast.AST] = find_def(tree, me['line']) if tree else None
            if fn is not None:
                facts: dict[str, Any] = self.cache['facts'].get(rel, {})
                d: Optional[dict[str, Any]] = next((x for x in facts.get('defs', []) if x['line'] == me['line']), None)
                by_line: dict[int, list[dict[str, Any]]] = {}
                for e in edges:
                    if e['source'] == 'cbm' and e['from'] == qn and e['line'] is not None:
                        by_line.setdefault(e['line'], []).append(e)
                e2, n2, unresolved, counts = self._ast_out(rel, d['qual'] if d else '', fn, qn, by_line)
                nodes.update({k: v for k, v in n2.items() if k not in nodes})
                have: set[tuple[str, str]] = {(e['to'], e['type']) for e in edges}
                edges += [e for e in e2 if (e['to'], e['type']) not in have]
            return {'nodes': nodes, 'edges': edges, 'unresolved': unresolved, 'complete': complete,
                    'counts': dict(counts), 'ast_checked': fn is not None}
        # Anchor the pattern on the known node: `(a)-[r]->(b) WHERE b...` scans every edge in CBM 0.11
        # (~1.3 s); the reversed pattern returns identical rows in ~40 ms.
        rows, ok = self._q(f'MATCH (b)<-[r:{OUT_RELS}]-(a) WHERE b.qualified_name = {Q} RETURN type(r) AS t, '
                           f'properties(r) AS p, ' + NODE_FIELDS.format(v='a'))
        base_rows, ok2 = self._q(f'MATCH (a)-[r:OVERRIDE]->(b) WHERE a.qualified_name = {Q} RETURN '
                                 + NODE_FIELDS.format(v='b')) if me.get('kind') in ('Method', None) else ([], True)
        complete = ok and ok2
        for row in rows:
            a2: dict[str, Any] = cbm_node(row)
            props = _props(row.get('p'))
            nodes[a2['id']] = a2
            edges.append({'from': a2['id'], 'to': qn, 'type': row['t'], 'file': a2['file'],
                          'line': _int(props.get('line')), 'source': 'cbm', 'props': props,
                          **({'low_confidence': True} if _low(props) else {})})
        for row in base_rows:
            b = cbm_node(row)
            nodes[b['id']] = b
            edges.append({'from': b['id'], 'to': qn, 'type': 'OVERRIDDEN_BY', 'file': me.get('file'),
                          'line': me.get('line'), 'source': 'cbm', 'props': {'derived_from': 'OVERRIDE'}})
        for e in self.nested_callers().get(qn, []):
            nodes[e['_node']['id']] = e['_node']
            edges.append({k: v for k, v in e.items() if k != '_node'})
        for r in self._routes_for_handler(qn):
            nodes[r['id']] = self.route_node(r)
            edges.append({'from': r['id'], 'to': qn, 'type': 'HANDLES', 'file': r['file'], 'line': r['line'],
                          'source': 'ast', 'props': {}})
        return {'nodes': nodes, 'edges': edges, 'unresolved': [], 'complete': complete, 'counts': {}}

    def _ast_entry(self, nid: str, direction: str) -> dict[str, Any]:
        dotted_name: str = nid[len('ast::'):]
        bm: dict[str, tuple[str, dict[str, Any]]] = self._facts_by_module()
        hit: Optional[tuple[str, str]] = resolve_module(dotted_name, set(bm))
        if not hit:
            raise KeyError(f'unknown AST node {nid}')
        rel, f = bm[hit[0]]
        d: Optional[dict[str, Any]] = next((x for x in f['defs'] if x['qual'] == hit[1]), None)
        if not hit[1]:
            return {'nodes': {}, 'edges': [], 'unresolved': [], 'complete': True, 'counts': {}}
        if d is None:
            raise KeyError(f'AST definition not found: {nid}')
        me: dict[str, Any] = self.def_node(rel, d)
        if direction == 'out':
            tree: Optional[ast.Module] = self._tree(rel)
            fn: Optional[ast.AST] = find_def(tree, d['line']) if tree else None
            if fn is None:
                raise KeyError(f'cannot parse {rel}:{d["line"]}')
            edges, nodes, unresolved, counts = self._ast_out(rel, d['qual'], fn, nid, None)
            return {'nodes': {nid: me, **nodes}, 'edges': edges, 'unresolved': unresolved, 'complete': True,
                    'counts': dict(counts), 'ast_checked': True}
        nodes2: dict[str, dict[str, Any]] = {nid: me}
        edges2: list[dict[str, Any]] = []
        for e in self.nested_callers().get(nid, []):
            nodes2[e['_node']['id']] = e['_node']
            edges2.append({k: v for k, v in e.items() if k != '_node'})
        for r in self._routes_for_handler(nid):
            nodes2[r['id']] = self.route_node(r)
            edges2.append({'from': r['id'], 'to': nid, 'type': 'HANDLES', 'file': r['file'], 'line': r['line'],
                           'source': 'ast', 'props': {}})
        return {'nodes': nodes2, 'edges': edges2, 'unresolved': [], 'complete': True, 'counts': {}}

    def expand(self, nid: str, direction: str = 'out', force: bool = False) -> tuple[dict[str, Any], bool]:
        """Return (entry, cache_hit). An error raises; it is never stored as an empty neighbourhood."""
        if direction not in ('out', 'in'):
            raise ValueError('direction must be out|in')
        key: str = f'{direction}|{nid}'
        with self.lock:
            if key in self.cache['entries'] and not force:
                self.stats['cache_hits'] += 1
                return self.cache['entries'][key], True
            t0: float = time.perf_counter()
            if nid.startswith('route::'):
                entry: dict[str, Any] = self._route_entry(nid, direction)
            elif nid.startswith('ast::'):
                entry = self._ast_entry(nid, direction)
            else:
                entry = self._cbm_entry(nid, direction)
            entry['files'] = sorted({str(n.get('file')) for n in entry['nodes'].values() if n.get('file')}
                                    | {str(e.get('file')) for e in entry['edges'] if e.get('file')})
            entry['nodes'] = list(entry['nodes'].values())
            self.cache['entries'][key] = entry
            for n in entry['nodes']:
                self.cache['nodes'][n['id']] = n
            self.stats['cache_misses'] += 1
            self.stats['expansions'] += 1
            self.stats['last_expand_ms'] = round((time.perf_counter() - t0) * 1000, 1)
            self.version += 1
            return entry, False

    def expand_bfs(self, nid: str, direction: str, depth: int, cap: int, candidates: bool = False) -> dict[str, Any]:
        """Bounded BFS expansion; reports truncation explicitly. `candidates` also follows CANDIDATE_CALL
        edges (interactive exploration); sweeps leave it off so analysis input stays factual."""
        follow: frozenset[str] = FLOW | {'REGISTERS', 'INCLUDES'} | ({'CANDIDATE_CALL'} if candidates else frozenset())
        seen: set[str] = {nid}
        q: deque[tuple[str, int]] = deque([(nid, 0)])
        hits = misses = 0
        truncated: bool = False
        while q:
            cur, d = q.popleft()
            entry, hit = self.expand(cur, direction)
            hits += hit
            misses += not hit
            if d + 1 > depth:
                continue
            kinds: dict[str, str] = {n['id']: n['kind'] for n in entry['nodes']}
            for e in entry['edges']:
                nxt: str = e['to'] if direction == 'out' else e['from']
                if nxt == cur or nxt in seen or e['type'] not in follow:
                    continue
                if kinds.get(nxt) in ('External', 'HttpEndpoint', 'EnvVar', 'Variable', 'Class'):
                    continue
                if len(seen) >= cap:
                    truncated = True
                    break
                seen.add(nxt)
                q.append((nxt, d + 1))
        return {'visited': len(seen), 'cache_hits': hits, 'cbm_expansions': misses, 'truncated': truncated}

    # ------------------------------------------------------------------ state
    def twin_facts(self) -> list[dict[str, Any]]:
        """Fingerprints of defs, keyed to graph node ids (needs the CBM symbol table for stable ids)."""
        if self.cache.get('symbols') is None:
            return []
        out: list[dict[str, Any]] = []
        for rel, f in sorted(self.cache['facts'].items()):
            for d in f['defs']:
                if d.get('fp'):
                    n: dict[str, Any] = self.def_node(rel, d)
                    out.append({'id': n['id'], 'name': d['name'], 'file': rel, 'line': d['line'], 'h': d['fp']['h'],
                                'size': d['fp']['size'], 'nparams': d['fp']['nparams'], 'callees': d['fp']['callees'],
                                'tables': sorted({a['table'] for a in d.get('access', [])})})
        return out

    def medium_graph(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Shared-medium layer: Table nodes plus READS_TABLE/WRITES_TABLE edges from every def whose SQL text names
        a table (AST-derived, independent of CBM; needs the symbol table only to reuse CBM node ids)."""
        key: tuple[int, int] = (self.version, len(self.cache['facts']))
        if self._medium_memo and self._medium_memo[0] == key:
            return self._medium_memo[1], self._medium_memo[2]
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        if self.cache.get('symbols') is not None:
            for rel, f in sorted(self.cache['facts'].items()):
                for d in f['defs']:
                    for a in d.get('access', []):
                        fn: dict[str, Any] = self.def_node(rel, d)
                        tid: str = f'table::{a["table"]}'
                        nodes[fn['id']] = fn
                        nodes[tid] = {'id': tid, 'kind': 'Table', 'label': a['table'], 'qn': tid, 'file': '', 'line': None,
                                      'end_line': None, 'component': 'tables', 'source': 'ast'}
                        edges.append({'from': fn['id'], 'to': tid,
                                      'type': 'READS_TABLE' if a['mode'] == 'read' else 'WRITES_TABLE', 'file': rel,
                                      'line': a['line'], 'source': 'ast',
                                      'props': {'op': a['op'], 'window': a['window']}})
        self._medium_memo = (key, nodes, edges)
        return nodes, edges

    def graph(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
        nodes: dict[str, dict[str, Any]] = dict(self.cache['nodes'])
        for r in self.inventory.get('routes', []):
            nodes.setdefault(r['id'], self.route_node(r))
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        for entry in self.cache['entries'].values():
            for e in entry['edges']:
                edges.setdefault((e['from'], e['to'], e['type']), e)
        mn, me = self.medium_graph()
        for k, v in mn.items():
            nodes.setdefault(k, v)
        for e in me:
            edges.setdefault((e['from'], e['to'], e['type']), e)
        return nodes, list(edges.values()), [r['id'] for r in self.inventory.get('routes', [])]

    def twin_nodes(self) -> dict[str, dict[str, Any]]:
        return {t['id']: self.cache['nodes'].get(t['id']) or self.symbols()['by_qn'].get(t['id']) or
                {'id': t['id'], 'kind': 'Function', 'label': t['name'], 'file': t['file'], 'line': t['line'],
                 'component': t['id'].rsplit('.', 1)[0], 'source': 'ast'} for t in self.twin_facts()}

    def apply_baseline(self, res: dict[str, Any]) -> None:
        """Tag findings acknowledged in the committed baseline as known; report stale baseline ids."""
        ack: dict[str, Any] = {}
        if self.baseline_path and self.baseline_path.is_file():
            try:
                ack = {a['id']: a for a in json.loads(self.baseline_path.read_text(encoding='utf-8')).get('acknowledged', [])}
            except (OSError, ValueError, KeyError, TypeError):
                ack = {}
        ids: set[str] = {f['id'] for f in res['findings']}
        for f in res['findings']:
            f['status'] = 'known' if f['id'] in ack else 'new'
            if f['id'] in ack:
                f['acknowledged'] = {k: ack[f['id']].get(k) for k in ('reason', 'owner', 'at')}
        res['scope']['baseline'] = {'file': str(self.baseline_path) if self.baseline_path else None,
                                    'known': sum(1 for f in res['findings'] if f['status'] == 'known'),
                                    'new': sum(1 for f in res['findings'] if f['status'] == 'new'),
                                    'stale_ids': sorted(set(ack) - ids)}

    def findings(self) -> dict[str, Any]:
        if self._findings_memo[0] == self.version:
            return self._findings_memo[1]
        nodes, edges, roots = self.graph()
        ext: set[str] = {k for k, n in nodes.items() if n['kind'] in ('External', 'HttpEndpoint')}
        by_qn: dict[str, dict[str, Any]] = self._symbol_index()['by_qn'] if self.cache.get('symbols') else {}
        for n in nodes.values():
            if n['kind'] == 'Variable':
                n['owner_kind'] = (by_qn.get(n['component']) or {}).get('kind', 'unknown')
        fan: dict[str, int] = self.cache.get('fanin') or {}
        nodes.update(self.twin_nodes())
        res: dict[str, Any] = analyze(nodes, [e for e in edges if e['to'] not in ext], roots, fan,
                                      self.utility_threshold(), self.twin_facts())
        self.apply_baseline(res)
        self._findings_memo = (self.version, res)
        return res

    def coverage(self, nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
        routes: list[dict[str, Any]] = self.inventory.get('routes', [])
        entries: dict[str, Any] = self.cache['entries']
        unresolved: Counter[str] = Counter()
        for k, e in entries.items():
            for u in e.get('unresolved', []):
                unresolved[u['status']] += 1
        handler_kinds: Counter[str] = Counter()
        for r in routes:
            h: Optional[dict[str, Any]] = r.get('handler')
            if not h:
                handler_kinds['unresolved'] += 1
            elif h['nested']:
                handler_kinds['nested_ast_only'] += 1
            else:
                handler_kinds['cbm_symbol' if self.cache.get('symbols') is not None and
                              self._symbol_index()['by_line'].get((h['file'], h['line'])) else 'module_level'] += 1
        cross: dict[str, Any] = {'available': self.cache.get('cbm_routes') is not None}
        if cross['available']:
            cbm_set: dict[str, bool] = {r['qn']: r['client_target'] for r in self.cache['cbm_routes']}
            ast_set: set[str] = {r['cbm_route_qn'] for r in routes if r['cbm_route_qn'] and r['method'] != 'WS'}
            cross.update({'cbm_route_nodes': len(cbm_set), 'ast_route_keys': len(ast_set),
                          'matched': len(ast_set & set(cbm_set)),
                          'ast_only': sorted(ast_set - set(cbm_set)),
                          'cbm_only_server': sorted(q for q in set(cbm_set) - ast_set if not cbm_set[q]),
                          'cbm_only_client_http_targets': sorted(q for q in set(cbm_set) - ast_set if cbm_set[q])})
        parse_errors: list[str] = [f"{rel}: {f['parse_error']}" for rel, f in self.cache['facts'].items() if f.get('parse_error')]
        return {
            'routes_discovered': len(routes),
            'routes_by_status': dict(Counter(r['status'] for r in routes)),
            'routes_conditional_registration': sum(1 for r in routes if r.get('conditional_registration')),
            'routes_loaded_in_selector': len(routes),
            'routes_expanded': sum(1 for r in routes if f"out|{r['id']}" in entries),
            'handlers': dict(handler_kinds),
            'route_cross_check_vs_cbm': cross,
            'route_limitations': self.inventory.get('limitations', []),
            'apps': self.inventory.get('apps', []),
            'loaded_nodes': len(nodes), 'loaded_edges': len(edges),
            'edge_sources': dict(Counter(e.get('source') for e in edges)),
            'edge_types': dict(Counter(e['type'] for e in edges)),
            'expanded_entries': len(entries),
            'incomplete_entries': sorted(k for k, e in entries.items() if not e.get('complete', True)),
            'unresolved_calls': dict(unresolved),
            'parse_errors': parse_errors,
            'cbm_limitations': ['CBM 0.11 does not index nested function bodies; nested handlers are linked by the '
                                'AST adapter (edge source=ast).',
                                'CBM Route nodes are keyed by METHOD+path only and ignore include_router prefixes; '
                                'the route inventory is AST-derived.',
                                'Calls on objects of unknown type resolve only when the method name is unique in '
                                'scope (strategy=unique_method_name); otherwise listed as ambiguous.'],
            'complete_repository_audit': False,
        }

    def state(self) -> dict[str, Any]:
        with self.lock:
            nodes, edges, roots = self.graph()
            fr: dict[str, Any] = self.findings() if self.cache.get('fanin') is not None else \
                {'findings': [], 'scope': {'note': 'CBM statistics not loaded yet; analysis pending first expansion.'}}
            cov: dict[str, Any] = self.coverage(nodes, edges)
            cbm_stats: dict[str, Any] = {'cbm_calls': self.cbm.calls if self.cbm else 0,
                                         'cbm_query_seconds': round(self.cbm.call_seconds, 2) if self.cbm else 0,
                                         'cbm_version': self.cbm.version if self.cbm else ''}
            unresolved: dict[str, list[dict[str, Any]]] = {k.split('|', 1)[1]: e['unresolved']
                                                           for k, e in self.cache['entries'].items()
                                                           if k.startswith('out|') and e.get('unresolved')}
            return {'project': self.project, 'scope': str(self.scope), 'nodes': list(nodes.values()), 'edges': edges,
                    'routes': [{k: r[k] for k in ('id', 'method', 'path', 'app', 'status', 'handler_status', 'file',
                                                  'line', 'mount_chain', 'cbm_route_qn', 'handler',
                                                  'conditional_registration')}
                               for r in self.inventory.get('routes', [])],
                    'expanded': sorted(self.cache['entries']), 'unresolved': unresolved,
                    'findings': fr['findings'], 'analysis_scope': fr['scope'], 'coverage': cov,
                    'stats': {**self.stats, **cbm_stats, 'index': self.cache.get('index')},
                    'ready': self.ready.is_set(), 'ready_error': self.ready_error}

    def delta(self, before_nodes: set[str], before_edges: set[tuple[str, str, str]]) -> dict[str, Any]:
        st: dict[str, Any] = self.state()
        st['nodes'] = [n for n in st['nodes'] if n['id'] not in before_nodes]
        st['edges'] = [e for e in st['edges'] if (e['from'], e['to'], e['type']) not in before_edges]
        st['delta'] = True
        return st

    def source(self, nid: str) -> dict[str, Any]:
        n: Optional[dict[str, Any]] = self.graph()[0].get(nid)
        if not n or not n.get('file'):
            raise KeyError('node has no source file')
        p: Path = (self.scope / n['file']).resolve()
        if not p.is_relative_to(self.scope) or not p.is_file():
            raise KeyError('source outside scope')
        lines: list[str] = p.read_text(encoding='utf-8-sig', errors='replace').splitlines()
        start: int = max(1, (n.get('line') or 1))
        end: int = min(len(lines), max(start, n.get('end_line') or start) , start + 79)
        return {'file': n['file'], 'abs': str(p), 'start': start, 'end': end, 'text': '\n'.join(lines[start - 1:end])}

    def search(self, q: str) -> list[dict[str, Any]]:
        ql: str = q.lower()
        out: list[dict[str, Any]] = []
        for n in self.symbols()['by_qn'].values():
            if n['kind'] != 'External' and (ql in n['label'].lower() or ql in n['qn'].lower()):
                out.append(n)
        out.sort(key=lambda n: (not n['label'].lower().startswith(ql), len(n['qn'])))
        return out[:40]


# ---------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    engine: Engine

    def log_message(self, *args: Any) -> None:
        pass

    def send(self, obj: Any, status: int = 200) -> None:
        b: bytes = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        p: dict[str, list[str]] = parse_qs(u.query)
        arg: Callable[[str, str], str] = lambda k, d='': p.get(k, [d])[0]
        eng: Engine = self.engine
        try:
            if u.path == '/':
                b: bytes = eng.render_html().encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif u.path == '/favicon.ico':
                self.send_response(204)
                self.end_headers()
            elif u.path == '/api/state':
                self.send(eng.state())
            elif u.path == '/api/expand':
                nid: str = arg('id')
                depth: int = max(1, min(6, int(arg('depth', '1'))))
                dirs: list[str] = ['out', 'in'] if arg('dir', 'both') == 'both' else [arg('dir')]
                with eng.lock:
                    nodes, edges, _ = eng.graph()
                    bn: set[str] = set(nodes)
                    be: set[tuple[str, str, str]] = {(e['from'], e['to'], e['type']) for e in edges}
                    t0: float = time.perf_counter()
                    runs: list[dict[str, Any]] = [eng.expand_bfs(nid, d, depth if d == 'out' else 1, 400, candidates=True)
                                               for d in dirs]
                    eng.persist()
                    res: dict[str, Any] = eng.delta(bn, be)
                res['request'] = {'id': nid, 'dirs': dirs, 'depth': depth, 'runs': runs,
                                  'ms': round((time.perf_counter() - t0) * 1000, 1)}
                self.send(res)
            elif u.path == '/api/source':
                self.send(eng.source(arg('id')))
            elif u.path == '/api/search':
                self.send({'results': eng.search(arg('q'))})
            else:
                self.send({'error': 'Not found'}, 404)
        except KeyError as exc:
            self.send({'error': str(exc), 'kind': 'not_found'}, 404)
        except (CBMError, TimeoutError, ValueError, OSError) as exc:
            log(f'ERROR {self.path}: {exc}')
            self.send({'error': str(exc), 'kind': 'cbm_error' if isinstance(exc, CBMError) else 'error'}, 500)


def main() -> None:
    ap = argparse.ArgumentParser(description='Interactive execution-path explorer (CBM + FastAPI AST adapter)')
    ap.add_argument('--repo', default='.')
    ap.add_argument('--scope', default='src', help='Folder (relative to --repo) to index and analyse; "." for all')
    ap.add_argument('--project', required=True, help='CBM project base name; "-<scope>" is appended for sub-scopes')
    ap.add_argument('--port', type=int, default=8766)
    ap.add_argument('--out', default='.arch-audit/pathways.html')
    ap.add_argument('--report', default='.arch-audit/findings.json')
    ap.add_argument('--cache-dir', default=None)
    ap.add_argument('--cbm', default=None, help='Path to codebase-memory-mcp (default: PATH / %%LOCALAPPDATA%%)')
    ap.add_argument('--neighbors', type=int, default=200, help='CBM page size for adjacency queries')
    ap.add_argument('--timeout', type=int, default=30, help='Per-query CBM timeout (seconds)')
    ap.add_argument('--sweep-depth', type=int, default=0,
                    help='Before serving, expand every route downstream N levels (cached) for repository-wide findings')
    ap.add_argument('--roots', choices=('routes', 'all'), default='routes',
                    help='Sweep roots: API routes only, or also every unreferenced public callable (non-HTTP entry points)')
    ap.add_argument('--sweep-cap', type=int, default=400, help='Max nodes visited per route during a sweep')
    ap.add_argument('--baseline', default='.arch-audit/baseline.json',
                    help='Committed acknowledgements: {"acknowledged": [{"id","reason","owner","at"}]}; matching findings show as known')
    ap.add_argument('--baseline-update', action='store_true',
                    help='Acknowledge every current finding not yet in the baseline (reviewed-and-accepted snapshot), then exit')
    ap.add_argument('--refresh', action='store_true', help='Force CBM re-index and drop cached expansions')
    ap.add_argument('--refresh-routes', action='store_true', help='(compat) routes are always re-derived from changed files')
    ap.add_argument('--no-daemon', action='store_true', help='(compat, ignored) a single MCP session is always used')
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--no-serve', action='store_true', help='Write HTML/JSON and exit')
    a = ap.parse_args()
    if not 1 <= a.neighbors <= 5000:
        ap.error('--neighbors must be 1..5000')
    t_start: float = time.perf_counter()
    repo: Path = Path(a.repo).resolve()
    scope: Path = (repo / a.scope).resolve()
    if not scope.is_dir() or not scope.is_relative_to(repo):
        ap.error('Scope must be an existing directory inside --repo')
    scope_id: str = scope.relative_to(repo).as_posix()
    project: str = a.project if scope_id == '.' else a.project + '-' + re.sub(r'[^A-Za-z0-9]+', '-', scope_id).strip('-')
    ident: str = hashlib.sha256((str(scope) + '\n' + project).encode()).hexdigest()[:16]
    cache_dir: Path = Path(a.cache_dir) if a.cache_dir else \
        Path(os.getenv('LOCALAPPDATA') or Path.home() / '.cache') / 'architecture-pathway-audit' / ident
    log(f'Scope {scope} -> CBM project {project}; cache {cache_dir}')
    cbm: CBMClient = CBMClient(find_executable(a.cbm), timeout=a.timeout, log=log)
    eng: Engine = Engine(scope, project, cache_dir, repo / a.out, repo / a.report, cbm, page=a.neighbors)
    if a.refresh:
        eng.cache['entries'] = {}
        eng.cache['index'] = None
    eng.baseline_path = repo / a.baseline
    changed: set[str] = eng.sync_files()
    dropped: int = eng.invalidate(changed) if eng.cache.get('index') else 0
    first: bool = not eng.cache.get('index')
    log(f'Routes: {len(eng.inventory["routes"])} discovered via AST in {time.perf_counter() - t_start:.2f}s; '
        f'{len(changed)} changed files{" (first run)" if first else ""}; {dropped} cached expansions invalidated; '
        f'{len(eng.cache["entries"])} reused.')
    threading.Thread(target=eng.ensure_index, daemon=True, name='cbm-index').start()
    if a.sweep_depth > 0:
        eng.ready.wait()
        t0: float = time.perf_counter()
        eng.symbols()
        eng.fanin()
        eng.cbm_routes()
        hits = misses = visited = trunc = 0
        sweep_ids: list[str] = [r['id'] for r in eng.inventory['routes']] + (eng.orphan_roots() if a.roots == 'all' else [])
        for rid in sweep_ids:
            res: dict[str, Any] = eng.expand_bfs(rid, 'out', a.sweep_depth, a.sweep_cap)
            hits += res['cache_hits']
            misses += res['cbm_expansions']
            visited += res['visited']
            trunc += res['truncated']
        log(f'Sweep depth {a.sweep_depth}: {len(sweep_ids)} roots, {visited} node visits, '
            f'{misses} expansions fetched, {hits} cache hits, {trunc} truncated, {time.perf_counter() - t0:.1f}s')
    elif not first and not changed:
        pass
    if a.baseline_update:
        eng.ready.wait()
        eng.fanin()
        cur: dict[str, Any] = {x['id']: x for x in eng.findings()['findings']}
        old: dict[str, Any] = json.loads(eng.baseline_path.read_text(encoding='utf-8')) if eng.baseline_path.is_file() else {}
        have: list[dict[str, Any]] = old.get('acknowledged', [])
        seen: set[str] = {x['id'] for x in have}
        stamp: str = time.strftime('%Y-%m-%d')
        have += [{'id': i, 'kind': x['kind'], 'anchor': x['anchor'], 'reason': 'baselined: reviewed, accepted for now',
                  'owner': '', 'at': stamp} for i, x in sorted(cur.items()) if i not in seen]
        eng.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        eng.baseline_path.write_text(json.dumps({'acknowledged': have}, indent=1), encoding='utf-8')
        log(f'Baseline {eng.baseline_path}: {len(have)} acknowledged ({len(have) - len(seen)} added)')
        cbm.close()
        return
    if a.no_serve:
        eng.ready.wait()
        if not eng.ready_error:
            eng.fanin()
            eng.cbm_routes()
        eng.persist()
        st: dict[str, Any] = eng.state()
        log(f'Wrote {eng.out} and {eng.report}: {len(st["nodes"])} nodes, {len(st["edges"])} edges, '
            f'{len(st["findings"])} findings, {st["stats"]["cbm_calls"]} CBM calls, total {time.perf_counter() - t_start:.1f}s')
        cbm.close()
        return
    Handler.engine = eng
    server: ThreadingHTTPServer = ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
    url: str = f'http://127.0.0.1:{a.port}/'
    log(f'Explorer {url} ready in {time.perf_counter() - t_start:.2f}s (CBM warms in background). Ctrl+C to stop.')
    log(f'Snapshot: {eng.out} | Findings: {eng.report}')
    if not a.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with eng.lock:
            eng.persist()
        cbm.close()


if __name__ == '__main__':
    try:
        main()
    except (CBMError, OSError, subprocess.SubprocessError) as e:
        print('ERROR:', e, file=sys.stderr)
        sys.exit(2)
