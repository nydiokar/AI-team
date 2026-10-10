"""Shared-medium rules on synthetic graphs (no project names) + SQL extraction."""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pyast_adapter import data_access  # noqa: E402
from structure import analyze  # noqa: E402


def fn(src):
    return ast.parse(src).body[0]


def acc(table, mode, op, window='full'):
    return (table, mode, op, window)


def run(accesses):
    """accesses: {function: [(table, mode, op, window)]} -> findings"""
    nodes, edges = {}, []
    for f, items in accesses.items():
        nodes[f] = {'id': f, 'kind': 'Function', 'component': 'm', 'file': 'm.py'}
        for t, mode, op, window in items:
            nodes.setdefault('table::' + t, {'id': 'table::' + t, 'kind': 'Table', 'label': t, 'file': ''})
            edges.append({'from': f, 'to': 'table::' + t, 'type': 'READS_TABLE' if mode == 'read' else 'WRITES_TABLE',
                          'file': 'm.py', 'line': 1, 'source': 'ast', 'props': {'op': op, 'window': window}})
    return analyze(nodes, edges, [], {}, 10)['findings']


def kinds(fs):
    return {f['kind'] for f in fs}


class TestExtraction(unittest.TestCase):
    def test_sql_tables_and_windows(self):
        a = data_access(fn('def f(c):\n    c.execute("SELECT * FROM events WHERE a=1 LIMIT 500")\n'
                           '    c.execute("INSERT INTO outbox (a) VALUES (1)")\n'
                           '    c.execute("DELETE FROM stale WHERE a=1")\n'
                           '    c.execute("SELECT x FROM log ORDER BY id DESC LIMIT 5")\n'
                           '    c.execute("SELECT x FROM one WHERE a=1 LIMIT 1")\n'))
        got = {(x['table'], x['mode'], x['op'], x['window']) for x in a}
        self.assertEqual(got, {('events', 'read', 'select', 'oldest_n'), ('outbox', 'write', 'insert', 'full'),
                               ('stale', 'write', 'delete', 'full'), ('log', 'read', 'select', 'newest_n'),
                               ('one', 'read', 'select', 'full')})

    def test_prose_is_not_sql(self):
        self.assertEqual(data_access(fn('def f():\n    return "read from the user and update the settings later"\n')), [])


class TestRules(unittest.TestCase):
    def test_oldest_n_on_append_only_flagged_but_not_on_mutable(self):
        fs = run({'w': [acc('log', 'write', 'insert')], 'r': [acc('log', 'read', 'select', 'oldest_n')]})
        self.assertIn('TRUNCATED_WINDOW', kinds(fs))
        fs = run({'w': [acc('log', 'write', 'insert')], 'w2': [acc('log', 'write', 'delete')],
                  'r': [acc('log', 'read', 'select', 'oldest_n')]})
        self.assertNotIn('TRUNCATED_WINDOW', kinds(fs))
        fs = run({'w': [acc('log', 'write', 'insert')], 'r': [acc('log', 'read', 'select', 'newest_n')]})
        self.assertNotIn('TRUNCATED_WINDOW', kinds(fs))

    def test_orphan_medium(self):
        fs = run({'w': [acc('dead', 'write', 'insert')], 'a': [acc('ok', 'write', 'insert')], 'b': [acc('ok', 'read', 'select')]})
        self.assertEqual([f['anchor'] for f in fs if f['kind'] == 'ORPHAN_MEDIUM'], ['table::dead'])

    def test_split_readers(self):
        fs = run({'rec': [acc('a', 'write', 'insert'), acc('b', 'write', 'insert')],
                  'ra': [acc('a', 'read', 'select')], 'rb': [acc('b', 'read', 'select')]})
        self.assertIn('SPLIT_READERS', kinds(fs))

    def test_one_reader_of_both_is_not_split(self):
        fs = run({'rec': [acc('a', 'write', 'insert'), acc('b', 'write', 'insert')],
                  'ra': [acc('a', 'read', 'select'), acc('b', 'read', 'select')]})
        self.assertNotIn('SPLIT_READERS', kinds(fs))


if __name__ == '__main__':
    unittest.main()
