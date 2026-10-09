"""Integration tests against the REAL codebase-memory-mcp binary (skipped when it is not installed).

Uses a disposable FastAPI fixture copied to a temp dir and indexed into its own CBM project
(`architecture-pathway-audit-selftest`); the user's project indexes are never touched.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pathway import pe, write_fixture  # noqa: E402
from cbm_client import CBMClient, CBMError, find_executable  # noqa: E402

PROJECT = 'architecture-pathway-audit-selftest'

try:
    EXE = find_executable()
except CBMError:
    EXE = None


@unittest.skipUnless(EXE, 'codebase-memory-mcp not installed')
class TestRealCBM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = tempfile.TemporaryDirectory()
        base = Path(cls.t.name)
        cls.scope = base / 'repo'
        write_fixture(cls.scope)
        cls.base = base
        cls.cbm = CBMClient(EXE, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.cbm.close()
        shutil.rmtree(cls.t.name, ignore_errors=True)

    def engine(self):
        eng = pe.Engine(self.scope, PROJECT, self.base / 'cache', self.base / 'out.html', self.base / 'f.json', self.cbm)
        eng.sync_files()
        eng.invalidate(eng.changed) if eng.cache.get('index') else None
        eng.ensure_index()
        self.assertIsNone(eng.ready_error)
        return eng

    def rid(self, eng, method, path):
        return next(r['id'] for r in eng.inventory['routes'] if r['method'] == method and r['path'] == path)

    def test_1_graph_cache_invalidation_and_detector(self):
        eng = self.engine()
        # id() is not supported by CBM 0.11: prove the failure surfaces as an error, never as a leaf
        with self.assertRaises(CBMError):
            self.cbm.query(PROJECT, 'MATCH (a) WHERE id(a) = 1 RETURN a')
        rA, rB, rC = (self.rid(eng, 'POST', p) for p in ('/v1/orders/a', '/v1/orders/{order_id}/b', '/v1/orders/c'))
        for r in (rA, rB, rC):
            eng.expand_bfs(r, 'out', 4, 100)
        nodes, edges, _ = eng.graph()
        pairs = {(nodes[e['from']]['label'], e['type'], nodes[e['to']]['label'], e['source']) for e in edges}
        self.assertIn(('create_a', 'CALLS', 'submit', 'ast'), pairs)            # nested handler: AST -> CBM symbol
        self.assertIn(('submit', 'CALLS', 'persist_order', 'cbm'), pairs)       # module function: CBM edge
        self.assertIn(('write_order', 'CALLS', 'persist_order', 'cbm'), pairs)
        submit = next(n for n in nodes.values() if n['label'] == 'submit')
        self.assertEqual(submit['source'], 'cbm')
        self.assertTrue(submit['id'].startswith(PROJECT + '.'))
        # upstream of a CBM symbol: callers come back with the right direction
        entry, _ = eng.expand(submit['id'], 'in')
        callers = {nodes.get(e['from'], {}).get('label') or e['from'].split('.')[-1] for e in entry['edges'] if e['to'] == submit['id']}
        self.assertTrue({'create_a', 'create_b'} <= callers, callers)
        # generic structural detector flags the competing path without project hints
        eng.fanin()
        f = eng.findings()['findings']
        byp = [x for x in f if x['kind'] == 'BYPASS' and x['anchor'] == submit['id']]
        self.assertTrue(byp, [x['kind'] for x in f])
        self.assertTrue(any(rC in x['related'] for x in byp))
        # warm reuse: a fresh engine on unchanged sources performs no CBM query for cached expansions
        eng.persist()
        calls_before = self.cbm.calls
        eng2 = pe.Engine(self.scope, PROJECT, self.base / 'cache', self.base / 'out.html', self.base / 'f.json', self.cbm)
        self.assertEqual(eng2.sync_files(), set())
        eng2.ready.set()
        res = eng2.expand_bfs(rA, 'out', 4, 100)
        self.assertEqual(res['cbm_expansions'], 0)
        self.assertEqual(self.cbm.calls, calls_before)
        # modify one file: only affected entries are dropped, and the new call appears after re-index
        legacy = self.scope / 'app/services/legacy.py'
        legacy.write_text('from app.services import store\n\n\ndef audit_hook(x):\n    return x\n\n\n'
                          'def write_order(x):\n    audit_hook(x)\n    return store.persist_order(x)\n', encoding='utf-8')
        eng3 = pe.Engine(self.scope, PROJECT, self.base / 'cache', self.base / 'out.html', self.base / 'f.json', self.cbm)
        changed = eng3.sync_files()
        self.assertEqual(changed, {'app/services/legacy.py'})
        dropped = eng3.invalidate(changed)
        kept_submit_out = f'out|{submit["id"]}' in eng3.cache['entries']
        self.assertGreater(dropped, 0)
        self.assertTrue(kept_submit_out, 'entry unrelated to the changed file must survive')
        eng3.ensure_index()
        self.assertEqual(eng3.stats['index_runs'], 1)
        eng3.expand_bfs(rC, 'out', 4, 100)
        n3, e3, _ = eng3.graph()
        labels = {(n3[e['from']]['label'], n3[e['to']]['label']) for e in e3 if e['type'] == 'CALLS'}
        self.assertIn(('write_order', 'audit_hook'), labels)


if __name__ == '__main__':
    unittest.main(verbosity=2)
