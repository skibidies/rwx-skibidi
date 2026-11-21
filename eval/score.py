"""Score the selector against locally measured truth (eval/README.md).

For every patch in /tmp/eval/patches with a truth file, run the selector,
then report the board's cost model: busiest worker + 2 s per worker, plus
300 s per failing test we left out.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import selector as S  # noqa: E402

PATCHES = Path("/tmp/eval/patches")
TRUTH = Path("/tmp/eval/truth")
MAPS = Path(__file__).resolve().parent.parent / "maps"
SRC = Path("/tmp/eval/base")


def main() -> None:
    S.MAPS = MAPS
    S.SRC = SRC
    tests = json.loads(
        (Path(__file__).resolve().parent.parent / "sample" / "tests.json").read_text()
    )["tests"]
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    number_of = {}
    for t in tests:
        S.Maps.numbers_by_id.setdefault(t["id"], []).append(t["number"])
        number_of[t["id"]] = t["number"]
    maps = S.Maps()
    sel = S.Selector(maps)

    rows = []
    for patch in sorted(PATCHES.glob("*.patch")):
        truth_file = TRUTH / f"{patch.stem}.json"
        if not truth_file.exists():
            continue
        truth = json.loads(truth_file.read_text())
        failing = {number_of[i] for i in truth.get("failing", []) if i in number_of}
        workers = S.pack(sorted(sel.select_commit(patch.read_text())), ms)
        chosen = {n for w in workers for n in w}
        missed = failing - chosen
        loads = [sum(ms[n] for n in w) for w in workers]
        cost = (max(loads) + S.WORKER_COST_MS * len(workers)) / 1000 if workers else 0.0
        oracle_loads = []
        if failing:
            oracle_workers = S.pack(sorted(failing), ms)
            oracle_loads = [sum(ms[n] for n in w) for w in oracle_workers]
            oracle = (max(oracle_loads) + S.WORKER_COST_MS * len(oracle_workers)) / 1000
        else:
            oracle = 0.0
        rows.append((patch.stem, len(chosen), len(failing), cost, oracle, sorted(missed)))

    print(f"{'commit':10} {'sel':>4} {'fail':>4} {'cost':>7} {'oracle':>7} {'over':>7}  notes")
    tot_cost = tot_oracle = 0.0
    total_missed = 0
    for name, nsel, nfail, cost, oracle, missed in rows:
        over = cost - oracle
        tot_cost += cost
        tot_oracle += oracle
        total_missed += len(missed)
        note = ""
        if missed:
            names = ",".join(tests[n]["id"].split("::")[-1] for n in missed[:3])
            note = f"MISS {len(missed)}: {names}"
        print(f"{name:10} {nsel:4} {nfail:4} {cost:7.2f} {oracle:7.2f} {over:7.2f}  {note}")
    n = len(rows)
    print(
        f"\n{n} patches | cost {tot_cost:.1f} s | oracle {tot_oracle:.1f} s "
        f"| overhead {tot_cost - tot_oracle:.1f} s "
        f"({(tot_cost - tot_oracle) / max(tot_oracle, 1) * 100:.0f}%) "
        f"| missed failing tests {total_missed} | score {tot_cost + 300 * sum(1 for r in rows if r[5]):.1f}"
    )


if __name__ == "__main__":
    main()
