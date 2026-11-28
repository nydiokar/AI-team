"""Independent SQLite replay of the frozen witnesses and adversarial scorer tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BENCH: Path = Path(__file__).resolve().parents[1] / 'benchmark'
CORPUS: Path = BENCH / 'corpus'
sys.path.insert(0, str(BENCH))

from run_corpus import _child, run_case  # noqa: E402
from score import Manifest, Observation, assess_case, load_corpus, score, validate_fixture, validate_report  # noqa: E402


class TestWitnesses(unittest.TestCase):
    def test_latest_rejection_is_outside_the_oldest_window(self) -> None:
        old: dict = runpy.run_path(str(CORPUS / 'variants/latest-review/ledger.py'))
        base: dict = runpy.run_path(str(CORPUS / 'base/ledger.py'))
        with sqlite3.connect(':memory:') as conn:
            conn.execute('CREATE TABLE flow_events (id INTEGER PRIMARY KEY, spec TEXT, passed INTEGER)')
            conn.executemany('INSERT INTO flow_events (spec, passed) VALUES (?, ?)', [('other', 1)] * 4999)
            old['append_review'](conn, 'subject', 1)
            old['append_review'](conn, 'subject', 0)
            self.assertEqual(old['latest_review'](conn, 'subject'), (1,))
            self.assertEqual(base['latest_review'](conn, 'subject'), (0,))

    def test_worker_attribution_is_outside_the_membership_window(self) -> None:
        old: dict = runpy.run_path(str(CORPUS / 'variants/case-membership/ledger.py'))
        base: dict = runpy.run_path(str(CORPUS / 'base/ledger.py'))
        with sqlite3.connect(':memory:') as conn:
            conn.execute('CREATE TABLE flow_links (id INTEGER PRIMARY KEY, session TEXT, role TEXT)')
            conn.executemany('INSERT INTO flow_links (session, role) VALUES (?, ?)', ((str(i), 'member') for i in range(200)))
            old['append_link'](conn, 'newest', 'worker')
            self.assertNotIn('newest', old['role_by_session'](conn))
            self.assertEqual(base['role_by_session'](conn)['newest'], 'worker')

    def test_limit_two_distinguishes_one_from_many(self) -> None:
        control: dict = runpy.run_path(str(CORPUS / 'controls/requester-ambiguity/ledger.py'))
        with sqlite3.connect(':memory:') as conn:
            conn.execute('CREATE TABLE flow_links (id INTEGER PRIMARY KEY, session TEXT, role TEXT)')
            self.assertFalse(control['is_unique_requester'](conn))
            control['append_link'](conn, 'sole', 'worker')
            self.assertTrue(control['is_unique_requester'](conn))
            for id in range(100):
                control['append_link'](conn, f'worker-{id}', 'worker')
            self.assertFalse(control['is_unique_requester'](conn))

    def test_bypass_and_owner_disagree_about_terminal_messages(self) -> None:
        variant: Path = CORPUS / 'variants/pending-bypass'
        consumer: dict = runpy.run_path(str(variant / 'consumer.py'))
        owner: dict = runpy.run_path(str(variant / 'owner.py'))
        with sqlite3.connect(':memory:') as conn:
            conn.execute('CREATE TABLE agent_inbox (id INTEGER PRIMARY KEY, state TEXT)')
            conn.executemany('INSERT INTO agent_inbox (state) VALUES (?)', [('pending',), ('dead',), ('acked',)])
            self.assertEqual(owner['pending_for'](conn), [(1,)])
            self.assertEqual(owner['inspect_terminal'](conn), [(2,)])
            self.assertEqual(consumer['waiting'](conn), [(1,), (2,)])


def graph_report(*findings: dict, status: str = 'completed') -> dict:
    return {
        'scope': '/fixture', 'run': report_identity(status=status, stage='analysis'),
        'analysis_scope': {'omitted_by_kind': {}, 'counts_by_kind': {}, 'counts_by_level': {}, 'emitted_by_kind': {},
                           'findings_offset': 0, 'next_findings_offset': None}, 'findings': list(findings),
        'stats': {'index': {'nodes': 2, 'edges': 1, 'receipt': {'root_path': '/fixture', 'project': 'fixture'}},
                  'cbm_version': '0.11.0'},
        'coverage': {'source_files': 1, 'parse_errors': [], 'incomplete_entries': [], 'skipped_files': {},
                     'cbm_index_coverage': {key: {'count': 0} for key in ('parse_partial', 'parse_unusable', 'skipped')},
                     'sweep': {'root_selection': 'all', 'expanded_roots': 1, 'route_roots': 0, 'non_http_roots': 1,
                               'depth_limited_roots': 0, 'node_limited_roots': 0, 'incomplete_adjacency_roots': 0}},
    }


def report_identity(*, status: str, stage: str) -> dict:
    return {'status': status, 'stage': stage, 'id': '1' * 32, 'source_sha256': '2' * 64,
            'python': '3.12.16', 'started_at': '2026-11-26T00:00:00Z', 'errors': [],
            'requested_scope': ['.'], 'effective_scope': ['/fixture'],
            'required_analysis_complete': status == 'completed'}


def clean_contract() -> dict:
    fixture: Path = CORPUS / 'controls' / 'inbox-lifecycle'
    paths: list[str] = ['consumer.py', 'owner.py']
    digest = hashlib.sha256()
    for path in paths:
        digest.update(json.dumps(path).encode() + b'\0' + hashlib.sha256((fixture / path).read_bytes()).digest())
    identity = report_identity(status='completed', stage='assertions')
    identity.update(requested_scope=paths, effective_scope=paths, source_sha256=digest.hexdigest())
    return {
        'run': identity, 'ok': True, 'violations': [],
        'contract_sha256': hashlib.sha256((fixture / 'contract.yaml').read_bytes()).hexdigest(),
        'coverage': {'files': 2, 'parse_errors': [], 'skipped': [], 'uncovered_scopes': [], 'uncovered_forbidden_areas': []},
    }


class TestScorer(unittest.TestCase):
    def test_fact_substring_and_nearby_evidence_do_not_match(self) -> None:
        truth, _ = load_corpus(CORPUS)
        case = truth.cases[0]
        assert case.graph is not None
        witness = case.graph
        def finding(level: str, kind: str, line: int, prefix: str) -> dict:
            return {
                'level': level, 'kind': kind, 'anchor': witness.entity,
                'evidence_omitted': 0,
                'title': f'{witness.entity} {witness.file} {witness.line}',
                'evidence': [{'edges': [{
                    'from': f'{prefix}.ledger.latest_review', 'to': witness.entity,
                    'file': witness.file, 'line': line, 'type': witness.edge_type, 'props': {'window': witness.window},
                }]}],
            }
        base: dict = graph_report()
        candidates: dict = graph_report(
            finding('fact', 'CONVERGENCE', witness.line, 'cb-project'),
            finding('candidate', witness.kind, witness.line + 1, 'cb-project'),
        )
        self.assertEqual(assess_case(case, candidates, base), ('missed', 1))
        exact: dict = graph_report(finding('candidate', witness.kind, witness.line, 'new_project-with-different-prefix'))
        self.assertEqual(assess_case(case, exact, base), ('localized', 1))
        self.assertEqual(assess_case(case, exact, exact), ('preexisting', 1))
        sampled: dict = finding('candidate', witness.kind, witness.line + 1, 'cb-project')
        sampled['evidence_omitted'] = 10
        with self.assertRaisesRegex(ValueError, 'omitted or unaccounted'):
            assess_case(case, graph_report(sampled), base)
        with self.assertRaisesRegex(ValueError, 'omitted or unaccounted'):
            assess_case(case, exact, graph_report(sampled))

    def test_failed_or_incomplete_controls_remain_unknown(self) -> None:
        _, digest = load_corpus(CORPUS)
        with tempfile.TemporaryDirectory() as location:
            out = Path(location)
            manifest = Manifest(generation='run-' + '1' * 32, corpus_sha256=digest, status='failed')
            gen: Path = out / manifest.generation
            gen.mkdir()
            for case, data in (
                ('base', graph_report()),
                ('requester-ambiguity', graph_report(status='incomplete')),
                ('inbox-lifecycle', clean_contract()),
            ):
                raw: bytes = json.dumps(data).encode()
                (gen / f'{case}.json').write_bytes(raw)
                manifest.runs[case] = Observation(
                    status='completed', returncode=0, sha256=hashlib.sha256(raw).hexdigest(),
                    run_id=data['run']['id'], source_sha256=data['run']['source_sha256'],
                )
            (out / 'manifest.json').write_text(manifest.model_dump_json())
            results: dict = score(CORPUS, out)
            self.assertEqual(results['status'], 'incomplete')
            self.assertEqual(results['benign_controls_flagged'], {'matched': 0, 'evaluated': 1, 'labeled': 2, 'unknown': 1})
            self.assertEqual(results['candidate_localization']['unknown'], 2)

    def test_completed_label_without_producer_evidence_is_rejected(self) -> None:
        incomplete: dict = {'run': {'status': 'completed'}, 'findings': [], 'analysis_scope': {'omitted_by_kind': {}}}
        with self.assertRaisesRegex(ValueError, 'execution or source identity'):
            validate_report(incomplete, graph=True, returncode=0)
        incomplete = graph_report()
        incomplete['coverage']['sweep']['depth_limited_roots'] = 1
        with self.assertRaisesRegex(ValueError, 'coverage evidence'):
            validate_report(incomplete, graph=True, returncode=0)

    def test_reconciles_uncapped_zero_and_rejects_missing_native_coverage(self) -> None:
        report: dict = graph_report()
        validate_report(report, graph=True, returncode=0)
        report['analysis_scope']['counts_by_kind'] = {'TRUNCATED_WINDOW': 105}
        with self.assertRaisesRegex(ValueError, 'inconsistent finding counts'):
            validate_report(report, graph=True, returncode=0)
        report = graph_report()
        report['coverage']['cbm_index_coverage']['parse_partial']['count'] = 1
        with self.assertRaisesRegex(ValueError, 'coverage evidence'):
            validate_report(report, graph=True, returncode=0)

    def test_contract_fixture_rejects_rewritten_policy(self) -> None:
        report: dict = clean_contract()
        fixture: Path = CORPUS / 'controls' / 'inbox-lifecycle'
        validate_fixture(report, fixture, graph=False)
        report['contract_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'frozen policy'):
            validate_fixture(report, fixture, graph=False)

    def test_manifest_rejects_report_with_mismatched_source_identity(self) -> None:
        _, digest = load_corpus(CORPUS)
        with tempfile.TemporaryDirectory() as location:
            out = Path(location)
            manifest = Manifest(generation='run-' + '1' * 32, corpus_sha256=digest, status='completed')
            gen: Path = out / manifest.generation
            gen.mkdir()
            report: dict = clean_contract()
            raw: bytes = json.dumps(report).encode()
            (gen / 'inbox-lifecycle.json').write_bytes(raw)
            manifest.runs['inbox-lifecycle'] = Observation(
                status='completed', returncode=0, sha256=hashlib.sha256(raw).hexdigest(),
                run_id=report['run']['id'], source_sha256='a' * 64,
            )
            (out / 'manifest.json').write_text(manifest.model_dump_json())
            results: dict = score(CORPUS, out)
            control: dict = next(row for row in results['cases'] if row['id'] == 'inbox-lifecycle')
            self.assertEqual(control['result'], 'unknown')
            self.assertIn('source identity differs', control['error'])

    def test_graph_failure_without_report_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as location, patch('run_corpus._child', return_value=1):
            result: Observation = run_case(None, CORPUS, Path(location), 1, None)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(result.sha256)

    def test_timeout_does_not_report_completion(self) -> None:
        with tempfile.TemporaryDirectory() as location, self.assertRaises(subprocess.TimeoutExpired):
            _child([sys.executable, '-c', 'from time import sleep; sleep(10)'], Path(location) / 'child.log', 1)


if __name__ == '__main__':
    unittest.main()
