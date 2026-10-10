"""Generic Python AST adapter: FastAPI route discovery + per-function call sites.

Why it exists (verified on codebase-memory-mcp 0.11.0): CBM does not create nodes for
nested functions and drops their call edges, so handlers declared inside a router
factory (`def build_router(): router = APIRouter(); @router.get(...) def h(): ...`)
have no downstream graph at all. CBM also keys Route nodes only by METHOD+path, so
same-path routes of different apps collapse, and it does not apply `include_router`
prefixes. This module reconstructs the route inventory from source without any
project-specific names, and exposes call sites so nested bodies can be linked back
to CBM symbols.

Everything here is a pure function of file contents. Results are JSON-serialisable
dicts so they can be cached per file signature.
"""
from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import re
from typing import Any, Iterable, Optional

HTTP_VERBS: frozenset[str] = frozenset({'get', 'post', 'put', 'patch', 'delete', 'options', 'head', 'trace'})
ROUTE_DECORATORS: frozenset[str] = HTTP_VERBS | {'api_route', 'route', 'websocket', 'websocket_route'}
ROUTER_CTORS: frozenset[str] = frozenset({'FastAPI', 'APIRouter'})
BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))
SPAWN_CALLEES: frozenset[str] = frozenset({'create_task', 'ensure_future', 'run_in_executor', 'to_thread', 'submit',
                                           'Thread', 'Process', 'start_new_thread', 'add_task', 'call_soon',
                                           'call_soon_threadsafe', 'run_coroutine_threadsafe', 'gather'})


def module_name(rel: str) -> str:
    """'control/routes/sessions.py' -> 'control.routes.sessions' (CBM's convention)."""
    return re.sub(r'\.pyi?$', '', rel.replace('\\', '/')).replace('/', '.')


def norm_path(path: str) -> str:
    """Normalise path parameters the way CBM names Route nodes: '/a/{id}' -> '/a/{}'."""
    return re.sub(r'\{[^}]*\}', '{}', path)


def dotted(expr: ast.AST) -> Optional[str]:
    """Return 'a.b.c' for Name/Attribute chains, None for anything dynamic."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return '.'.join(reversed(parts))
    return None


def _const_str(expr: Optional[ast.AST]) -> Optional[str]:
    return expr.value if isinstance(expr, ast.Constant) and isinstance(expr.value, str) else None


def _kw(call: ast.Call, name: str) -> Optional[ast.AST]:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _methods(call: ast.Call, verb: str) -> list[str]:
    if verb in HTTP_VERBS:
        return [verb.upper()]
    if verb in ('websocket', 'websocket_route'):
        return ['WS']
    m: Optional[ast.AST] = _kw(call, 'methods')
    if isinstance(m, (ast.List, ast.Tuple, ast.Set)):
        vals: list[str] = [s.upper() for s in (_const_str(e) for e in m.elts) if s]
        if vals:
            return sorted(vals)
    return ['GET'] if m is None else ['<dynamic>']


def _resolve_relative(module: str, is_pkg: bool, level: int, target: Optional[str]) -> str:
    pkg: list[str] = module.split('.') if is_pkg else module.split('.')[:-1]
    if level > 1:
        pkg = pkg[:len(pkg) - (level - 1)]
    return '.'.join([*pkg, *([target] if target else [])])


def parse_file(source: str, rel: str) -> dict[str, Any]:
    """Extract defs, imports, router instances, route registrations and include_router calls."""
    tree: ast.Module = ast.parse(source, filename=rel)
    mod: str = module_name(rel)
    is_pkg: bool = rel.replace('\\', '/').endswith('__init__.py')
    facts: dict[str, Any] = {'module': mod, 'imports': {}, 'defs': [], 'routers': {}, 'routes': [],
                             'includes': [], 'returns': {}, 'mounts': [], 'router_args': [], 'cond_returns': {}}
    router_vars: set[str] = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                             and (dotted(n.value.func) or '').split('.')[-1] in ROUTER_CTORS
                             for t in n.targets if isinstance(t, ast.Name)}

    def on_def(node: ast.AST, scope: str, scope_kind: str, cond: bool) -> None:
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        qual: str = f'{scope}.{node.name}' if scope else node.name
        is_class: bool = isinstance(node, ast.ClassDef)
        facts['defs'].append({'qual': qual, 'name': node.name, 'line': node.lineno,
                              'end_line': getattr(node, 'end_lineno', node.lineno),
                              'async': isinstance(node, ast.AsyncFunctionDef),
                              'kind': 'class' if is_class else ('method' if scope_kind == 'class' else 'function'),
                              'nested': scope_kind == 'function', 'parent': scope,
                              'params': [] if is_class else [a.arg for a in [*node.args.posonlyargs, *node.args.args,
                                                                             *node.args.kwonlyargs]]})
        if not is_class:
            acc: list[dict[str, Any]] = data_access(node)
            if acc:
                facts['defs'][-1]['access'] = acc
            fp: Optional[dict[str, Any]] = fingerprint(node)
            if fp:
                facts['defs'][-1]['fp'] = fp
        for dec in node.decorator_list:
            walk(dec, scope, scope_kind, cond)
            if not is_class and isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                    and dec.func.attr in ROUTE_DECORATORS:
                p: Optional[ast.AST] = dec.args[0] if dec.args else _kw(dec, 'path')
                facts['routes'].append({'router': dotted(dec.func.value), 'scope': scope, 'line': dec.lineno,
                                        'path': _const_str(p), 'methods': _methods(dec, dec.func.attr),
                                        'handler': qual, 'handler_ref': None, 'conditional': cond})
        for child in node.body:
            walk(child, qual, 'class' if is_class else 'function', False)

    def bind(name: str, target: str, scope: str) -> None:
        # Module-level imports win; a function-local import only fills an unbound name.
        if scope == '' or name not in facts['imports']:
            facts['imports'][name] = target

    def on_node(sub: ast.AST, scope: str, scope_kind: str, cond: bool) -> None:
        if isinstance(sub, ast.Import):
            for a in sub.names:
                bind(a.asname or a.name.split('.')[0], a.name if a.asname else a.name.split('.')[0], scope)
        elif isinstance(sub, ast.ImportFrom):
            base: str = _resolve_relative(mod, is_pkg, sub.level, sub.module) if sub.level else (sub.module or '')
            for a in sub.names:
                if a.name != '*':
                    bind(a.asname or a.name, f'{base}.{a.name}', scope)
        elif isinstance(sub, (ast.Assign, ast.AnnAssign)) and isinstance(sub.value, ast.Call):
            ctor: Optional[str] = dotted(sub.value.func)
            targets: list[ast.AST] = list(sub.targets) if isinstance(sub, ast.Assign) else [sub.target]
            if ctor and ctor.split('.')[-1] in ROUTER_CTORS:
                pref: Optional[ast.AST] = _kw(sub.value, 'prefix')
                for t in targets:
                    if isinstance(t, ast.Name):
                        facts['routers'][f'{scope}:{t.id}'] = {
                            'kind': 'app' if ctor.split('.')[-1] == 'FastAPI' else 'router',
                            'prefix': (_const_str(pref) or '') if pref is not None else '',
                            'dynamic_prefix': pref is not None and _const_str(pref) is None,
                            'line': sub.lineno, 'scope': scope, 'var': t.id}
        if isinstance(sub, ast.Call) and router_vars:
            # Calls that hand a router instance to another function (router passed as parameter).
            pos: list[Optional[str]] = [a.id if isinstance(a, ast.Name) else None for a in sub.args]
            kws: dict[str, str] = {k.arg: k.value.id for k in sub.keywords if k.arg and isinstance(k.value, ast.Name)}
            if {*pos, *kws.values()} & router_vars:
                facts['router_args'].append({'func': dotted(sub.func), 'scope': scope, 'line': sub.lineno,
                                             'args': pos, 'kwargs': kws})
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            recv: Optional[str] = dotted(sub.func.value)
            attr: str = sub.func.attr
            if attr == 'include_router' and sub.args:
                child: ast.AST = sub.args[0]
                desc: dict[str, Any] = ({'kind': 'call', 'func': dotted(child.func)} if isinstance(child, ast.Call)
                                        else {'kind': 'ref', 'ref': dotted(child)})
                pref2: Optional[ast.AST] = _kw(sub, 'prefix')
                facts['includes'].append({'parent': recv, 'scope': scope, 'child': desc, 'line': sub.lineno,
                                          'prefix': (_const_str(pref2) or '') if pref2 is not None else '',
                                          'dynamic_prefix': pref2 is not None and _const_str(pref2) is None})
            elif attr in ('add_api_route', 'add_api_websocket_route') and sub.args:
                ep: Optional[ast.AST] = sub.args[1] if len(sub.args) > 1 else _kw(sub, 'endpoint')
                facts['routes'].append({'router': recv, 'scope': scope, 'line': sub.lineno,
                                        'path': _const_str(sub.args[0]),
                                        'methods': ['WS'] if 'websocket' in attr else _methods(sub, 'api_route'),
                                        'handler': None, 'handler_ref': dotted(ep) if ep is not None else None,
                                        'conditional': cond})
            elif attr == 'mount' and sub.args:
                facts['mounts'].append({'parent': recv, 'scope': scope, 'line': sub.lineno,
                                        'path': _const_str(sub.args[0])})
        elif isinstance(sub, ast.Return) and scope_kind == 'function':
            if isinstance(sub.value, ast.Name):
                facts['returns'].setdefault(scope, []).append(sub.value.id)
            if cond:  # an early exit: registrations after it may not run
                facts['cond_returns'][scope] = min(sub.lineno, facts['cond_returns'].get(scope, sub.lineno))

    branchy: tuple[type, ...] = (ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While, ast.Match, ast.IfExp)

    def walk(node: ast.AST, scope: str, scope_kind: str, cond: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            on_def(node, scope, scope_kind, cond)
            return
        on_node(node, scope, scope_kind, cond)
        inner: bool = cond or isinstance(node, branchy)
        for child in ast.iter_child_nodes(node):
            walk(child, scope, scope_kind, inner)

    for top in tree.body:
        walk(top, '', 'module', False)
    return facts


# ---------------------------------------------------------------------------
# Composition: resolve routers, include_router chains and full paths.
# ---------------------------------------------------------------------------
def resolve_module(name: str, modules: set[str]) -> Optional[tuple[str, str]]:
    """Split an absolute dotted import into (scoped module, remaining attribute path).

    Imports may carry a prefix outside the indexed scope (e.g. 'src.' when indexing src/),
    so leading components are dropped until a scoped module matches.
    """
    parts: list[str] = name.split('.')
    for start in range(len(parts)):
        for end in range(len(parts), start, -1):
            cand: str = '.'.join(parts[start:end])
            if cand in modules or cand + '.__init__' in modules:
                return (cand if cand in modules else cand + '.__init__'), '.'.join(parts[end:])
    return None


def _scope_chain(scope: str) -> list[str]:
    chain: list[str] = [scope]
    while scope:
        scope = scope.rpartition('.')[0]
        chain.append(scope)
    return chain


def compose_routes(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build the full route inventory from per-file facts ({rel: facts})."""
    by_mod: dict[str, tuple[str, dict[str, Any]]] = {f['module']: (rel, f) for rel, f in files.items()}
    modules: set[str] = set(by_mod)

    def router_in(mod: str, scope: str, name: str) -> Optional[tuple[str, str]]:
        _, f = by_mod[mod]
        for s in _scope_chain(scope):
            if f'{s}:{name}' in f['routers']:
                return mod, f'{s}:{name}'
        return None

    def resolve_ref(mod: str, scope: str, ref: Optional[str]) -> Optional[tuple[str, str]]:
        """Resolve a dotted expression to a router instance (module, 'scope:var')."""
        if not ref:
            return None
        head, _, rest = ref.partition('.')
        if not rest:
            local: Optional[tuple[str, str]] = router_in(mod, scope, head)
            if local:
                return local
        target: Optional[str] = by_mod[mod][1]['imports'].get(head)
        if target:
            hit: Optional[tuple[str, str]] = resolve_module(f'{target}.{rest}' if rest else target, modules)
            if hit and hit[1] and '.' not in hit[1]:
                return router_in(hit[0], '', hit[1])
        return None

    def resolve_func(mod: str, scope: str, ref: Optional[str]) -> Optional[tuple[str, str]]:
        """Resolve a dotted callable to (module, def qual)."""
        if not ref:
            return None
        f: dict[str, Any] = by_mod[mod][1]
        quals: set[str] = {d['qual'] for d in f['defs']}
        head, _, rest = ref.partition('.')
        if not rest:
            for s in _scope_chain(scope):
                q: str = f'{s}.{head}' if s else head
                if q in quals:
                    return mod, q
        target: Optional[str] = f['imports'].get(head)
        if target:
            hit: Optional[tuple[str, str]] = resolve_module(f'{target}.{rest}' if rest else target, modules)
            if hit and hit[1] and hit[1] in {d['qual'] for d in by_mod[hit[0]][1]['defs']}:
                return hit
        return None

    # Parent -> child router links.
    links: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    for mod, (rel, f) in sorted(by_mod.items()):
        for inc in f['includes']:
            parent: Optional[tuple[str, str]] = resolve_ref(mod, inc['scope'], inc['parent'])
            child: Optional[tuple[str, str]] = None
            if inc['child']['kind'] == 'ref':
                child = resolve_ref(mod, inc['scope'], inc['child']['ref'])
            else:
                fn: Optional[tuple[str, str]] = resolve_func(mod, inc['scope'], inc['child']['func'])
                if fn:
                    returned: list[str] = by_mod[fn[0]][1]['returns'].get(fn[1], [])
                    child = next((r for r in (router_in(fn[0], fn[1], v) for v in returned) if r), None)
            if parent and child:
                links.append({'parent': parent, 'child': child, 'prefix': inc['prefix'] or '',
                              'file': rel, 'line': inc['line'], 'composer': inc['scope'], 'composer_mod': mod})
            else:
                limitations.append({'kind': 'include_router_unresolved', 'file': rel, 'line': inc['line'],
                                    'detail': f"parent={inc['parent']} child={inc['child']}"})
            if inc['dynamic_prefix']:
                limitations.append({'kind': 'dynamic_prefix', 'file': rel, 'line': inc['line'],
                                    'detail': 'include_router prefix is not a string literal'})
        for m in f['mounts']:
            limitations.append({'kind': 'mounted_subapp', 'file': rel, 'line': m['line'],
                                'detail': f"mount({m['path']!r}) routes are not enumerated (static files or sub-app)"})

    parents: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ln in links:
        parents.setdefault(ln['child'], []).append(ln)

    def mount_chains(rkey: tuple[str, str], seen: frozenset[tuple[str, str]]) -> list[tuple[tuple[str, str], str, list[dict[str, Any]]]]:
        """All (root app, accumulated prefix, include links) chains above a router."""
        info: dict[str, Any] = by_mod[rkey[0]][1]['routers'][rkey[1]]
        own: str = info['prefix'] or ''
        if info['kind'] == 'app':
            return [(rkey, own, [])]
        out: list[tuple[tuple[str, str], str, list[dict[str, Any]]]] = []
        for ln in parents.get(rkey, []):
            if ln['parent'] in seen:
                limitations.append({'kind': 'router_cycle', 'file': ln['file'], 'line': ln['line'], 'detail': str(rkey)})
                continue
            for app, pre, chain in mount_chains(ln['parent'], seen | {rkey}):
                out.append((app, pre + ln['prefix'] + own, chain + [ln]))
        return out

    def receivers(mod: str, scope: str, ref: Optional[str]) -> list[tuple[tuple[str, str], Optional[dict[str, Any]]]]:
        """Router instances a registration receiver can denote, incl. a router passed as a parameter."""
        direct: Optional[tuple[str, str]] = resolve_ref(mod, scope, ref)
        if direct:
            return [(direct, None)]
        fdef: Optional[dict[str, Any]] = next((d for d in by_mod[mod][1]['defs'] if d['qual'] == scope), None)
        if not ref or '.' in ref or not fdef or ref not in fdef['params']:
            return []
        params: list[str] = fdef['params'][1:] if fdef['kind'] == 'method' else fdef['params']
        found: list[tuple[tuple[str, str], Optional[dict[str, Any]]]] = []
        for cmod, (crel, cf) in sorted(by_mod.items()):
            for call in cf['router_args']:
                if resolve_func(cmod, call['scope'], call['func']) != (mod, scope):
                    continue
                arg: Optional[str] = call['kwargs'].get(ref)
                if arg is None and ref in params and params.index(ref) < len(call['args']):
                    arg = call['args'][params.index(ref)]
                hit: Optional[tuple[str, str]] = resolve_ref(cmod, call['scope'], arg)
                if hit:
                    found.append((hit, {'file': crel, 'line': call['line'], 'prefix': '', 'composer': call['scope'],
                                        'composer_mod': cmod, 'via': f'parameter {ref}'}))
        return found

    routes: list[dict[str, Any]] = []
    for mod, (rel, f) in sorted(by_mod.items()):
        defs: dict[str, dict[str, Any]] = {d['qual']: d for d in f['defs']}
        for r in f['routes']:
            handler_qual: Optional[str] = r['handler']
            handler_mod: str = mod
            if handler_qual is None and r['handler_ref']:
                fn2: Optional[tuple[str, str]] = resolve_func(mod, r['scope'], r['handler_ref'])
                if fn2:
                    handler_mod, handler_qual = fn2
            hrel: str = by_mod[handler_mod][0]
            hdef: Optional[dict[str, Any]] = (defs if handler_mod == mod else
                                              {d['qual']: d for d in by_mod[handler_mod][1]['defs']}).get(handler_qual or '')
            early: Optional[int] = f.get('cond_returns', {}).get(r['scope'])
            base: dict[str, Any] = {
                'conditional_registration': bool(r.get('conditional')) or (early is not None and early < r['line']),
                'methods': r['methods'], 'raw_path': r['path'], 'file': rel, 'line': r['line'],
                'registrar': r['scope'], 'registrar_module': mod,
                'handler': ({'module': handler_mod, 'file': hrel, 'qual': handler_qual, 'line': hdef['line'],
                             'end_line': hdef['end_line'], 'async': hdef['async'], 'nested': hdef['nested']}
                            if hdef else None)}
            recvs = receivers(mod, r['scope'], r['router'])
            variants: list[tuple[Optional[tuple[str, str]], Optional[tuple[str, str]], str, list[dict[str, Any]], str]] = []
            for rkey, via in recvs:
                chains = mount_chains(rkey, frozenset())
                for app, pre, chain in chains:
                    variants.append((rkey, app, pre, ([via] if via else []) + chain, 'registered'))
                if not chains:
                    variants.append((rkey, None, '', [via] if via else [], 'router_not_mounted'))
            if not variants:
                variants.append((None, None, '', [], 'receiver_unresolved'))
            for rkey, app, pre, chain, status in variants:
                if r['path'] is None:
                    status = 'dynamic_path'
                full: Optional[str] = (pre + r['path']) if (r['path'] is not None and app) else r['path']
                app_id: str = f'{by_mod[app[0]][0]}:{app[1]}' if app else ''
                rrel: str = by_mod[rkey[0]][0] if rkey else ''
                for meth in r['methods']:
                    routes.append({**base, 'id': f"route::{app_id}::{meth} {full}", 'method': meth, 'path': full,
                                   'app': app_id, 'router': f'{rrel}:{rkey[1]}' if rkey else None,
                                   'status': status,
                                   'handler_status': 'resolved' if hdef else 'unresolved',
                                   'mount_chain': [{'file': c['file'], 'line': c['line'], 'prefix': c['prefix'],
                                                    'composer': c['composer'], 'composer_module': c['composer_mod'],
                                                    'via': c.get('via', 'include_router')}
                                                   for c in chain],
                                   'cbm_route_qn': f'__route__{meth}__{norm_path(full)}' if full else None})
    for r2 in routes:
        if r2['conditional_registration']:
            limitations.append({'kind': 'conditional_registration', 'file': r2['file'], 'line': r2['line'],
                                'detail': f"{r2['method']} {r2['path']} is registered under a branch or after a "
                                          f"conditional early return; it may be absent at runtime"})
    routes.sort(key=lambda x: (x['app'], x['path'] or '', x['method']))
    seen_ids: dict[str, int] = {}
    for r in routes:  # make ids unique if two handlers register the same method+path
        n: int = seen_ids.get(r['id'], 0)
        seen_ids[r['id']] = n + 1
        if n:
            r['id'] += f'#{n + 1}'
            r['duplicate_registration'] = True
    return {'routes': routes, 'limitations': limitations,
            'apps': sorted({r['app'] for r in routes if r['app']})}


# ---------------------------------------------------------------------------
# Call sites inside one function body (used for nested defs CBM did not index and to
# annotate CBM edges with await/spawn facts).
# ---------------------------------------------------------------------------
def find_def(tree: ast.Module, line: int) -> Optional[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.lineno == line:
            return node
    return None


_SQL_KEYWORDS: frozenset[str] = frozenset({'select', 'values', 'set', 'where', 'from', 'as', 'on', 'and', 'or', 'not', 'null',
                                           'case', 'when', 'then', 'else', 'end', 'exists', 'in', 'is', 'by', 'limit'})
_SQL_STMT: re.Pattern[str] = re.compile(r'\b(SELECT|INSERT|UPDATE|DELETE|REPLACE)\b', re.I)
_SQL_INSERT: re.Pattern[str] = re.compile(r'\b(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+["`\[]?(\w+)', re.I)
_SQL_UPDATE: re.Pattern[str] = re.compile(r'\bUPDATE\s+(?:OR\s+\w+\s+)?["`\[]?(\w+)["`\]]?\s+SET\b', re.I)
_SQL_DELETE: re.Pattern[str] = re.compile(r'\bDELETE\s+FROM\s+["`\[]?(\w+)', re.I)
_SQL_READ: re.Pattern[str] = re.compile(r'\b(?:FROM|JOIN)\s+["`\[]?(\w+)', re.I)


def _strings(fn: ast.AST) -> list[tuple[int, str]]:
    """String constants (and the constant parts of f-strings) in a def body, excluding nested defs."""
    out: list[tuple[int, str]] = []
    stack: list[ast.AST] = list(getattr(fn, 'body', []))
    while stack:
        n: ast.AST = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(n, ast.JoinedStr):
            out.append((n.lineno, ' '.join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else '?'
                                           for v in n.values)))
            continue
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) > 12:
            out.append((n.lineno, n.value))
        stack.extend(ast.iter_child_nodes(n))
    return sorted(out)


def data_access(fn: ast.AST) -> list[dict[str, Any]]:
    """SQL tables a def reads or writes, from SQL text in its string literals.

    Returns [{'table', 'mode': 'read'|'write', 'op': select|insert|update|delete, 'line', 'window'}] where window is
    'oldest_n' (ORDER BY ASC/unordered + LIMIT), 'newest_n' (ORDER BY .. DESC LIMIT), 'bounded' or 'full'.
    """
    out: list[dict[str, Any]] = []
    for line, text in _strings(fn):
        if not _SQL_STMT.search(text):
            continue
        flat: str = ' '.join(text.split())
        for rx, op in ((_SQL_INSERT, 'insert'), (_SQL_UPDATE, 'update'), (_SQL_DELETE, 'delete')):
            for m in rx.finditer(flat):
                if m.group(1).lower() not in _SQL_KEYWORDS:
                    out.append({'table': m.group(1), 'mode': 'write', 'op': op, 'line': line, 'window': 'full'})
        if re.search(r'\bSELECT\b', flat, re.I):
            scan: str = _SQL_DELETE.sub(' ', flat)
            lm: Optional[re.Match[str]] = re.search(r'\bLIMIT\s+(\d+)', flat, re.I)
            # LIMIT 1 is a first/exists lookup, not a window over a growing set
            limited: bool = bool(re.search(r'\bLIMIT\b', flat, re.I)) and not (lm and int(lm.group(1)) <= 1)
            ordered_desc: bool = bool(re.search(r'\bORDER\s+BY\b[^;)]*\bDESC\b', flat, re.I))
            window: str = ('newest_n' if ordered_desc else 'oldest_n') if limited else 'full'
            for m in _SQL_READ.finditer(scan):
                if m.group(1).lower() not in _SQL_KEYWORDS:
                    out.append({'table': m.group(1), 'mode': 'read', 'op': 'select', 'line': line, 'window': window})
    seen: set[tuple[str, str, str, str]] = set()
    uniq: list[dict[str, Any]] = []
    for a in out:
        k: tuple[str, str, str, str] = (a['table'].lower(), a['mode'], a['op'], a['window'])
        if k not in seen:
            seen.add(k)
            a['table'] = a['table'].lower()
            uniq.append(a)
    return uniq


MIN_FP_NODES: int = 12


class _Rename(ast.NodeTransformer):
    """Alpha-rename local names, drop annotations/docstrings/function names: the body's behaviour shape remains."""

    def __init__(self, local: list[str]) -> None:
        self.ids: dict[str, str] = {n: f'v{i}' for i, n in enumerate(local)}

    def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.Name(id=self.ids.get(node.id, node.id), ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        return ast.arg(arg=self.ids.get(node.arg, node.arg), annotation=None)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802
        if isinstance(node.value, str) and not _SQL_STMT.search(node.value):
            return ast.Constant(value='S')
        if isinstance(node.value, str):
            return ast.Constant(value=' '.join(node.value.lower().split()))
        return node

    def _fn(self, node: Any) -> ast.AST:
        self.generic_visit(node)
        node.name, node.returns, node.decorator_list = '_', None, []
        return node
    visit_FunctionDef = visit_AsyncFunctionDef = _fn  # noqa: N815


def fingerprint(fn: ast.AST) -> Optional[dict[str, Any]]:
    """Behaviour-shape fingerprint of a def: hash of the alpha-renamed body + callee set + arity.

    Equal hashes mean the same operations in the same order over the same non-local names, whatever the function,
    its locals or its parameters are called.
    """
    body: list[ast.stmt] = list(getattr(fn, 'body', []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    wrapper: ast.Module = ast.Module(body=copy.deepcopy(body), type_ignores=[])
    size: int = sum(1 for _ in ast.walk(wrapper))
    if size < MIN_FP_NODES:
        return None
    args: ast.arguments = fn.args  # type: ignore[attr-defined]
    params: list[str] = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
    local: list[str] = list(params)
    for n in ast.walk(wrapper):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id not in local:
            local.append(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name and n.name not in local:
            local.append(n.name)
    header: ast.Module = ast.Module(body=[ast.Expr(value=ast.Tuple(elts=[ast.Name(id=x, ctx=ast.Load()) for x in params],
                                                                  ctx=ast.Load()))], type_ignores=[])
    norm: _Rename = _Rename(local)
    text: str = ast.dump(norm.visit(header), annotate_fields=False) + ast.dump(norm.visit(wrapper), annotate_fields=False)
    callees: list[str] = sorted({c['name'] for c in call_sites(fn) if c['kind'] != 'dynamic' and c['name']})
    return {'h': hashlib.sha1(text.encode()).hexdigest()[:16], 'size': size, 'nparams': len(params), 'callees': callees}


def call_sites(fn: ast.AST) -> list[dict[str, Any]]:
    """Calls and spawned function references in a def body, excluding nested def/class bodies.

    kind: 'call' (direct), 'dynamic' (callee is not a Name/Attribute chain),
    'spawned_call' (a call passed into create_task/submit/Thread/...), 'spawn_ref'
    (a function reference handed to such a scheduler).
    """
    nodes: list[ast.AST] = []
    stack: list[ast.AST] = list(getattr(fn, 'body', []))
    while stack:
        n: ast.AST = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(getattr(n, 'decorator_list', []))
            continue
        nodes.append(n)
        stack.extend(ast.iter_child_nodes(n))
    awaited: set[int] = {id(n.value) for n in nodes if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)}
    spawned: set[int] = set()
    refs: list[dict[str, Any]] = []
    for n in nodes:
        if isinstance(n, ast.Call) and (dotted(n.func) or '').split('.')[-1] in SPAWN_CALLEES:
            for a in [*n.args, *(k.value for k in n.keywords)]:
                ref: Optional[str] = dotted(a) if isinstance(a, (ast.Name, ast.Attribute)) else None
                if isinstance(a, ast.Call):
                    spawned.add(id(a))
                elif ref:
                    refs.append({'line': a.lineno, 'text': ref, 'name': ref.split('.')[-1],
                                 'awaited': False, 'kind': 'spawn_ref'})
    out: list[dict[str, Any]] = []
    for n in nodes:
        if isinstance(n, ast.Call):
            text: Optional[str] = dotted(n.func)
            kind: str = 'spawned_call' if id(n) in spawned else ('call' if text else 'dynamic')
            out.append({'line': n.lineno, 'text': text or '<dynamic>', 'name': (text or '').split('.')[-1],
                        'awaited': id(n) in awaited, 'kind': kind})
    out.extend(refs)
    out.sort(key=lambda c: (c['line'], c['text'], c['kind']))
    return out
