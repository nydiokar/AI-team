"""Run the labeled corpus, publishing a fresh manifest of complete or failed observations."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
from uuid import uuid4

from score import Case, Manifest, Observation, load_corpus, validate_fixture, validate_report


SCRIPTS: Path = Path(__file__).resolve().parents[1] / 'scripts'


def _publish(out: Path, manifest: Manifest) -> None:
    staged: Path = out / f'.{manifest.generation}.tmp'
    staged.write_text(manifest.model_dump_json(indent=2), encoding='utf-8')
    staged.replace(out / 'manifest.json')


def _child(args: list[str], log: Path, timeout: int) -> int:
    with log.open('wb') as stream:
        with subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True) as process:
            try:
                return process.wait(timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                # Only the disposable process group created above belongs to this run.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise


def run_case(case: Case | None, corpus: Path, generation: Path, timeout: int, cbm: str | None) -> Observation:
    name: str = case.id if case else 'base'
    fixture: Path = corpus / (case.path if case else 'base')
    repo: Path = generation / 'fixtures' / name
    destination: Path = generation / f'{name}.json'
    graph: bool = case is None or case.graph is not None
    if graph:
        command: list[str] = [
            sys.executable, str(SCRIPTS / 'pathway_explorer.py'), '--repo', str(repo), '--scope', '.',
            '--project', f'a116-{generation.name}-{name}', '--cache-dir', str(generation / f'cache-{name}'),
            '--out', str(generation / f'{name}.html'), '--report', str(destination),
            '--baseline', str(generation / 'no-acknowledgements.json'), '--no-serve', '--roots', 'all',
            '--sweep-depth', '12', '--sweep-cap', '600',
        ]
        if cbm:
            command.extend(['--cbm', cbm])
    else:
        command = [sys.executable, str(SCRIPTS / 'contract_check.py'), '--repo', str(repo),
                   '--contract', str(repo / 'contract.yaml'), '--json', str(destination)]
    returncode: int | None = None
    try:
        shutil.copytree(fixture, repo, ignore=shutil.ignore_patterns('__pycache__', '.git'))
        if not graph:
            initialized: int = _child(['git', 'init', '-q', str(repo)], generation / f'{name}.git.log', timeout)
            if initialized:
                raise ValueError(f'fixture git init exited {initialized}')
        returncode = _child(command, generation / f'{name}.log', timeout)
        raw: bytes = destination.read_bytes()
        report: dict = json.loads(raw)
        run_id, source_sha = validate_report(report, graph=graph, returncode=returncode)
        validate_fixture(report, fixture, graph=graph)
        if graph and report.get('scope') != str(repo):
            raise ValueError('explorer report covers a different fixture')
        return Observation(status='completed', returncode=returncode, sha256=hashlib.sha256(raw).hexdigest(),
                           run_id=run_id, source_sha256=source_sha)
    except subprocess.TimeoutExpired:
        error: str = f'child exceeded the {timeout}s deadline'
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        error = str(exc)
    return Observation(status='failed', returncode=returncode, error=error)


def run(corpus: Path, out: Path, timeout: int, cbm: str | None) -> int:
    out.mkdir(parents=True, exist_ok=True)
    with (out / '.run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('another corpus run owns this results directory') from exc
        manifest = Manifest(generation=f'run-{uuid4().hex}', corpus_sha256='', status='running')
        _publish(out, manifest)
        labels, digest = load_corpus(corpus)
        manifest.corpus_sha256 = digest
        generation: Path = out / manifest.generation
        generation.mkdir()
        for case in [None, *labels.cases]:
            name: str = case.id if case else 'base'
            record: Observation = run_case(case, corpus, generation, timeout, cbm)
            manifest.runs[name] = record
            print(name, record.status, record.error or '', flush=True)
        _, current_digest = load_corpus(corpus)
        if current_digest != digest:
            raise ValueError('corpus inputs changed during the run')
        manifest.status = 'completed' if all(r.status == 'completed' for r in manifest.runs.values()) else 'failed'
        _publish(out, manifest)
    return 0 if manifest.status == 'completed' else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', type=Path)
    parser.add_argument('results', type=Path)
    parser.add_argument('--timeout', type=int, default=120, help='per-observation deadline in seconds')
    parser.add_argument('--cbm', default=None, help='specific Codebase Memory binary')
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error('--timeout must be positive')
    try:
        return run(args.corpus.resolve(), args.results.resolve(), args.timeout, args.cbm)
    except (OSError, ValueError) as exc:
        print(f'corpus run failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
