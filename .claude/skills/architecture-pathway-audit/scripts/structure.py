"""Deterministic structural analysis over the *loaded* execution graph.

Three tiers, never conflated:
  fact       -- an observed structural property (convergence, divergence/rejoin).
  candidate  -- a structural pattern that MAY indicate a defect (bypass, duplicate
                implementation, config divergence, multiple writers, package cycle).
  verified   -- never produced here. Semantic verification needs an agent/human to
                read the source; every finding carries semantic_verification='not performed'.

No project-specific names, paths or known defects are encoded. Shared utilities are
excluded from convergence-style rules by a graph statistic (global fan-in from CBM),
not by name, so legitimate fan-out to helpers is not reported as an anomaly.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from typing import Any, Iterable, Optional

FLOW: frozenset[str] = frozenset({'HANDLES', 'CALLS', 'ASYNC_CALLS', 'HTTP_CALLS', 'SPAWNS', 'CALL_REFERENCE',
                                  'OVERRIDDEN_BY'})
MAX_DEPTH: int = 12
LOW_CONFIDENCE: float = 0.3  # CBM resolution confidence below this is a name guess (e.g. suffix_match 0.09)
MAX_FINDINGS_PER_KIND: int = 40
FOOTPRINT_MIN_CALLEES: int = 4
FOOTPRINT_JACCARD: float = 0.85
FOCUS_TABLES: int = 3  # an operation/reader touching more tables than this is not one focused fact-recording step


def _fid(kind: str, *parts: Iterable[str] | str) -> str:
    flat: list[str] = []
    for p in parts:
        flat.extend([p] if isinstance(p, str) else sorted(p))
    return hashlib.sha1('|'.join([kind, *flat]).encode()).hexdigest()[:12]


def _path(parent: dict[str, Optional[str]], end: str) -> list[str]:
    out: list[str] = [end]
    while parent.get(out[-1]) is not None:
        out.append(parent[out[-1]])  # type: ignore[arg-type]
    return out[::-1]


def _edge_ev(path: list[str], emap: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    ev: list[dict[str, Any]] = []
    for a, b in zip(path, path[1:]):
        e: dict[str, Any] = emap.get((a, b), {})
        ev.append({'from': a, 'to': b, 'type': e.get('type'), 'file': e.get('file'), 'line': e.get('line'),
                   'source': e.get('source')})
    return ev


def _dominators(root: str, succ: dict[str, list[str]], reach: set[str]) -> dict[str, str]:
    """Immediate dominators (Cooper-Harvey-Kennedy) on the subgraph reachable from root."""
    order: list[str] = []
    seen: set[str] = {root}
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:  # iterative DFS post-order
        n, i = stack.pop()
        kids: list[str] = [k for k in succ.get(n, []) if k in reach]
        if i < len(kids):
            stack.append((n, i + 1))
            if kids[i] not in seen:
                seen.add(kids[i])
                stack.append((kids[i], 0))
        else:
            order.append(n)
    rpo: list[str] = order[::-1]
    index: dict[str, int] = {n: i for i, n in enumerate(rpo)}
    preds: dict[str, list[str]] = defaultdict(list)
    for n in rpo:
        for k in succ.get(n, []):
            if k in index:
                preds[k].append(n)
    idom: dict[str, str] = {root: root}
    changed: bool = True
    while changed:
        changed = False
        for n in rpo[1:]:
            ps: list[str] = [p for p in preds[n] if p in idom]
            if not ps:
                continue
            new: str = ps[0]
            for p in ps[1:]:
                a, b = p, new
                while a != b:
                    while index[a] > index[b]:
                        a = idom[a]
                    while index[b] > index[a]:
                        b = idom[b]
                new = a
            if idom.get(n) != new:
                idom[n] = new
                changed = True
    return idom


def _dom_set(idom: dict[str, str], n: str) -> set[str]:
    out: set[str] = set()
    while n in idom and idom[n] != n:
        n = idom[n]
        out.add(n)
    return out


HUB_MIN_READERS: int = 10  # a medium read by at least this many functions AND in the top decile is a shared hub


def analyze(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], roots: list[str],
            fanin: dict[str, int], utility_threshold: int,
            twins: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Return {'findings': [...], 'scope': {...}} for the loaded graph."""
    succ: dict[str, list[str]] = defaultdict(list)
    emap: dict[tuple[str, str], dict[str, Any]] = {}
    configs: dict[str, set[str]] = defaultdict(set)
    writers: dict[str, set[str]] = defaultdict(set)
    similar: list[dict[str, Any]] = []
    t_readers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    t_writers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in sorted(edges, key=lambda x: (x['from'], x['to'], x['type'])):
        if e['type'] in FLOW and not e.get('low_confidence'):
            if (e['from'], e['to']) not in emap:
                succ[e['from']].append(e['to'])
            emap.setdefault((e['from'], e['to']), e)
        elif e['type'] == 'CONFIGURES':
            configs[e['from']].add(e['to'])
        elif e['type'] == 'WRITES':
            writers[e['to']].add(e['from'])
        elif e['type'] == 'SIMILAR_TO':
            similar.append(e)
        elif e['type'] in ('READS_TABLE', 'WRITES_TABLE'):
            (t_readers if e['type'] == 'READS_TABLE' else t_writers)[e['to']].append(e)

    def utility(n: str) -> bool:
        return fanin.get(n, 0) >= utility_threshold

    live_roots: list[str] = [r for r in roots if r in succ]
    reach: dict[str, dict[str, Optional[str]]] = {}
    depth: dict[str, dict[str, int]] = {}
    for r in live_roots:
        parent: dict[str, Optional[str]] = {r: None}
        d: dict[str, int] = {r: 0}
        q: deque[str] = deque([r])
        while q:
            n: str = q.popleft()
            if d[n] >= MAX_DEPTH or (n != r and utility(n)):
                continue  # shared utilities are leaves: what lies beneath them is not route-specific
            for k in succ.get(n, []):
                if k not in parent:
                    parent[k] = n
                    d[k] = d[n] + 1
                    q.append(k)
        reach[r] = parent
        depth[r] = d
    findings: list[dict[str, Any]] = []
    per_kind: dict[str, int] = defaultdict(int)

    def add(f: dict[str, Any]) -> None:
        per_kind[f['kind']] += 1  # counted even when capped, so truncation is reported, not silent
        if per_kind[f['kind']] > MAX_FINDINGS_PER_KIND:
            return
        f.setdefault('semantic_verification', 'not performed (requires source review by an agent or human)')
        findings.append(f)

    # 1. Convergence (fact): non-utility component reached from >=2 distinct handlers.
    handler_of: dict[str, str] = {r: (succ[r][0] if succ.get(r) else r) for r in live_roots}
    reached_by: dict[str, set[str]] = defaultdict(set)
    for r, par in reach.items():
        for n in par:
            if n != r and n != handler_of[r] and not utility(n) and n not in roots:
                reached_by[n].add(r)
    conv: dict[str, set[str]] = {n: rs for n, rs in reached_by.items()
                                 if len({handler_of[r] for r in rs}) >= 2}
    # Report only convergence "entry points": nodes none of whose flow-predecessors share the same route set.
    preds: dict[str, set[str]] = defaultdict(set)
    for a, ks in succ.items():
        for k in ks:
            preds[k].add(a)
    entry_conv: list[str] = sorted(n for n, rs in conv.items()
                                   if not any(conv.get(p) == rs for p in preds[n]))
    for n in entry_conv:
        rs: list[str] = sorted(conv[n])
        add({'id': _fid('CONVERGENCE', n), 'kind': 'CONVERGENCE', 'level': 'fact', 'anchor': n, 'related': rs,
             'title': f'{len(rs)} routes converge on one component',
             'detail': 'Several API entry points reach this component. Shared use is normal; '
                       'it is the precondition for the bypass/config checks below.',
             'evidence': [{'path': _path(reach[r], n), 'edges': _edge_ev(_path(reach[r], n), emap)} for r in rs[:4]]})

    # 2. Divergence + reconvergence (fact) inside one route's loaded subgraph.
    rejoin_seen: set[str] = set()
    for branch in sorted(succ):
        kids: list[str] = [k for k in succ[branch] if not utility(k)]
        if len(kids) < 2 or branch in rejoin_seen:
            continue
        desc: dict[str, dict[str, Optional[str]]] = {}
        for k in kids[:20]:
            par: dict[str, Optional[str]] = {k: None}
            q2: deque[tuple[str, int]] = deque([(k, 0)])
            while q2:
                n2, dd = q2.popleft()
                if dd >= 5 or utility(n2):
                    continue
                for c in succ.get(n2, []):
                    if c not in par and c != branch:
                        par[c] = n2
                        q2.append((c, dd + 1))
            desc[k] = par
        done: bool = False
        for i, a in enumerate(kids[:20]):
            for b in kids[i + 1:20]:
                common: list[str] = sorted(x for x in (set(desc[a]) & set(desc[b])) - {a, b} if not utility(x))
                if common:
                    j: str = common[0]
                    pa: list[str] = [branch, *_path(desc[a], j)]
                    pb: list[str] = [branch, *_path(desc[b], j)]
                    cfg_a: set[str] = set().union(*(configs[x] for x in pa))
                    cfg_b: set[str] = set().union(*(configs[x] for x in pb))
                    add({'id': _fid('DIVERGE_REJOIN', branch, j), 'kind': 'DIVERGE_REJOIN', 'level': 'fact',
                         'anchor': branch, 'related': [a, b, j],
                         'title': 'Execution diverges and reconverges',
                         'detail': 'Two branches leave this node and meet again downstream. Compare guards and '
                                   'configuration of both branches.',
                         'evidence': [{'path': pa, 'edges': _edge_ev(pa, emap)}, {'path': pb, 'edges': _edge_ev(pb, emap)}]})
                    if cfg_a != cfg_b:
                        add({'id': _fid('CONFIG_DIVERGENCE', branch, j), 'kind': 'CONFIG_DIVERGENCE',
                             'level': 'candidate', 'anchor': j, 'related': [branch, a, b],
                             'title': 'Reconverging branches read different configuration',
                             'detail': f'Branch via {a} reads {sorted(cfg_a - cfg_b) or "nothing extra"}; '
                                       f'branch via {b} reads {sorted(cfg_b - cfg_a) or "nothing extra"}.',
                             'evidence': [{'path': pa, 'edges': _edge_ev(pa, emap), 'config': sorted(cfg_a)},
                                          {'path': pb, 'edges': _edge_ev(pb, emap), 'config': sorted(cfg_b)}]})
                    done = True
                    break
            if done:
                break
        rejoin_seen.add(branch)

    # 3. Bypass of a common gate (candidate): C dominates sink S for >=2 routes, another route reaches S avoiding C.
    idoms: dict[str, dict[str, str]] = {r: _dominators(r, succ, set(reach[r])) for r in live_roots}
    bypass_keys: set[tuple[str, frozenset[str]]] = set()
    for s in sorted(conv, key=lambda n: (min(depth[r].get(n, 99) for r in conv[n]), n)):
        rs2: list[str] = sorted(conv[s])
        doms: dict[str, set[str]] = {r: _dom_set(idoms[r], s) - {r, handler_of[r]} for r in rs2}
        gates: dict[str, list[str]] = defaultdict(list)
        for r, ds in doms.items():
            for c in ds:
                gates[c].append(r)
        for c, through in sorted(gates.items()):
            if len({handler_of[r] for r in through}) < 2 or utility(c):
                continue
            around: list[str] = [r for r in rs2 if c not in doms[r] and handler_of[r] not in {handler_of[t] for t in through}]
            if len({handler_of[r] for r in around}) > len({handler_of[r] for r in through}):
                continue  # C is one caller among many, not a gate that most paths share
            key: tuple[str, frozenset[str]] = (c, frozenset(handler_of[r] for r in around))
            if not around or key in bypass_keys:
                continue
            bypass_keys.add(key)
            ev: list[dict[str, Any]] = [{'role': 'through_gate', 'path': _path(reach[r], s),
                                         'edges': _edge_ev(_path(reach[r], s), emap)} for r in through[:2]]
            ev += [{'role': 'around_gate', 'path': _path(reach[r], s), 'edges': _edge_ev(_path(reach[r], s), emap)}
                   for r in around[:2]]
            add({'id': _fid('BYPASS', c, s, [handler_of[r] for r in around]), 'kind': 'BYPASS', 'level': 'candidate',
                 'anchor': c, 'related': [s, *sorted(around)],
                 'title': 'Route reaches a shared component without its common gate',
                 'detail': f'{len(through)} route(s) always pass through the gate before reaching the sink; '
                           f'{len(around)} route(s) reach the same sink on a path that avoids it.',
                 'evidence': ev})

    # 4. Multiple writers (candidate): one module-level variable written by >=2 loaded functions.
    #    Attribute writes are skipped: CBM resolves `obj.attr = x` targets by attribute name only.
    for v, ws in sorted(writers.items()):
        if nodes.get(v, {}).get('owner_kind') != 'Module':
            continue
        fn_ws: list[str] = sorted(w for w in ws if nodes.get(w, {}).get('kind') in ('Function', 'Method', 'NestedFunction'))
        if len(fn_ws) >= 2:
            add({'id': _fid('MULTIPLE_WRITERS', v, fn_ws), 'kind': 'MULTIPLE_WRITERS', 'level': 'candidate',
                 'anchor': v, 'related': fn_ws, 'title': f'{len(fn_ws)} functions write the same state',
                 'detail': 'More than one mutation pathway targets this variable (CBM WRITES edges).',
                 'evidence': [{'path': [w, v], 'edges': [{'from': w, 'to': v, 'type': 'WRITES', 'source': 'cbm'}]}
                              for w in fn_ws[:6]]})

    # 5. Duplicate implementations (candidate): CBM SIMILAR_TO between two loaded, route-reachable symbols.
    reachable: set[str] = set().union(*(set(p) for p in reach.values())) if reach else set()
    for e in similar:
        if e['from'] in reachable or e['to'] in reachable:
            add({'id': _fid('DUPLICATE_IMPLEMENTATION', [e['from'], e['to']]), 'kind': 'DUPLICATE_IMPLEMENTATION',
                 'level': 'candidate', 'anchor': e['from'], 'related': [e['to']],
                 'title': 'Near-duplicate implementation', 'detail': f"CBM SIMILAR_TO {e.get('props') or {}}",
                 'evidence': [{'path': [e['from'], e['to']], 'edges': [{**{k: e.get(k) for k in ('from', 'to', 'type')},
                                                                      'source': 'cbm', 'props': e.get('props')}]}]})

    # 6. Overlapping responsibility (candidate): distinct handlers with highly overlapping downstream sets.
    sig: dict[str, set[str]] = {}
    for r in live_roots:
        h: str = handler_of[r]
        if h not in sig:
            sig[h] = {n for n in reach[r] if n not in (r, h) and not utility(n) and n not in roots}
    hs: list[str] = sorted(h for h, s in sig.items() if len(s) >= 4)
    pairs: list[tuple[float, str, str]] = []
    for i, a in enumerate(hs):
        for b in hs[i + 1:]:
            inter: set[str] = sig[a] & sig[b]
            if len(inter) < 4 or a in sig[b] or b in sig[a]:
                continue
            jac: float = len(inter) / len(sig[a] | sig[b])
            if jac >= 0.6 and sig[a] - sig[b] and sig[b] - sig[a]:
                pairs.append((jac, a, b))
    for jac, a, b in sorted(pairs, reverse=True)[:MAX_FINDINGS_PER_KIND]:
        add({'id': _fid('OVERLAPPING_PATHS', [a, b]), 'kind': 'OVERLAPPING_PATHS', 'level': 'candidate',
             'anchor': a, 'related': [b],
             'title': f'Two handlers share {jac:.0%} of their downstream components',
             'detail': f'Shared {len(sig[a] & sig[b])}; only-{a.split(".")[-1]}: {len(sig[a] - sig[b])}; '
                       f'only-{b.split(".")[-1]}: {len(sig[b] - sig[a])}. Possible parallel implementation of one '
                       'responsibility, or legitimate siblings.',
             'evidence': [{'shared': sorted(sig[a] & sig[b])[:25], 'only_a': sorted(sig[a] - sig[b])[:15],
                           'only_b': sorted(sig[b] - sig[a])[:15]}]})

    # 6b. Sibling overlap (candidate): two operations of ONE component (same class) that reach mostly the same
    #     downstream components, neither calling the other. Independent of routes: any loaded method qualifies.
    def downstream(start: str) -> set[str]:
        seen: set[str] = {start}
        q3: deque[tuple[str, int]] = deque([(start, 0)])
        while q3:
            n3, d3 = q3.popleft()
            if d3 >= 6 or (n3 != start and utility(n3)):
                continue
            for c in succ.get(n3, []):
                if c not in seen:
                    seen.add(c)
                    q3.append((c, d3 + 1))
        return {x for x in seen if x != start and not utility(x)}
    by_comp: dict[str, list[str]] = defaultdict(list)
    for n, nd in nodes.items():
        if nd.get('kind') == 'Method' and nd.get('component') and succ.get(n) and not utility(n):
            by_comp[nd['component']].append(n)
    sib_pairs: list[tuple[float, str, str, set[str]]] = []
    for comp, ms in sorted(by_comp.items()):
        if len(ms) < 2:
            continue
        ds: dict[str, set[str]] = {m: downstream(m) for m in sorted(ms)}
        names: list[str] = sorted(ds)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                inter2: set[str] = ds[a] & ds[b]
                if len(inter2) < 3 or b in ds[a] or a in ds[b]:
                    continue
                jac2: float = len(inter2) / len(ds[a] | ds[b])
                if jac2 >= 0.5 and ds[a] - ds[b] and ds[b] - ds[a]:
                    sib_pairs.append((jac2, a, b, inter2))
    parent_of: dict[str, str] = {}

    def find(x: str) -> str:
        while parent_of.setdefault(x, x) != x:
            parent_of[x] = parent_of[parent_of[x]]
            x = parent_of[x]
        return x
    for _, a, b, _ in sib_pairs:
        parent_of[find(a)] = find(b)
    clusters: dict[str, list[tuple[float, str, str, set[str]]]] = defaultdict(list)
    for t in sib_pairs:  # one finding per cluster of mutually overlapping siblings, not one per pair
        clusters[find(t[1])].append(t)
    for _, pairs2 in sorted(clusters.items(), key=lambda kv: (-max(t[0] for t in kv[1]), min(t[1] for t in kv[1]))):
        members: list[str] = sorted({m for t in pairs2 for m in (t[1], t[2])})
        top: tuple[float, str, str, set[str]] = max(pairs2, key=lambda t: (t[0], len(t[3])))
        comp_name: str = str(nodes[members[0]].get('component'))
        add({'id': _fid('SIBLING_OVERLAP', members), 'kind': 'SIBLING_OVERLAP', 'level': 'candidate',
             'anchor': members[0], 'related': members[1:],
             'title': f'{len(members)} operations of one component overlap in downstream components '
                      f'(up to {top[0]:.0%})',
             'detail': f'Siblings in {comp_name.split(".")[-1]} do not call each other yet reach common components '
                       f'({len(pairs2)} overlapping pair(s)). Possible parallel implementation of one responsibility, '
                       'or legitimate variants over a shared core.',
             'evidence': [{'pair': [t[1], t[2]], 'jaccard': round(t[0], 2), 'shared': sorted(t[3])[:12]}
                          for t in sorted(pairs2, key=lambda t: (-t[0], t[1], t[2]))[:6]]})

    # M. Shared-medium rules (tables extracted from SQL text; medium-agnostic by construction).
    def tname(t: str) -> str:
        return str(nodes.get(t, {}).get('label') or t)

    def acc_ev(es: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{'path': [e['from'], e['to']], 'edges': [{'from': e['from'], 'to': e['to'], 'type': e['type'],
                                                          'file': e.get('file'), 'line': e.get('line'), 'source': 'ast',
                                                          'props': e.get('props')}]} for e in es[:6]]
    # M1. Orphaned medium: written but never read, or read but never written, within the analysed scope.
    for t in sorted(set(t_readers) | set(t_writers)):
        if bool(t_readers[t]) != bool(t_writers[t]):
            side: str = 'written but never read' if t_writers[t] else 'read but never written'
            add({'id': _fid('ORPHAN_MEDIUM', t), 'kind': 'ORPHAN_MEDIUM', 'level': 'candidate', 'anchor': t,
                 'related': sorted({e['from'] for e in (t_writers[t] or t_readers[t])}),
                 'title': f'Table {tname(t)} is {side} in scope',
                 'detail': 'May be consumed/produced outside the analysed scope (other process, migration, '
                           'dynamic SQL) or be dead/void state.', 'evidence': acc_ev(t_writers[t] or t_readers[t])})
    # M2. Oldest-N window over an append-only medium: the newest entries become unreachable as it grows.
    for t in sorted(t_readers):
        ws: list[dict[str, Any]] = t_writers.get(t, [])
        if ws and all((e.get('props') or {}).get('op') == 'insert' for e in ws):
            bad: list[dict[str, Any]] = [e for e in t_readers[t] if (e.get('props') or {}).get('window') == 'oldest_n']
            if bad:
                add({'id': _fid('TRUNCATED_WINDOW', t, [e['from'] for e in bad]), 'kind': 'TRUNCATED_WINDOW',
                     'level': 'candidate', 'anchor': t, 'related': sorted({e['from'] for e in bad}),
                     'title': f'{len({e["from"] for e in bad})} reader(s) take the oldest-N rows of append-only {tname(t)}',
                     'detail': 'The table is only ever inserted into; LIMIT without ORDER BY .. DESC returns the oldest '
                               'rows, so state derived from it silently degrades as the table grows.',
                     'evidence': acc_ev(bad)})
    # M3. Co-written, separately read: tables written together by one operation (within 2 call levels) that have
    #     readers of only one of them, on both sides. Two stores of one fact with split readers can disagree.
    direct_w: dict[str, set[str]] = defaultdict(set)
    for t, es in t_writers.items():
        for e in es:
            direct_w[e['from']].add(t)
    direct_r: dict[str, set[str]] = defaultdict(set)
    for t, es in t_readers.items():
        for e in es:
            direct_r[e['from']].add(t)

    def writes_within(start: str, depth: int = 1) -> set[str]:
        seen2: set[str] = {start}
        q4: deque[tuple[str, int]] = deque([(start, 0)])
        got: set[str] = set(direct_w.get(start, ()))
        while q4:
            n4, d4 = q4.popleft()
            if d4 >= depth:
                continue
            for c in succ.get(n4, []):
                if c not in seen2:
                    seen2.add(c)
                    got |= direct_w.get(c, set())
                    q4.append((c, d4 + 1))
        return got
    origins: dict[tuple[str, str], list[str]] = defaultdict(list)
    for f0 in sorted(set(succ) | set(direct_w)):
        ts: list[str] = sorted(writes_within(f0))
        if 2 <= len(ts) <= FOCUS_TABLES:
            for i, x in enumerate(ts):
                for y in ts[i + 1:]:
                    origins[(x, y)].append(f0)
    counts_r: list[int] = sorted(len({e['from'] for e in es}) for es in t_readers.values())
    hub_cut: int = max(HUB_MIN_READERS, counts_r[int(len(counts_r) * 0.9)] if counts_r else HUB_MIN_READERS)
    hubs_m: set[str] = {t for t, es in t_readers.items() if len({e['from'] for e in es}) >= hub_cut}
    split: list[tuple[int, str, str, list[str], list[str]]] = []
    for (x, y), orig in origins.items():
        focused: list[str] = [r for r in direct_r if len(direct_r[r]) <= FOCUS_TABLES]  # readers asking one focused question
        rx: list[str] = sorted(r for r in focused if x in direct_r[r] and y not in direct_r[r] and x not in direct_w.get(r, ()))
        ry: list[str] = sorted(r for r in focused if y in direct_r[r] and x not in direct_r[r] and y not in direct_w.get(r, ()))
        if rx and ry:
            split.append((len(rx) + len(ry), x, y, rx, ry))
    for _, x, y, rx, ry in sorted(split, key=lambda t: (t[0], t[1], t[2]))[:MAX_FINDINGS_PER_KIND]:  # fewest readers first
        add({'id': _fid('SPLIT_READERS', x, y), 'kind': 'SPLIT_READERS', 'level': 'candidate', 'anchor': x,
             'related': [y, *rx[:6], *ry[:6]],
             'title': f'{tname(x)} and {tname(y)} are written together but read apart',
             'detail': f'{len(origins[(x, y)])} operation(s) write both; {len(rx)} reader(s) use only {tname(x)}, '
                       f'{len(ry)} use only {tname(y)}. Check that the readers answer the same question the same way.',
             'tags': [f'hub_medium:{tname(h)}' for h in (x, y) if h in hubs_m],
             'evidence': [{'co_writers': origins[(x, y)][:6], 'readers_only_x': rx[:8], 'readers_only_y': ry[:8]}]})

    # T. Behavioural twins: bodies that are the same up to names (exact fingerprint), or that have the same arity,
    #    the same tables and (nearly) the same callees (footprint), under different names, not calling each other.
    tw: list[dict[str, Any]] = [t for t in (twins or []) if t['id'] in nodes]
    exact_pairs: set[frozenset[str]] = set()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in tw:
        groups[t['h']].append(t)
    for h, ms in sorted(groups.items()):
        if len(ms) < 2:
            continue
        ids: list[str] = sorted(m['id'] for m in ms)
        exact_pairs.update(frozenset((a, b)) for i, a in enumerate(ids) for b in ids[i + 1:])
        names: set[str] = {m['name'] for m in ms}
        add({'id': _fid('BEHAVIOURAL_TWIN', ids), 'kind': 'BEHAVIOURAL_TWIN', 'level': 'candidate', 'anchor': ids[0],
             'related': ids[1:],
             'title': f'{len(ids)} functions have identical bodies up to naming'
                      + ('' if len(names) > 1 else ' (same name: likely copied override)'),
             'detail': 'Same operations in the same order over the same non-local names; locals, parameters and '
                       'strings differ at most. One implementation duplicated, or a deliberate polymorphic copy.',
             'tags': ['same_name'] if len(names) == 1 else ['renamed'],
             'evidence': [{'twin': m['id'], 'file': m['file'], 'line': m['line'], 'nodes': m['size']} for m in ms[:8]]})
    cand: list[dict[str, Any]] = [t for t in tw if len(t['callees']) >= FOOTPRINT_MIN_CALLEES]
    pair_hits: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    by_arity: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for t in cand:
        by_arity[t['nparams']].append(t)
    for ms in by_arity.values():
        for i, a in enumerate(ms):
            ca: set[str] = set(a['callees'])
            for b in ms[i + 1:]:
                if a['name'] == b['name'] or frozenset((a['id'], b['id'])) in exact_pairs:
                    continue
                if set(a['tables']) != set(b['tables']) or b['id'] in succ.get(a['id'], []) or a['id'] in succ.get(b['id'], []):
                    continue
                cb: set[str] = set(b['callees'])
                jac3: float = len(ca & cb) / len(ca | cb)
                if jac3 >= FOOTPRINT_JACCARD:
                    pair_hits.append((jac3, a, b))
    fp_parent: dict[str, str] = {}

    def fp_find(x: str) -> str:
        while fp_parent.setdefault(x, x) != x:
            fp_parent[x] = fp_parent[fp_parent[x]]
            x = fp_parent[x]
        return x
    for _, a, b in pair_hits:
        fp_parent[fp_find(a['id'])] = fp_find(b['id'])
    fp_groups: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for jac3, a, b in pair_hits:
        fp_groups[fp_find(a['id'])].update({a['id']: a, b['id']: b})
    for _, mem in sorted(fp_groups.items(), key=lambda kv: min(kv[1])):
        ids2: list[str] = sorted(mem)
        add({'id': _fid('SAME_FOOTPRINT', ids2), 'kind': 'SAME_FOOTPRINT', 'level': 'candidate', 'anchor': ids2[0],
             'related': ids2[1:],
             'title': f'{len(ids2)} differently named functions take the same inputs shape and use the same dependencies',
             'detail': 'Same arity, same tables, near-identical callee sets, no calls between them. Possible parallel '
                       'implementation of one operation, or legitimate variants over one shared core.',
             'evidence': [{'twin': m['id'], 'file': m['file'], 'line': m['line'], 'callees': m['callees'][:12],
                           'tables': m['tables']} for m in [mem[i] for i in ids2[:8]]]})

    # 7. Package dependency cycles (candidate) among loaded call edges.
    def pkg(n: str) -> Optional[str]:
        f: str = str(nodes.get(n, {}).get('file') or '')
        return f.split('/')[0] if '/' in f else None
    cross: dict[tuple[str, str], dict[str, Any]] = {}
    for (a, b), e in sorted(emap.items()):
        pa2, pb2 = pkg(a), pkg(b)
        if pa2 and pb2 and pa2 != pb2 and e['type'] != 'HANDLES':
            cross.setdefault((pa2, pb2), e)
    for (p1, p2), e in sorted(cross.items()):
        if p1 < p2 and (p2, p1) in cross:
            back: dict[str, Any] = cross[(p2, p1)]
            add({'id': _fid('PACKAGE_CYCLE', [p1, p2]), 'kind': 'PACKAGE_CYCLE', 'level': 'candidate',
                 'anchor': e['from'], 'related': [e['to'], back['from'], back['to']],
                 'title': f'Packages {p1} and {p2} call each other',
                 'detail': 'Bidirectional dependency between top-level packages in the loaded graph.',
                 'evidence': [{'path': [e['from'], e['to']], 'edges': _edge_ev([e['from'], e['to']], emap)},
                              {'path': [back['from'], back['to']], 'edges': _edge_ev([back['from'], back['to']], emap)}]})

    for f in findings:
        f.setdefault('tags', [])
    order: dict[str, int] = {'candidate': 0, 'fact': 1}
    findings.sort(key=lambda f: (order[f['level']], any(t.startswith('hub_medium') for t in f['tags']), f['kind'], f['anchor'], f['id']))
    return {'findings': findings,
            'scope': {'roots_with_loaded_paths': len(live_roots), 'max_depth': MAX_DEPTH,
                      'counts_by_kind': dict(sorted(per_kind.items())),
                      'capped_kinds': sorted(k for k, v in per_kind.items() if v > MAX_FINDINGS_PER_KIND),
                      'cap_per_kind': MAX_FINDINGS_PER_KIND,
                      'utility_fanin_threshold': utility_threshold,
                      'hubs': [{'id': k, 'fan_in': v} for k, v in sorted(fanin.items(), key=lambda kv: (-kv[1], kv[0]))
                               if v >= utility_threshold and k in nodes and nodes[k].get('kind') != 'External'][:25],
                      'note': 'Computed only over expanded neighbourhoods; absence of a finding is not evidence of absence.'}}
