"""A116/T1 — executable witnesses: incomplete analysis must never look like a clean gate.

These fixtures are the frozen baseline witnesses for the contract-checker defect recorded in
MEASUREMENTS.md: a typo scope, an unparseable required Python file and a skipped binary all used
to return ok=true with zero evidence. The candidate must refuse them with exit 2, and keep the
original 0/1 meanings for genuinely completed checks.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from contract_check import Contract, check, main


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class TestCheckerFailClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'mod.py').write_text('def send(m):\n    return m\n', encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, '-c', 'user.email=a@b', '-c', 'user.name=t', 'commit', '-qm', 'init')
        self.contract_file = Path(self.tmp.name) / 'contract.yaml'

    def tearDown(self):
        self.tmp.cleanup()

    def contract(self, **fields):
        return Contract.model_validate({'retired': ['never_defined_anywhere'], **fields})

    def cli(self, contract_yaml: str):
        self.contract_file.write_text(contract_yaml, encoding='utf-8')
        report = Path(self.tmp.name) / 'result.json'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['--contract', str(self.contract_file), '--repo', str(self.repo),
                         '--rev', 'HEAD', '--json', str(report)])
        return code, out.getvalue(), json.loads(report.read_text(encoding='utf-8'))

    def test_typo_scope_is_incomplete_not_clean(self):
        r = check(self.contract(scope=['typo']), self.repo, 'HEAD')
        self.assertFalse(r['ok'])
        self.assertEqual(r['status'], 'incomplete')
        self.assertFalse(r['analysis_complete'])
        self.assertIn('no files scanned', ' '.join(r['analysis_errors']))

        code, text, saved = self.cli('scope: [typo]\nretired: [old_send]\n')
        self.assertEqual(code, 2)
        self.assertIn('REQUIRED ANALYSIS INCOMPLETE', text)
        self.assertNotIn('CONTRACT MET', text)
        self.assertEqual(saved['status'], 'incomplete')

    def test_unparseable_required_python_is_incomplete(self):
        (self.repo / 'src' / 'bad.py').write_text(
            'def broken(:\n    c.execute("SELECT * FROM events")\n', encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, '-c', 'user.email=a@b', '-c', 'user.name=t', 'commit', '-qm', 'bad')
        r = check(self.contract(scope=['src'], stores={'events': {'readers': []}}), self.repo, 'HEAD')
        self.assertEqual(r['status'], 'incomplete')
        self.assertIn('src/bad.py', r['coverage']['parse_errors'])

    def test_skipped_binary_input_is_incomplete(self):
        (self.repo / 'src' / 'blob.py').write_bytes(b'\x00\x01binary')
        git(self.repo, 'add', '-A')
        git(self.repo, '-c', 'user.email=a@b', '-c', 'user.name=t', 'commit', '-qm', 'blob')
        r = check(self.contract(scope=['src']), self.repo, 'HEAD')
        self.assertEqual(r['status'], 'incomplete')
        self.assertIn('src/blob.py', r['coverage']['skipped'])

    def test_completed_checks_keep_zero_and_one(self):
        code, _, saved = self.cli('scope: [src]\nretired: [never_defined_anywhere]\n')
        self.assertEqual(code, 0)
        self.assertEqual(saved['status'], 'pass')
        (self.repo / 'src' / 'mod.py').write_text('def send(m):\n    return old_send(m)\n', encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, '-c', 'user.email=a@b', '-c', 'user.name=t', 'commit', '-qm', 'violation')
        code, text, saved = self.cli('scope: [src]\nretired: [old_send]\n')
        self.assertEqual(code, 1)
        self.assertIn('CONTRACT NOT MET', text)
        self.assertEqual(saved['status'], 'violations')

    def test_source_digest_tracks_checked_content(self):
        before = check(self.contract(scope=['src']), self.repo, 'HEAD')['source_digest']
        (self.repo / 'src' / 'mod.py').write_text('def send(m):\n    return m + 1\n', encoding='utf-8')
        git(self.repo, 'add', '-A')
        git(self.repo, '-c', 'user.email=a@b', '-c', 'user.name=t', 'commit', '-qm', 'edit')
        after = check(self.contract(scope=['src']), self.repo, 'HEAD')['source_digest']
        self.assertNotEqual(before, after)

    def test_empty_scope_is_invalid_input(self):
        with self.assertRaises(ValueError):
            self.contract(scope=[])


class TestExplorerFailureHonest(unittest.TestCase):
    """A116/T2 — a failed or partial analysis must be visible in JSON, HTML and exit status."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.scope = base / 'repo'
        (self.scope / 'app').mkdir(parents=True)
        (self.scope / 'app' / 'svc.py').write_text('def handler():\n    return 1\n', encoding='utf-8')
        self.out = base / 'pathways.html'
        self.report = base / 'findings.json'
        self.cache = base / 'cache'

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, cbm=None):
        import pathway_explorer as pe
        eng = pe.Engine(self.scope, 'fixture', self.cache, self.out, self.report, cbm)
        eng.sync_files()
        eng.ready.set()
        return eng

    def test_index_failure_is_published_as_failed(self):
        eng = self.engine()
        eng.ready_error = 'CBM unavailable: fake failure'
        eng.persist()
        saved = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(saved['run']['status'], 'failed')
        self.assertFalse(saved['run']['required_analysis_complete'])
        self.assertIn('fake failure', ' '.join(error['detail'] for error in saved['run']['errors']))
        self.assertIn('fake failure', self.out.read_text(encoding='utf-8'))  # browser state carries it

    def test_row_capped_expansion_is_incomplete(self):
        eng = self.engine()
        eng.cache['symbols'] = []
        eng.cache['fanin'] = {}
        eng.cache['cbm_routes'] = []
        eng.cache['entries']['out|svc'] = {'files': [], 'edges': [], 'unresolved': [], 'complete': False}
        eng.persist()
        saved = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(saved['run']['status'], 'incomplete')
        self.assertFalse(saved['run']['required_analysis_complete'])
        self.assertIn('incomplete entries', ' '.join(error['detail'] for error in saved['run']['errors']))

    def test_clean_run_is_complete(self):
        eng = self.engine()
        eng.cache['symbols'] = []
        eng.cache['fanin'] = {}
        eng.cache['cbm_routes'] = []
        eng.sweep_paths(1, 10, 'all')
        eng.persist()
        saved = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(saved['run']['status'], 'completed')
        self.assertTrue(saved['run']['required_analysis_complete'])
        self.assertTrue(saved['run']['source_sha256'])

    def test_corrupt_cache_is_explicit_not_silent(self):
        self.cache.mkdir(parents=True)
        (self.cache / 'pathway_cache.json').write_text('{broken', encoding='utf-8')
        eng = self.engine()
        self.assertEqual(eng.cache_state, 'invalid')

    def test_cache_identity_mismatch_is_reported(self):
        self.cache.mkdir(parents=True)
        body = {'schema': 9, 'project': 'fixture', 'scope': str(self.scope), 'python': '3.11',
                'parser': 'stdlib-ast', 'files': {}, 'facts': {}, 'entries': {}, 'nodes': {},
                'symbols': None, 'fanin': None, 'cbm_routes': None, 'index': None, 'index_token': 0}
        (self.cache / 'pathway_cache.json').write_text(json.dumps(body), encoding='utf-8')
        eng = self.engine()  # sync_files repopulates the fresh cache; stale entries must not survive
        self.assertEqual(eng.cache_state, 'identity_changed')
        self.assertEqual(eng.cache['entries'], {})
        self.assertIsNone(eng.cache['index'])

    def test_no_serve_failed_index_returns_two_with_artifact(self):
        import pathway_explorer as pe
        fake = Path(self.tmp.name) / 'fake-cbm'
        fake.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        fake.chmod(0o755)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pe.main(['--repo', str(self.scope), '--scope', '.', '--project', 'fixture-cli',
                            '--cbm', str(fake), '--no-serve', '--out', str(self.out),
                            '--report', str(self.report), '--cache-dir', str(self.cache / 'cli')])
        self.assertEqual(code, 2)
        saved = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(saved['run']['status'], 'failed')
        self.assertIn('ANALYSIS NOT COMPLETE', out.getvalue())

    def test_no_serve_unresponsive_cbm_returns_two_with_fresh_artifact(self):
        import pathway_explorer as pe
        fake = Path(self.tmp.name) / 'stalled-cbm'
        fake.write_text('#!/bin/sh\nexec sleep 30\n', encoding='utf-8')
        fake.chmod(0o755)
        self.report.write_text('{"run":{"status":"completed"}}')
        code = pe.main(['--repo', str(self.scope), '--scope', '.', '--cbm', str(fake), '--timeout', '1',
                        '--no-serve', '--out', str(self.out), '--report', str(self.report),
                        '--cache-dir', str(self.cache)])
        saved = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(code, 2)
        self.assertEqual(saved['run']['status'], 'failed')
        self.assertIn('timed out', saved['run']['errors'][0]['detail'])

    def test_viewer_surfaces_run_status(self):
        viewer = (SCRIPTS / 'viewer.html').read_text(encoding='utf-8')
        self.assertIn("'run'", viewer)
        self.assertIn('S.run?.status', viewer)
        self.assertIn('runNotice()', viewer)


if __name__ == '__main__':
    unittest.main()
