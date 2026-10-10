"""Architecture contract checker: deterministic end-state assertions (no project names)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from contract_check import Contract, check, load_contract, scan_python, scan_text  # noqa: E402

PY = '''"""Module doc mentioning old_send."""
from pkg.legacy import old_send
import json

QUERY = "INSERT INTO outbox (a) VALUES (1)"


class Backend:
    def old_send(self, m):
        """Docstring: replaces nothing."""
        return run_legacy_turn(m)

    def send(self, m):
        tool = getattr(self, "old_send")
        return self.old_send(m)


def writer(c):
    c.execute("INSERT INTO outbox (a) VALUES (1)")


def reader(c):
    return c.execute("SELECT * FROM outbox WHERE a = 1").fetchall()
'''


def kinds(occ, token):
    return sorted({o['kind'] for o in occ if o['token'] == token})


class TestScan(unittest.TestCase):
    def test_python_tokens_by_kind(self):
        occ, _acc = scan_python(PY, 'm.py')
        self.assertEqual(kinds(occ, 'old_send'), ['def', 'doc', 'import', 'ref', 'string'])
        self.assertEqual(kinds(occ, 'run_legacy_turn'), ['ref'])
        self.assertEqual(kinds(scan_python('def g(a, *, managed=None):\n    pass\n', 'g.py')[0], 'managed'), ['def'])
        # comments are not tokens; the module docstring is 'doc', never a violation by itself
        self.assertTrue(all(o['line'] >= 1 for o in occ))

    def test_python_store_access_with_qualnames(self):
        _occ, acc = scan_python(PY, 'm.py')
        got = sorted((a['where'], a['table'], a['mode']) for a in acc)
        self.assertIn(('m.py::writer', 'outbox', 'write'), got)
        self.assertIn(('m.py::reader', 'outbox', 'read'), got)

    def test_text_tokens(self):
        occ = scan_text('Call `old_send` then old_sender.\n', 'p.md')
        self.assertEqual([(o['token'], o['line'], o['kind']) for o in occ if o['token'].startswith('old_')],
                         [('old_send', 1, 'text'), ('old_sender', 1, 'text')])

    def test_unparseable_python_falls_back_to_text(self):
        occ, acc = scan_python('def broken(:\n    old_send()\n', 'b.py')
        self.assertEqual(kinds(occ, 'old_send'), ['text'])
        self.assertEqual(acc, [])


class TestContract(unittest.TestCase):
    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ValueError):
            Contract.model_validate({'retire': ['x']})  # typo of 'retired' must not silently pass

    def test_waiver_needs_reason(self):
        with self.assertRaises(ValueError):
            Contract.model_validate({'retired': ['x'], 'waivers': [{'match': 'x', 'path': 'a.py'}]})

    def test_load_from_packet_block(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'JOB.md'
            p.write_text('# Job\n```yaml\nstatus: active\n```\n\n```arch-contract\nretired: [old_send]\n```\n',
                         encoding='utf-8')
            self.assertEqual(load_contract(p).retired, ['old_send'])
            p.write_text('# Job without contract\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_contract(p)


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout


class TestCheckOnGit(unittest.TestCase):
    """Half-done cutover: the old path is removed from one package and left in another."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.email', 't@t')
        git(self.repo, 'config', 'user.name', 't')
        (self.repo / 'agent').mkdir()
        (self.repo / 'worker').mkdir()
        (self.repo / 'tests').mkdir()
        (self.repo / 'agent' / 'a.py').write_text('def old_send(m):\n    return m\n', encoding='utf-8')
        (self.repo / 'worker' / 'w.py').write_text(
            'from agent.a import old_send\n\ndef run(m):\n    return old_send(m)\n\n'
            'def log(c):\n    c.execute("INSERT INTO events (a) VALUES (1)")\n', encoding='utf-8')
        (self.repo / 'tests' / 't.py').write_text('from agent.a import old_send\n', encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'before')
        self.before = git(self.repo, 'rev-parse', 'HEAD').strip()
        # the "done" commit: agent side cleaned, worker side left behind
        (self.repo / 'agent' / 'a.py').write_text('def send(m):\n    return m\n', encoding='utf-8')
        git(self.repo, 'commit', '-qam', 'cutover agent')
        self.contract = Contract.model_validate({
            'scope': ['agent', 'worker'], 'retired': ['old_send'],
            'stores': {'events': {'writers': ['agent/*']}}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_half_done_cutover_is_reported(self):
        r = check(self.contract, self.repo, 'HEAD')
        self.assertFalse(r['ok'])
        names = {(v['rule'], v['file']) for v in r['violations']}
        self.assertIn(('retired:old_send', 'worker/w.py'), names)
        self.assertNotIn(('retired:old_send', 'agent/a.py'), names)  # cleaned side is clean
        self.assertNotIn(('retired:old_send', 'tests/t.py'), names)  # out of scope
        self.assertIn(('store:events:writers', 'worker/w.py'), names)

    def test_rev_is_read_from_git_not_the_worktree(self):
        r = check(self.contract, self.repo, self.before)
        self.assertIn(('retired:old_send', 'agent/a.py'), {(v['rule'], v['file']) for v in r['violations']})

    def test_waiver_is_applied_and_reported(self):
        c = Contract.model_validate({'scope': ['agent', 'worker'], 'retired': ['old_send'], 'waivers': [
            {'match': 'retired:old_send', 'path': 'worker/*', 'reason': 'removed in follow-up job'}]})
        r = check(c, self.repo, 'HEAD')
        self.assertTrue(r['ok'])
        self.assertEqual(len(r['waived']), 2)  # import + call in worker/w.py
        self.assertEqual(r['waived'][0]['reason'], 'removed in follow-up job')

    def test_pattern_and_forbidden_store(self):
        c = Contract.model_validate({'scope': ['worker'], 'retired_patterns': ['^old_'],
                                     'stores': {'events': {'writers': []}}})
        rules = {v['rule'] for v in check(c, self.repo, 'HEAD')['violations']}
        self.assertEqual(rules, {'retired_pattern:^old_', 'store:events:writers'})

    def test_forbidden_in_area_only(self):
        c = Contract.model_validate({'scope': ['agent', 'worker'], 'forbidden_in': {'worker': ['^old_send$']}})
        r = check(c, self.repo, self.before)
        self.assertEqual({(v['rule'], v['file']) for v in r['violations']}, {('forbidden_in:worker:^old_send$', 'worker/w.py')})

    def test_forbidden_in_kinds_filter(self):
        (self.repo / 'worker' / 'm.py').write_text('def f(x):\n    return old_send(x, "old_send refused")\n', encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'm')
        c = Contract.model_validate({'scope': ['worker'], 'forbidden_in': {
            'worker/m.py': [{'pattern': '^old_send$', 'kinds': ['def', 'ref', 'import']}]}})
        self.assertEqual([v['kind'] for v in check(c, self.repo, 'HEAD')['violations']], ['ref'])
        with self.assertRaises(ValueError):
            Contract.model_validate({'forbidden_in': {'x': [{'pattern': 'a', 'kinds': ['comment']}]}})

    def test_bom_file_is_parsed_as_python(self):
        (self.repo / 'agent' / 'bom.py').write_bytes(b'\xef\xbb\xbfdef old_send():\n    pass\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'bom')
        r = check(Contract.model_validate({'scope': ['agent'], 'retired': ['old_send']}), self.repo, 'HEAD')
        self.assertEqual(r['coverage']['parse_errors'], [])
        self.assertEqual([v['kind'] for v in r['violations']], ['def'])

    def test_clean_contract_passes(self):
        c = Contract.model_validate({'scope': ['agent'], 'retired': ['old_send']})
        r = check(c, self.repo, 'HEAD')
        self.assertTrue(r['ok'])
        self.assertEqual(r['coverage']['files'], 1)


if __name__ == '__main__':
    unittest.main()
