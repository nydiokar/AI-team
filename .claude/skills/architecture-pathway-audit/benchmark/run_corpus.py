"""Run the frozen detector over a blind corpus. Usage: run_corpus.py <corpus_dir> <results_dir>"""
import concurrent.futures as cf
import pathlib
import subprocess
import sys
import time

CORPUS = pathlib.Path(sys.argv[1])  # corpus root: base/, variants/, controls/, GROUND_TRUTH.json
OUT = pathlib.Path(sys.argv[2])  # results dir
OUT.mkdir(parents=True, exist_ok=True)
EXPLORER = str(pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'pathway_explorer.py')

folders = [('base', CORPUS / 'base')]
folders += [(p.name, p) for p in sorted((CORPUS / 'variants').iterdir()) if p.is_dir()]
folders += [(p.name, p) for p in sorted((CORPUS / 'controls').iterdir()) if p.is_dir()]


def run(item):
    name, path = item
    t0 = time.time()
    cmd = [sys.executable, EXPLORER, '--repo', str(path), '--scope', '.', '--project', f'cb-{name}',
           '--cache-dir', str(OUT / f'cache-{name}'), '--out', str(OUT / f'{name}.html'), '--report', str(OUT / f'{name}.json'),
           '--baseline', str(OUT / 'none.json'), '--no-serve', '--sweep-depth', '6', '--sweep-cap', '600', '--roots', 'all']
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    return name, r.returncode, round(time.time() - t0, 1), (r.stdout + r.stderr)[-300:]


with cf.ThreadPoolExecutor(3) as ex:
    for name, rc, dt, tail in ex.map(run, folders):
        print(name, rc, dt, tail.strip().splitlines()[-1] if tail.strip() else '', flush=True)
