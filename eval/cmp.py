"""Compare selector output against local ground truth (evaldata/truth)."""
import json, sys
sys.path.insert(0, '.')
import selector as S
from pathlib import Path
EVAL = Path('../evaldata')
S.MAPS = Path('maps'); S.SRC = Path('../sqlglot-src'); S.DATA = Path('sample')
tests = json.loads(Path('sample/tests.json').read_text())['tests']
by_id = {t['id']: t['number'] for t in tests}
for t in tests:
    S.Maps.numbers_by_id.setdefault(t['id'], []).append(t['number'])
m = S.Maps(); sel = S.Selector(m)
for p in sorted((EVAL / 'truth').glob('*.json')):
    tr = json.loads(p.read_text())
    patch = (EVAL / 'patches' / f'{p.stem}.patch').read_text()
    got = set(sel.select_commit(patch))
    failed = {}
    for nid in tr.get('failing', []):
        parts = nid.split('::')
        key = '::'.join(parts[:-1]) if len(parts) > 2 else nid
        num = by_id.get(nid) or by_id.get(key)
        if num is not None:
            failed[num] = nid
    miss = [v for k, v in failed.items() if k not in got]
    print(f"{p.stem:13} sel={len(got):5} fail={len(failed):4} miss={len(miss):4}")
    for v in miss[:6]:
        print("    MISS", v)
