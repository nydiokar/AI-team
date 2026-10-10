"""Score detector output vs ground truth. Usage: score.py <corpus_dir> <results_dir>

A variant is only counted by touching entities via a finding that is NEW relative to base; read the output
critically: a CONVERGENCE/DIVERGE_REJOIN fact mentioning the function is not a catch of the defect."""
import json
import pathlib
import re
import sys
from collections import Counter

R = pathlib.Path(sys.argv[2])
GT = json.load(open(pathlib.Path(sys.argv[1]) / 'GROUND_TRUTH.json'))


def norm(s):
    s = re.sub(r'^cb-[a-z0-9]+\.', '', s)
    return s.replace('route::', 'R:')


def load(name):
    d = json.load(open(R / f'{name}.json'))
    out = []
    for f in d['findings']:
        ids = [norm(f['anchor'])] + [norm(x) for x in f['related']]
        text = norm(json.dumps(f))
        out.append({'kind': f['kind'], 'sig': (f['kind'], tuple(sorted(ids))), 'ids': ids, 'text': text, 'title': f['title'],
                    'tags': f.get('tags', [])})
    return out


base = load('base')
base_sigs = {f['sig'] for f in base}
base_kinds = Counter(f['kind'] for f in base)
print('BASE findings', len(base), dict(base_kinds))


def touches(f, items, tables):
    hits = []
    for it in items:
        if it in f['text']:
            hits.append(it.split('.')[-1])
    for t in tables:
        if f'table::{t}' in f['text'] or f'Table {t} ' in f['title']:
            hits.append('T:' + t)
    return hits


print('\n== DEFECT VARIANTS')
caught = 0
for v in GT['variants']:
    fs = load(v['id'])
    new = [f for f in fs if f['sig'] not in base_sigs]
    rel = []
    for f in new:
        h = touches(f, v['functions'], [])
        if h:
            rel.append((f['kind'], h, f['title'][:70]))
    rel_t = []
    for f in new:
        h = touches(f, [], v['tables'])
        if h and f['kind'] in ('ORPHAN_MEDIUM', 'TRUNCATED_WINDOW', 'SPLIT_READERS'):
            rel_t.append((f['kind'], h, f['title'][:70]))
    in_base = [f['kind'] for f in fs if f['sig'] in base_sigs and touches(f, v['functions'], [])]
    ok = bool(rel or rel_t)
    caught += ok
    print(v['id'], v['class'], 'HARD' if v.get('expected_hard') else '    ', 'CAUGHT' if ok else 'missed',
          f'new={len(new)}', rel[:2], rel_t[:2], '| already-in-base-mentions:', Counter(in_base).most_common(2))
print('recall', caught, '/', len(GT['variants']))

print('\n== CONTROLS (new findings touching the control addition = false positive)')
fp_total = 0
for k in GT['controls']:
    fs = load(k['id'])
    new = [f for f in fs if f['sig'] not in base_sigs]
    rel = [(f['kind'], touches(f, k['functions'], []), f['title'][:70]) for f in new if touches(f, k['functions'], [])]
    fp_total += bool(rel)
    print(k['id'], f'new={len(new)}', 'FALSE-POSITIVE' if rel else 'clean', rel[:3])
print('controls flagged', fp_total, '/', len(GT['controls']))

print('\n== ALL new-vs-base findings by kind across variants (noise vs signal view)')
c = Counter()
for v in GT['variants']:
    for f in load(v['id']):
        if f['sig'] not in base_sigs:
            c[f['kind']] += 1
print(dict(c))
