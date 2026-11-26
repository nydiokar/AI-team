"""Score the portable corpus by matching independently labeled source evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class GraphWitness(StrictModel):
    kind: str
    entity: str
    file: str
    line: int = Field(ge=1)
    edge_type: str
    window: str


class ContractWitness(StrictModel):
    rule: str
    file: str
    line: int = Field(ge=1)
    kind: str


class Case(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9-]*$')
    classification: Literal['defect', 'benign']
    description: str
    graph: GraphWitness | None = None
    contract: ContractWitness | None = None

    @model_validator(mode='after')
    def exactly_one_witness(self) -> Case:
        if (self.graph is None) == (self.contract is None):
            raise ValueError('each case needs exactly one graph or contract witness')
        return self

    @property
    def path(self) -> Path:
        return Path('variants' if self.classification == 'defect' else 'controls') / self.id


class GroundTruth(StrictModel):
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_ids(self) -> GroundTruth:
        names: list[str] = [case.id for case in self.cases]
        if 'base' in names or len(set(names)) != len(names):
            raise ValueError('case names must be unique and may not use the reserved base name')
        return self


class Observation(StrictModel):
    status: Literal['completed', 'failed']
    returncode: int | None = None
    sha256: str | None = None
    run_id: str | None = None
    source_sha256: str | None = None
    error: str | None = None


class Manifest(StrictModel):
    schema_version: Literal[1] = 1
    generation: str = Field(pattern=r'^run-[0-9a-f]{32}$')
    corpus_sha256: str
    status: Literal['running', 'completed', 'failed']
    runs: dict[str, Observation] = Field(default_factory=dict)


def load_corpus(corpus: Path) -> tuple[GroundTruth, str]:
    labels: bytes = (corpus / 'GROUND_TRUTH.json').read_bytes()
    gt: GroundTruth = GroundTruth.model_validate_json(labels)
    files: list[Path] = []
    for case_path in [Path('base'), *(case.path for case in gt.cases)]:
        path: Path = corpus / case_path
        if not path.is_dir():
            raise ValueError(f'missing fixture: {case_path}')
        for file in path.rglob('*'):
            if {'.git', '__pycache__'} & set(file.relative_to(path).parts):
                continue
            if file.is_symlink():
                raise ValueError(f'fixture contains a symbolic link: {file.relative_to(corpus)}')
            if file.is_file():
                files.append(file)
    digest = hashlib.sha256(labels)
    for file in sorted(files):
        digest.update(file.relative_to(corpus).as_posix().encode('utf-8'))
        digest.update(b'\x00')
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    return gt, digest.hexdigest()


def validate_report(report: dict, *, graph: bool, returncode: int) -> tuple[str, str]:
    if not isinstance(report, dict) or report.get('run', {}).get('status') != 'completed':
        raise ValueError('required analysis did not complete')
    run: dict = report['run']
    run_id: str = run.get('id') or ''
    source_sha: str = run.get('source_sha256') or ''
    coverage: dict = report.get('coverage') or {}
    expected_stage: str = 'analysis' if graph else 'assertions'
    if (not re.fullmatch('[0-9a-f]{32}', run_id) or not re.fullmatch('[0-9a-f]{64}', source_sha)
            or run.get('stage') != expected_stage or run.get('errors') != []
            or not run.get('started_at') or not run.get('python')
            or not run.get('requested_scope') or not run.get('effective_scope') or not coverage):
        raise ValueError('completed report lacks execution or source identity')
    if graph:
        scope: dict = report.get('analysis_scope') or {}
        sweep: dict = coverage.get('sweep') or {}
        stats: dict = report.get('stats') or {}
        if (returncode != 0 or not isinstance(report.get('findings'), list)
                or 'omitted_by_kind' not in scope or any(scope['omitted_by_kind'].values())
                or not stats.get('index') or not stats.get('cbm_version')
                or not coverage.get('source_files') or coverage.get('parse_errors') != []
                or coverage.get('incomplete_entries') != [] or coverage.get('skipped_files') != {}
                or sweep.get('root_selection') != 'all'
                or sweep.get('expanded_roots') != sweep['route_roots'] + sweep['non_http_roots']
                or any(sweep.get(key) != 0 for key in
                       ('depth_limited_roots', 'node_limited_roots', 'incomplete_adjacency_roots'))
                or run['effective_scope'] != [report.get('scope')]):
            raise ValueError('graph report failed, omitted findings, or lacks coverage evidence')
    else:
        if (not isinstance(report.get('violations'), list) or returncode != (1 if report['violations'] else 0)
                or report.get('ok') is not (returncode == 0)):
            raise ValueError('contract exit status and violations do not agree')
        if (not coverage.get('files')
                or any(coverage.get(key) != [] for key in
                       ('parse_errors', 'skipped', 'uncovered_scopes', 'uncovered_forbidden_areas'))):
            raise ValueError('contract lacks complete coverage evidence')
    return run_id, source_sha


def _read_report(out: Path, manifest: Manifest, case: str, *, graph: bool) -> dict:
    record: Observation | None = manifest.runs.get(case)
    if record is None or record.status != 'completed' or record.returncode is None:
        raise ValueError(record.error if record and record.error else 'missing completed observation')
    raw: bytes = (out / manifest.generation / f'{case}.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.sha256:
        raise ValueError('report digest differs from the completed observation')
    report: dict = json.loads(raw)
    run_id, source_sha = validate_report(report, graph=graph, returncode=record.returncode)
    if (run_id, source_sha) != (record.run_id, record.source_sha256):
        raise ValueError('run or source identity differs from the completed observation')
    return report


def _graph_matches(finding: dict, witness: GraphWitness) -> bool:
    if (finding.get('level'), finding.get('kind'), finding.get('anchor')) != ('candidate', witness.kind, witness.entity):
        return False
    return any(
        (edge.get('file'), edge.get('line'), edge.get('to'), edge.get('type'), (edge.get('props') or {}).get('window'))
        == (witness.file, witness.line, witness.entity, witness.edge_type, witness.window)
        for evidence in finding.get('evidence', []) for edge in evidence.get('edges', [])
    )


def assess_case(case: Case, report: dict, base: dict) -> tuple[str, int]:
    """Compare candidate localization or a deterministic policy violation to a frozen witness."""
    if case.graph:
        candidates: list[dict] = [f for f in report['findings'] if f.get('level') == 'candidate']
        matched: bool = any(_graph_matches(f, case.graph) for f in candidates)
        preexisting: bool = any(_graph_matches(f, case.graph) for f in base['findings'])
        total: int = len(candidates)
        defect_status: str = 'localized' if matched and not preexisting else ('preexisting' if matched else 'missed')
        control_status: str = 'flagged' if matched and not preexisting else 'unflagged'
    else:
        assert case.contract is not None
        key: dict = case.contract.model_dump()
        violations: list[dict] = report['violations']
        matched = any(all(hit.get(field) == value for field, value in key.items()) for hit in violations)
        total = len(violations)
        defect_status = 'verified_violation' if matched else 'missed'
        control_status = 'flagged' if violations else 'unflagged'
    return (defect_status if case.classification == 'defect' else control_status), total


def score(corpus: Path, out: Path) -> dict:
    labels, digest = load_corpus(corpus)
    manifest: Manifest = Manifest.model_validate_json((out / 'manifest.json').read_bytes())
    if manifest.corpus_sha256 != digest or manifest.status == 'running':
        raise ValueError('no finished observations for the current corpus')
    rows: list[dict] = []
    try:
        base: dict = _read_report(out, manifest, 'base', graph=True)
        base_error: str | None = None
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        base = {}
        base_error = str(exc)
    for case in labels.cases:
        row: dict = {'id': case.id, 'classification': case.classification, 'engine': 'graph' if case.graph else 'contract'}
        try:
            if case.graph and base_error is not None:
                raise ValueError(f'baseline unavailable: {base_error}')
            report: dict = _read_report(out, manifest, case.id, graph=case.graph is not None)
            row['result'], row['candidate_or_violation_count'] = assess_case(case, report, base)
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
            row.update(result='unknown', error=str(exc))
        rows.append(row)

    def metric(classification: str, engine: str | None, hit: str) -> dict:
        eligible: list[dict] = [r for r in rows if r['classification'] == classification and (engine is None or r['engine'] == engine)]
        unknown: int = sum(r['result'] == 'unknown' for r in eligible)
        return {'matched': sum(r['result'] == hit for r in eligible), 'evaluated': len(eligible) - unknown,
                'labeled': len(eligible), 'unknown': unknown}

    complete: bool = manifest.status == 'completed' and base_error is None and all(r['result'] != 'unknown' for r in rows)
    return {
        'status': 'completed' if complete else 'incomplete',
        'corpus_sha256': digest,
        'candidate_localization': metric('defect', 'graph', 'localized'),
        'verified_contract_defect_recall': metric('defect', 'contract', 'verified_violation'),
        'benign_controls_flagged': metric('benign', None, 'flagged'),
        'cases': rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', type=Path)
    parser.add_argument('results', type=Path)
    args = parser.parse_args()
    try:
        summary: dict = score(args.corpus, args.results)
    except (OSError, ValueError) as exc:
        summary = {'status': 'incomplete', 'error': str(exc)}
    print(json.dumps(summary, indent=2))
    return 0 if summary['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
