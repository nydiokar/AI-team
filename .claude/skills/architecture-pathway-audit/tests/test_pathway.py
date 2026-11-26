"""Unit tests (no CBM process): AST route adapter, structural rules, CBM paging, cache invalidation."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import tracemalloc
import unittest
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import cbm_client  # noqa: E402
import pyast_adapter as pa  # noqa: E402
import structure  # noqa: E402

spec = importlib.util.spec_from_file_location('pathway_explorer', SCRIPTS / 'pathway_explorer.py')
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

FIXTURE = {
    'app/main.py': '''
        from fastapi import FastAPI
        from app.routes import orders, health
        app = FastAPI()
        app.include_router(orders.build_router(), prefix="/v1")
        app.include_router(health.router)
        def mount_extra(target):
            @target.get("/extra")
            def extra():
                return 1
        mount_extra(app)
        app.add_api_route("/legacy", health.ping, methods=["PUT"])
    ''',
    'app/routes/health.py': '''
        from fastapi import APIRouter
        router = APIRouter(prefix="/sys")
        @router.get("/health")
        async def ping():
            return {"ok": True}
        @router.websocket("/ws")
        async def ws(sock):
            await sock.accept()
        def build_disabled(flag):
            r2 = APIRouter()
            if not flag:
                return r2
            @r2.get("/never-mounted")
            def nm():
                return 0
            return r2
    ''',
    'app/routes/orders.py': '''
        from fastapi import APIRouter
        from app.services import checkout, legacy
        def build_router():
            router = APIRouter(prefix="/orders")
            @router.post("/a")
            def create_a():
                return checkout.submit(1)
            @router.post("/{order_id}/b")
            def create_b(order_id: str):
                helper()
                return checkout.submit(2)
            @router.api_route("/c", methods=["POST", "PATCH"])
            def create_c():
                return legacy.write_order(3)
            def helper():
                return checkout.validate(0)
            return router
    ''',
    'app/services/checkout.py': '''
        from app.services import store
        def validate(x):
            return x
        def submit(x):
            validate(x)
            return store.persist_order(x)
    ''',
    'app/services/legacy.py': '''
        from app.services import store
        def write_order(x):
            return store.persist_order(x)
    ''',
    'app/services/store.py': '''
        def persist_order(x):
            return x
    ''',
}


def write_fixture(root: Path) -> None:
    for rel, body in FIXTURE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip(), encoding='utf-8')
    for pkg in ('app', 'app/routes', 'app/services'):
        (root / pkg / '__init__.py').write_text('', encoding='utf-8')


def inventory(root: Path) -> list:
    files = {p.relative_to(root).as_posix(): pa.parse_file(p.read_text(encoding='utf-8'), p.relative_to(root).as_posix())
             for p in root.rglob('*.py')}
    return pa.compose_routes(files)


class TestFastAPIAdapter(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.root = Path(self.t.name)
        write_fixture(self.root)
        self.inv = inventory(self.root)
        self.by = {(r['method'], r['path']): r for r in self.inv['routes']}

    def tearDown(self):
        self.t.cleanup()

    def test_prefixes_and_factory_composition(self):
        self.assertIn(('POST', '/v1/orders/a'), self.by)
        self.assertIn(('POST', '/v1/orders/{order_id}/b'), self.by)
        self.assertIn(('GET', '/sys/health'), self.by)
        r = self.by[('POST', '/v1/orders/a')]
        self.assertEqual(r['status'], 'registered')
        self.assertTrue(r['handler']['nested'])
        self.assertEqual(r['handler']['qual'], 'build_router.create_a')
        self.assertEqual(r['cbm_route_qn'], '__route__POST__/v1/orders/a')
        self.assertEqual(self.by[('POST', '/v1/orders/{order_id}/b')]['cbm_route_qn'], '__route__POST__/v1/orders/{}/b')

    def test_api_route_methods_websocket_and_add_api_route(self):
        self.assertIn(('POST', '/v1/orders/c'), self.by)
        self.assertIn(('PATCH', '/v1/orders/c'), self.by)
        self.assertIn(('WS', '/sys/ws'), self.by)
        legacy = self.by[('PUT', '/legacy')]
        self.assertEqual(legacy['handler']['qual'], 'ping')
        self.assertEqual(legacy['handler']['file'], 'app/routes/health.py')

    def test_router_passed_as_parameter(self):
        r = self.by[('GET', '/extra')]
        self.assertEqual(r['status'], 'registered')
        self.assertEqual(r['mount_chain'][0]['via'], 'parameter target')

    def test_unmounted_and_conditional(self):
        r = self.by[('GET', '/never-mounted')]
        self.assertEqual(r['status'], 'router_not_mounted')
        self.assertTrue(r['conditional_registration'])
        self.assertFalse(self.by[('GET', '/sys/health')]['conditional_registration'])

    def test_call_sites_await_and_nested_exclusion(self):
        tree = pa.ast.parse(textwrap.dedent('''
            async def f(x):
                await g(x)
                asyncio.create_task(h())
                loop.run_in_executor(None, worker)
                def inner():
                    hidden()
                return obj.method()
        '''))
        calls = {(c['text'], c['kind'], c['awaited']) for c in pa.call_sites(tree.body[0])}
        self.assertIn(('g', 'call', True), calls)
        self.assertIn(('h', 'spawned_call', False), calls)
        self.assertIn(('worker', 'spawn_ref', False), calls)
        self.assertIn(('obj.method', 'call', False), calls)
        self.assertNotIn('hidden', {c[0] for c in calls})

    def test_relative_and_aliased_imports(self):
        f = pa.parse_file('from .x import y as z\nimport a.b as c\n', 'pkg/mod.py')
        self.assertEqual(f['imports']['z'], 'pkg.x.y')
        self.assertEqual(f['imports']['c'], 'a.b')


class TestStructure(unittest.TestCase):
    def e(self, a, b, t='CALLS', **kw):
        return {'from': a, 'to': b, 'type': t, 'file': 'f.py', 'line': 1, 'source': 'cbm', **kw}

    def graph(self):
        nodes = {n: {'id': n, 'kind': 'Function', 'file': f'{n[0]}/x.py'} for n in
                 ['rA', 'rB', 'rC', 'hA', 'hB', 'hC', 'submit', 'validate', 'legacy', 'persist', 'util']}
        edges = [self.e('rA', 'hA', 'HANDLES'), self.e('rB', 'hB', 'HANDLES'), self.e('rC', 'hC', 'HANDLES'),
                 self.e('hA', 'submit'), self.e('hB', 'submit'), self.e('submit', 'validate'),
                 self.e('submit', 'persist'), self.e('hC', 'legacy'), self.e('legacy', 'persist')]
        return nodes, edges

    def test_bypass_detected_generically(self):
        nodes, edges = self.graph()
        res = structure.analyze(nodes, edges, ['rA', 'rB', 'rC'], {}, 10)
        byp = [f for f in res['findings'] if f['kind'] == 'BYPASS']
        self.assertEqual(len(byp), 1, res['findings'])
        self.assertEqual(byp[0]['anchor'], 'submit')
        self.assertIn('persist', byp[0]['related'])
        self.assertIn('rC', byp[0]['related'])
        self.assertEqual(byp[0]['level'], 'candidate')
        self.assertIn('not performed', byp[0]['semantic_verification'])

    def test_plain_fan_out_is_not_an_anomaly(self):
        nodes = {n: {'id': n, 'kind': 'Function', 'file': 'a/x.py'} for n in ['r', 'h', 'a', 'b', 'c']}
        edges = [self.e('r', 'h', 'HANDLES'), self.e('h', 'a'), self.e('h', 'b'), self.e('h', 'c')]
        res = structure.analyze(nodes, edges, ['r'], {}, 10)
        self.assertEqual(res['findings'], [])

    def test_shared_utility_does_not_create_bypass_or_rejoin(self):
        nodes, edges = self.graph()
        edges += [self.e('hA', 'util'), self.e('hC', 'util'), self.e('util', 'persist')]
        res = structure.analyze(nodes, edges, ['rA', 'rB', 'rC'], {'util': 50}, 10)
        self.assertFalse(any('util' in [f['anchor'], *f['related']] for f in res['findings']))

    def test_low_confidence_edges_ignored(self):
        nodes, edges = self.graph()
        edges = [e if e['to'] != 'legacy' else {**e, 'low_confidence': True} for e in edges]
        res = structure.analyze(nodes, edges, ['rA', 'rB', 'rC'], {}, 10)
        self.assertFalse(any(f['kind'] == 'BYPASS' for f in res['findings']))

    def test_per_kind_cap_is_reported(self):
        structure_cap = structure.MAX_FINDINGS_PER_KIND
        try:
            structure.MAX_FINDINGS_PER_KIND = 0
            nodes, edges = self.graph()
            res = structure.analyze(nodes, edges, ['rA', 'rB', 'rC'], {}, 10)
            self.assertEqual(res['findings'], [])
            self.assertIn('BYPASS', res['scope']['capped_kinds'])
        finally:
            structure.MAX_FINDINGS_PER_KIND = structure_cap

    def test_101_findings_are_counted_and_every_page_is_recoverable(self):
        root = 'writer'
        nodes = {root: {'kind': 'Function'}}
        edges = []
        for i in range(101):
            table = f'table::t{i:03}'
            nodes[table] = {'kind': 'Table'}
            edges.append(self.e(root, table, 'WRITES_TABLE'))
        # One structural fact per medium. Replaying reversed graph order must
        # preserve every item, including the page boundaries and known counts.
        first = structure.analyze(nodes, edges, [], {}, 10)
        kind = next(iter(first['scope']['counts_by_kind']))
        self.assertEqual(first['scope']['counts_by_kind'][kind], 101)
        self.assertEqual(first['scope']['emitted_by_kind'][kind], 40)
        self.assertEqual(first['scope']['omitted_by_kind'][kind], 61)
        second = structure.analyze(nodes, list(reversed(edges)), [], {}, 10, findings_offset=40)
        third = structure.analyze(nodes, edges, [], {}, 10, findings_offset=80)
        ids = {f['id'] for page in (first, second, third) for f in page['findings']}
        self.assertEqual(len(ids), 101)
        self.assertIsNone(third['scope']['next_findings_offset'])
        known = third['findings'][0]['id']
        repeat = structure.analyze(nodes, edges, [], {}, 10, acknowledged=frozenset({known}))
        self.assertEqual(repeat['matching_acknowledgements'], [known])
        self.assertNotIn(known, {f['id'] for f in repeat['findings']})


class FakeCBM(cbm_client.CBMClient):
    def __init__(self, pages):
        super().__init__('fake')
        self.pages = pages
        self.seen = []

    def tool(self, name, args, timeout=None):
        self.seen.append(args)
        return self.pages.pop(0)


class TestCBMClient(unittest.TestCase):
    def test_cursor_paging_and_total_check(self):
        c = FakeCBM([{'columns': ['q'], 'rows': [['a']], 'has_more': True, 'next_cursor': 'C1', 'total': 2, 'total_relation': 'eq'},
                     {'columns': ['q'], 'rows': [['b']], 'has_more': False}])
        rows, ok = c.query('p', 'MATCH (n) RETURN n.qualified_name AS q')
        self.assertEqual([r['q'] for r in rows], ['a', 'b'])
        self.assertTrue(ok)
        self.assertEqual(c.seen[1]['cursor'], 'C1')
        self.assertGreaterEqual(c.seen[0]['max_output_tokens'], 20000)

    def test_row_cap_reported_not_silent(self):
        c = FakeCBM([{'columns': ['q'], 'rows': [['a']], 'has_more': True, 'next_offset': 1}] * 3)
        rows, ok = c.query('p', 'q', page=1, max_pages=2)
        self.assertFalse(ok)
        self.assertEqual(len(rows), 2)

    def test_empty_page_with_has_more_raises(self):
        c = FakeCBM([{'columns': ['q'], 'rows': [], 'has_more': True}])
        with self.assertRaises(cbm_client.CBMError):
            c.query('p', 'q')

    def test_error_result_raises(self):
        with self.assertRaises(cbm_client.CBMError):
            cbm_client._structured({'isError': True, 'structuredContent': {'error': "unsupported function 'id' in WHERE"}})

    def test_cypher_quoting(self):
        self.assertEqual(cbm_client.cypher_str("a'b\\c"), "'a\\'b\\\\c'")


class TestEngineOffline(unittest.TestCase):
    """Engine paths that need no CBM process: AST routes, nested-handler linking, cache invalidation."""

    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        base = Path(self.t.name)
        self.scope = base / 'repo'
        write_fixture(self.scope)
        self.eng = pe.Engine(self.scope, 'fixture', base / 'cache', base / 'out.html', base / 'f.json', None)
        self.eng.sync_files()
        self.eng.cache['symbols'] = []  # no CBM symbols: every definition becomes an AST node
        self.eng.cache['fanin'] = {}
        self.eng.ready.set()

    def tearDown(self):
        self.t.cleanup()

    def rid(self, method, path):
        return next(r['id'] for r in self.eng.inventory['routes'] if r['method'] == method and r['path'] == path)

    def test_route_to_nested_handler_to_module_functions(self):
        rid = self.rid('POST', '/v1/orders/{order_id}/b')
        res = self.eng.expand_bfs(rid, 'out', 3, 100)
        self.assertFalse(res['truncated'])
        nodes, edges, _ = self.eng.graph()
        pairs = {(nodes[e['from']]['label'], e['type'], nodes[e['to']]['label']) for e in edges}
        self.assertIn(('POST /v1/orders/{order_id}/b', 'HANDLES', 'create_b'), pairs)
        self.assertIn(('create_b', 'CALLS', 'helper'), pairs)        # nested sibling via lexical scope
        self.assertIn(('create_b', 'CALLS', 'submit'), pairs)        # module import
        self.assertIn(('helper', 'CALLS', 'validate'), pairs)
        self.assertIn(('submit', 'CALLS', 'persist_order'), pairs)

    def test_upstream_registration_chain(self):
        rid = self.rid('POST', '/v1/orders/a')
        entry, hit = self.eng.expand(rid, 'in')
        self.assertFalse(hit)
        kinds = {(e['type'], e['from'].split('.')[-1]) for e in entry['edges']}
        self.assertIn(('REGISTERS', 'build_router'), kinds)
        self.assertIn(('INCLUDES', 'main'), kinds)  # module-level app composes the factory

    def test_cache_reuse_and_targeted_invalidation(self):
        self.eng.expand_bfs(self.rid('POST', '/v1/orders/c'), 'out', 3, 100)
        self.eng.expand_bfs(self.rid('POST', '/v1/orders/a'), 'out', 3, 100)
        self.eng.persist()
        n_before = len(self.eng.cache['entries'])
        again = pe.Engine(self.scope, 'fixture', self.eng.dir, self.eng.out, self.eng.report, None)
        self.assertEqual(again.sync_files(), set())
        self.assertEqual(len(again.cache['entries']), n_before)
        legacy = self.scope / 'app/services/legacy.py'
        legacy.write_text(legacy.read_text() + '\ndef audit():\n    return 1\n', encoding='utf-8')
        changed = again.sync_files()
        self.assertEqual(changed, {'app/services/legacy.py'})
        dropped = again.invalidate(changed)
        self.assertGreater(dropped, 0)
        keys = set(again.cache['entries'])
        self.assertFalse(any('legacy' in k for k in keys))
        self.assertTrue(any(k.startswith('out|') and 'submit' in k for k in keys), keys)  # untouched work kept

    def test_touch_without_content_change_is_not_a_change(self):
        p = self.scope / 'app/services/store.py'
        p.write_text(p.read_text(), encoding='utf-8')
        import os
        os.utime(p, None)
        self.assertEqual(self.eng.sync_files(), set())

    def test_bom_file_parsed_and_syntax_error_recorded(self):
        (self.scope / 'app/bom.py').write_bytes(b'\xef\xbb\xbfdef f():\n    return 1\n')
        (self.scope / 'app/broken.py').write_text('def (:\n', encoding='utf-8')
        self.eng.sync_files()
        self.assertEqual([d['name'] for d in self.eng.cache['facts']['app/bom.py']['defs']], ['f'])
        self.assertIn('SyntaxError', self.eng.cache['facts']['app/broken.py']['parse_error'])
        self.assertTrue(self.eng.coverage({}, [])['parse_errors'])

    def test_report_and_html_written(self):
        self.eng.expand_bfs(self.rid('POST', '/v1/orders/a'), 'out', 2, 100)
        self.eng.persist()
        rep = json.loads(self.eng.report.read_text(encoding='utf-8'))
        self.assertEqual(rep['coverage']['routes_discovered'], len(self.eng.inventory['routes']))
        html = self.eng.out.read_text(encoding='utf-8')
        self.assertIn('Execution Path Explorer', html)
        self.assertNotIn('/*__INITIAL_STATE__*/null', html)

    def test_fanin_and_route_queries_cannot_certify_truncated_paging(self):
        self.eng.cache['fanin'] = None
        with patch.object(self.eng, '_q', return_value=([], False)):
            with self.assertRaises(cbm_client.CBMError):
                self.eng.fanin()
            with self.assertRaises(cbm_client.CBMError):
                self.eng.cbm_routes()

    def test_source_hash_detects_same_size_same_timestamp_edit(self):
        import os
        path = self.scope / 'app/services/store.py'
        stamp = path.stat()
        path.write_text(path.read_text().replace('return x', 'return 0'))
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.eng.sync_files(), {'app/services/store.py'})

    def test_changed_source_prevents_final_artifact_and_oversize_drops_old_facts(self):
        path = self.scope / 'app/services/store.py'
        path.write_bytes(b' ' * (pe.MAX_SOURCE_BYTES + 1))
        with self.assertRaisesRegex(cbm_client.CBMError, 'Source inventory changed'):
            self.eng.persist(verify_source=True)
        self.assertIn('app/services/store.py', self.eng.sync_files())
        self.assertNotIn('app/services/store.py', self.eng.cache['facts'])
        self.assertIn('app/services/store.py', self.eng.coverage({}, [])['skipped_files'])

    def test_distinct_sql_execution_lines_survive_graph_merge(self):
        path = self.scope / 'app/ledger.py'
        path.write_text('QUERY = "SELECT * FROM test ORDER BY id ASC LIMIT 2"\n'
                        'def read(conn):\n'
                        '    conn.execute("SELECT * FROM test")\n'
                        '    conn.execute(QUERY)\n')
        self.eng.sync_files()
        _, edges, _ = self.eng.graph()
        reads = [e for e in edges if e['to'] == 'table::test']
        self.assertEqual([e['line'] for e in reads], [3, 4])
        self.assertEqual(reads[1]['props']['declaration_lines'], [1])

    def test_incomplete_query_is_retried_and_caps_are_distinct(self):
        rid = self.rid('POST', '/v1/orders/a')
        route, _ = self.eng.expand(rid)
        route['complete'] = False
        self.assertFalse(self.eng.expand(rid)[1])
        self.assertTrue(self.eng.expand_bfs(rid, 'out', 0, 100)['depth_limited'])
        self.assertTrue(self.eng.expand_bfs(rid, 'out', 3, 1)['truncated'])

    def test_browser_http_boundary_retains_admission_through_work(self):
        entered = threading.Event()
        release = threading.Event()
        original_state = self.eng.state

        def blocked_state() -> dict:
            entered.set()
            assert release.wait(10)
            return original_state()

        with pe.ExplorerServer(0, self.eng) as server, ThreadPoolExecutor(max_workers=100) as pool:
            serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            serving.start()
            def get(path: str) -> tuple[int, dict]:
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                try:
                    conn.request('GET', path)
                    response = conn.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    conn.close()
            try:
                self.assertEqual(get('/api/expand?id=old&depth=900')[0], 400)
                self.assertEqual(get('/api/expand?id=missing')[0], 404)
                status, state = get('/api/state')
                self.assertEqual(status, 200)
                self.assertEqual(state['run']['status'], 'partial')
                self.eng.ready_error = 'fixture index unavailable'
                self.assertEqual(get('/api/search?q=submit'), (
                    503, {'error': self.eng.ready_error, 'kind': 'index_unavailable'},
                ))
                self.eng.ready_error = None
                with patch.object(self.eng, 'state', side_effect=blocked_state):
                    tracemalloc.start()
                    tasks = [pool.submit(get, '/api/state') for _ in range(100)]
                    self.assertTrue(entered.wait(5))
                    # Four requests retain slots while state is unavailable. The
                    # other 96 must finish without waiting for release.
                    pending = set(tasks)
                    rejected = []
                    for _ in range(96):
                        done, pending = wait(pending, timeout=3, return_when=FIRST_COMPLETED)
                        self.assertTrue(done or len(rejected) == 96)
                        rejected.extend(f.result() for f in done)
                        if len(rejected) == 96:
                            break
                    self.assertEqual(len(rejected), 96)
                    self.assertTrue(all(code == 503 and body['kind'] == 'overloaded' for code, body in rejected))
                    peak = tracemalloc.get_traced_memory()[1]
                    self.assertLess(peak, 16 * 1024 * 1024)
                    print(f'100 simultaneous requests: 4 admitted, 96 overloaded; Python peak {peak} bytes')
                    release.set()
                    self.assertTrue(all(f.result()[0] == 200 for f in pending))
            finally:
                tracemalloc.stop()
                release.set()
                server.shutdown()
                serving.join(5)

    def test_cache_identity_and_corruption_are_visible(self):
        self.eng.persist()
        with patch.object(pe.platform, 'python_version', return_value='3.13.0-test'):
            newer = pe.Engine(self.scope, 'fixture', self.eng.dir, self.eng.out, self.eng.report, None)
        self.assertEqual(newer.cache_state, 'identity_changed')
        self.assertFalse(newer.cache['facts'])
        self.eng.cache_path.write_text('{bad')
        recovered = pe.Engine(self.scope, 'fixture', self.eng.dir, self.eng.out, self.eng.report, None)
        recovered.sync_files()
        recovered.ready.set()
        self.assertEqual(recovered.cache_state, 'invalid')
        self.assertEqual(recovered.state()['run']['status'], 'incomplete')


class TestExplorerCLI(unittest.TestCase):
    def test_missing_dependency_and_bad_scope_invalidate_clean_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / 'app.py').write_text('def example():\n    return 1\n')
            report, html = repo / 'out.json', repo / 'out.html'
            args = [sys.executable, str(SCRIPTS / 'pathway_explorer.py'), '--repo', d,
                    '--cache-dir', str(repo / 'cache'), '--project', 'test', '--no-serve',
                    '--cbm', str(repo / 'missing-cbm'), '--out', 'out.html', '--report', 'out.json']
            for scope in ('.', 'typo'):
                with self.subTest(scope=scope):
                    report.write_text('{"status": "completed", "findings": []}')
                    html.write_text('stale clean snapshot')
                    run = subprocess.run([*args, '--scope', scope], capture_output=True, timeout=10)
                    self.assertEqual(run.returncode, 2, run.stderr)
                    result = json.loads(report.read_text())
                    self.assertEqual(result['run']['status'], 'failed')
                    self.assertTrue(result['run']['errors'])
                    self.assertNotIn('stale clean snapshot', html.read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
