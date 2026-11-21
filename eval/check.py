"""Fast miss detector: run only the tests that could plausibly fail.

A failing test almost always executes a changed line, so the interesting
question is not "which of all 1620 tests fail" but "of the tests that touch
the changed code, did we leave a failing one out?". That superset is small
enough to run in a minute or two, so we can check many patches per hour.

usage: python eval/check.py <short> [<short> ...]
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import selector as S  # noqa: E402

PATCHES = Path("/tmp/eval/patches")
OUT = Path("/tmp/eval/check")


def superset(sel, maps, path, info):
    """Tests that could notice this file's change, if we had no clever rules."""
    out = set()
    lines = []
    for h in info["hunks"]:
        lines.extend(ln for ln, _t in h["del"])
        lines.extend(ln for ln, _t in h["add"])
        lines.append(h["old"])
    out |= sel.tests_for_lines(path, lines)
    for ln in lines:
        span = S.innermost_span(maps.scopes.get(path, []), ln)
        if span is not None:
            out |= sel.tests_for_span(path, span)
    out |= sel.tests_for_lines(path, list(range(1, 1)))  # no-op, keeps API honest
    covering = sel.tests_covering_file(path)
    if len(covering) < 500:
        out |= covering
    out |= sel.tests_reading(path)
    out |= sel.fixture_tests(path)
    if any(not sel._inside_function(maps.scopes.get(path, []), ln) for ln in lines):
        out |= sel.tests_importing(path)
    out |= sel.select_file(path, info)
    return out


def main(shorts):
    S.MAPS = REPO / "maps"
    S.SRC = Path("/tmp/eval/base")
    tests = json.loads((REPO / "sample" / "tests.json").read_text())["tests"]
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    ids = {}
    for t in tests:
        S.Maps.numbers_by_id.setdefault(t["id"], []).append(t["number"])
        ids[t["number"]] = t["id"]
    maps = S.Maps()
    sel = S.Selector(maps)
    OUT.mkdir(parents=True, exist_ok=True)

    rows = []
    for short in shorts:
        patch = (PATCHES / f"{short}.patch").read_text()
        chosen = sel.select_commit(patch)
        sup = set()
        for path, info in S.parse_patch(patch).items():
            sup |= superset(sel, maps, path, info)
        sup |= chosen
        listing = OUT / f"{short}.tests"
        listing.write_text("\n".join(ids[n] for n in sorted(sup) if n in ids))
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{PATCHES}:/patches:ro",
             "-v", f"{OUT}:/out", "-v", f"{listing}:/tests.txt:ro", "--cpus", "3",
             "blast-eval", "python", "/run_one.py", short, "/out", "3"],
            capture_output=True, text=True)
        truth_file = OUT / f"{short}.json"
        if not truth_file.exists():
            print(f"{short}: no result ({r.stderr[-200:]})")
            continue
        truth = json.loads(truth_file.read_text())
        num_of = {i: n for n, i in ids.items()}
        failing = {num_of[i] for i in truth.get("failing", []) if i in num_of}
        missed = failing - chosen
        loads = [sum(ms[n] for n in w) for w in S.pack(sorted(chosen), ms)] or [0]
        cost = max(loads) + S.WORKER_COST_MS * (1 if chosen else 0)
        rows.append((short, len(chosen), len(failing), cost / 1000, missed, len(sup)))
        note = ""
        if missed:
            note = "MISS " + ",".join(ids[n].split("::")[-1] for n in sorted(missed)[:4])
        print(f"{short:10} sel={len(chosen):4} run={len(sup):4} fail={len(failing):3} "
              f"cost={cost / 1000:6.2f} {note}")


if __name__ == "__main__":
    main(sys.argv[1:])
