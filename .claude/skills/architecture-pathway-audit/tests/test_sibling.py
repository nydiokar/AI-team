"""Generic SIBLING_OVERLAP rule: synthetic graphs only, no project names."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from structure import analyze  # noqa: E402


def graph(calls):
    nodes, edges = {}, []
    for a, bs in calls.items():
        for n in (a, *bs):
            nodes.setdefault(n, {'id': n, 'kind': 'Method', 'component': n.rsplit('.', 1)[0], 'file': 'm/x.py'})
        edges += [{'from': a, 'to': b, 'type': 'CALLS', 'file': 'm/x.py', 'line': 1, 'source': 'cbm'} for b in bs]
    return nodes, edges


SHARED = ['m.core.s1', 'm.core.s2', 'm.core.s3', 'm.core.s4']


def run(calls):
    nodes, edges = graph(calls)
    return [f for f in analyze(nodes, edges, [], {}, 10)['findings'] if f['kind'] == 'SIBLING_OVERLAP']


class TestSibling(unittest.TestCase):
    def test_parallel_implementations_in_one_class_are_flagged(self):
        fs = run({'m.C.run_a': SHARED + ['m.core.only_a'], 'm.C.run_b': SHARED + ['m.core.only_b']})
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]['level'], 'candidate')
        self.assertEqual(set([fs[0]['anchor'], *fs[0]['related']]), {'m.C.run_a', 'm.C.run_b'})

    def test_unrelated_siblings_not_flagged(self):
        self.assertEqual(run({'m.C.a': ['m.x.1', 'm.x.2', 'm.x.3'], 'm.C.b': ['m.y.1', 'm.y.2', 'm.y.3']}), [])

    def test_wrapper_that_calls_sibling_is_not_flagged(self):
        calls = {'m.C.inner': SHARED + ['m.core.o'], 'm.C.outer': ['m.C.inner'] + SHARED}
        self.assertEqual(run(calls), [])

    def test_different_classes_not_flagged(self):
        self.assertEqual(run({'m.C.a': SHARED + ['m.o.a'], 'm.D.a': SHARED + ['m.o.b']}), [])


if __name__ == '__main__':
    unittest.main()
