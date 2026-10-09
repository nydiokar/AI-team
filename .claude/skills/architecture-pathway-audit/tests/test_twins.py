"""Behavioural twin rules + baseline tagging, on synthetic input."""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pathway_explorer as pe  # noqa: E402
from pyast_adapter import fingerprint  # noqa: E402
from structure import analyze  # noqa: E402

BODY = '''
def {n}(self, {a}):
    row = self.db.fetch("SELECT * FROM orders WHERE id=?", {a})
    if row is None:
        raise KeyError("{m}")
    total = sum(i.price for i in row.items)
    self.audit.record("{m}", {a})
    return total
'''


def fp(src):
    return fingerprint(ast.parse(src).body[0])


def twin(i, name, f, tables=()):
    return {'id': i, 'name': name, 'file': 'm.py', 'line': 1, 'h': f['h'], 'size': f['size'], 'nparams': f['nparams'],
            'callees': f['callees'], 'tables': list(tables)}


def run(twins):
    nodes = {t['id']: {'id': t['id'], 'kind': 'Function', 'component': 'c', 'file': 'm.py'} for t in twins}
    return analyze(nodes, [], [], {}, 10, twins)['findings']


class TestFingerprint(unittest.TestCase):
    def test_renamed_copy_matches_and_different_body_does_not(self):
        a, b = fp(BODY.format(n='load', a='oid', m='x')), fp(BODY.format(n='fetch_it', a='key', m='other text'))
        self.assertEqual(a['h'], b['h'])
        c = fp(BODY.format(n='z', a='k', m='x').replace('sum(i.price', 'max(i.price'))
        self.assertNotEqual(a['h'], c['h'])

    def test_trivial_functions_have_no_fingerprint(self):
        self.assertIsNone(fp('def f(x):\n    return x\n'))


class TestRules(unittest.TestCase):
    def test_exact_twins_flagged_renamed(self):
        f1, f2 = fp(BODY.format(n='a', a='x', m='1')), fp(BODY.format(n='b', a='y', m='2'))
        fs = [f for f in run([twin('m.a', 'a', f1), twin('m.b', 'b', f2)]) if f['kind'] == 'BEHAVIOURAL_TWIN']
        self.assertEqual(len(fs), 1)
        self.assertIn('renamed', fs[0]['tags'])

    def test_unrelated_not_flagged(self):
        f1 = fp(BODY.format(n='a', a='x', m='1'))
        f2 = fp(BODY.format(n='b', a='y', m='2').replace('sum(', 'max('))
        self.assertEqual([f for f in run([twin('m.a', 'a', f1), twin('m.b', 'b', f2)])
                          if f['kind'] in ('BEHAVIOURAL_TWIN',)], [])

    def test_same_footprint_needs_same_tables(self):
        base = {'h': 'h1', 'size': 40, 'nparams': 2, 'callees': ['a', 'b', 'c', 'd', 'e']}
        t1 = twin('m.a', 'a', base, ['orders'])
        t2 = twin('m.b', 'b', {**base, 'h': 'h2', 'callees': ['a', 'b', 'c', 'd', 'e']}, ['orders'])
        t3 = twin('m.c', 'c', {**base, 'h': 'h3'}, ['invoices'])
        kinds = [f for f in run([t1, t2, t3]) if f['kind'] == 'SAME_FOOTPRINT']
        self.assertEqual(len(kinds), 1)
        self.assertEqual({kinds[0]['anchor'], *kinds[0]['related']}, {'m.a', 'm.b'})


class TestBaseline(unittest.TestCase):
    def test_known_and_stale(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            eng = pe.Engine(d / 'src', 'p', d / 'cache', d / 'o.html', d / 'f.json', None)
            eng.baseline_path = d / 'b.json'
            eng.baseline_path.write_text(json.dumps({'acknowledged': [
                {'id': 'k1', 'reason': 'fine', 'owner': 'me'}, {'id': 'gone', 'reason': 'x'}]}), encoding='utf-8')
            res = {'findings': [{'id': 'k1'}, {'id': 'n1'}], 'scope': {}}
            eng.apply_baseline(res)
            self.assertEqual([f['status'] for f in res['findings']], ['known', 'new'])
            self.assertEqual(res['scope']['baseline'], {'file': str(eng.baseline_path), 'known': 1, 'new': 1,
                                                       'stale_ids': ['gone']})


if __name__ == '__main__':
    unittest.main()
