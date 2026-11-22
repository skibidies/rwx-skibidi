"""Per-commit cost vs oracle on the sample set (no container needed)."""
import json, sys
sys.path.insert(0, '.')
import selector as S
from pathlib import Path
S.MAPS = Path('maps'); S.SRC = Path('../sqlglot-src'); S.DATA = Path('sample')
tests = json.loads(Path('sample/tests.json').read_text())['tests']
ms = {t['number']: int(round(t['seconds'] * 1000)) for t in tests}
for t in tests:
    S.Maps.numbers_by_id.setdefault(t['id'], []).append(t['number'])
m = S.Maps(); sel = S.Selector(m)
ans = json.load(open('sample/answers.json'))['commits']

def cost(nums):
    w = S.pack(sorted(nums), ms)
    return (max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)) / 1000 if w else 0.0

rows, miss = [], 0
for cid, v in ans.items():
    got = set(sel.select_commit((Path('sample/commits') / f'{cid}.patch').read_text()))
    fail = set(v['failing'])
    if fail - got:
        miss += 1
        print("MISS", cid, sorted(fail - got))
    rows.append((cid, v['kind'], len(got), len(fail), cost(got), cost(fail), len(got - fail)))
rows.sort(key=lambda r: -(r[4] - r[5]))
for r in rows[:6]:
    print(f"{r[0]:7} {r[1]:8} sel={r[2]:3} fail={r[3]:3} cost={r[4]:6.2f} "
          f"oracle={r[5]:6.2f} over={r[4]-r[5]:5.2f} extra={r[6]}")
print(f"total {sum(r[4] for r in rows):.2f} | oracle {sum(r[5] for r in rows):.2f} "
      f"| over {sum(r[4]-r[5] for r in rows):.2f} | misses {miss}")
