"""Extract per-test line coverage from the .coverage sqlite DB into a compact map.

Output: /maps/coverage.json.gz
  {"tests": {<test number>: {"<repo-relative file>": [[start, end], ...], ...}}}
Line numbers refer to the pinned sqlglot checkout in /src.
"""

import gzip
import json
import os
import sqlite3
from collections import defaultdict

DB = "/src/.coverage"
OUT = "/maps"
SRC = "/src"
TESTS_JSON = "/mapbuilder/tests.json"


def main() -> None:
    ids = [t["id"] for t in json.load(open(TESTS_JSON))["tests"]]
    number_of = {t["id"]: t["number"] for t in json.load(open(TESTS_JSON))["tests"]}

    con = sqlite3.connect(DB)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    print("coverage schema:")
    for (sql,) in con.execute("SELECT sql FROM sqlite_master"):
        print(" ", sql)
    files = dict(con.execute("SELECT id, path FROM file"))
    contexts = dict(con.execute("SELECT id, context FROM context"))

    # normalize coverage file paths to repo-relative
    rel = {}
    for fid, path in files.items():
        path = os.path.normpath(path)
        if path.startswith(SRC + "/"):
            rel[fid] = path[len(SRC) + 1 :]
        else:
            rel[fid] = None

    per_test = defaultdict(lambda: defaultdict(set))
    unmatched_contexts = set()
    used = set()
    if "line_bits" in tables:
        from coverage.numbits import numbits_to_nums
        line_rows = ((f, c, n) for f, c, blob in
                     con.execute("SELECT file_id, context_id, numbits FROM line_bits")
                     for n in numbits_to_nums(blob))
    elif "line" in tables:
        line_rows = con.execute("SELECT file_id, context_id, num FROM line")
    elif "arc" in tables:
        cols = [r[1] for r in con.execute("PRAGMA table_info(arc)")]
        if "fromno" in cols:
            line_rows = ((f, c, t) for f, c, fr, t in
                         con.execute("SELECT file_id, context_id, fromno, tono FROM arc") if fr == -1)
        else:
            line_rows = con.execute("SELECT file_id, context_id, num FROM arc")
    else:
        raise SystemExit(f"no line/arc table; tables: {sorted(tables)}")
    for fid, ctx_id, num in line_rows:
        rf = rel.get(fid)
        if rf is None:
            continue
        ctx = contexts.get(ctx_id, "")
        ctx = ctx.split("|")[0].removeprefix("./")
        n = number_of.get(ctx)
        if n is None:
            if ctx:
                unmatched_contexts.add(ctx)
            continue
        used.add(n)
        per_test[n][rf].add(num)

    print(f"tests with coverage: {len(used)} / {len(ids)}")
    missing = [i for i in ids if number_of[i] not in used]
    print(f"tests without coverage ({len(missing)}): {missing[:20]}")
    if unmatched_contexts:
        print(f"unmatched contexts ({len(unmatched_contexts)}): {sorted(unmatched_contexts)[:20]}")

    out = {"tests": {}}
    for n, fmap in per_test.items():
        out["tests"][n] = {
            f: ranges(sorted(lines)) for f, lines in sorted(fmap.items())
        }

    os.makedirs(OUT, exist_ok=True)
    with gzip.open(os.path.join(OUT, "coverage.json.gz"), "wt") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("wrote", os.path.join(OUT, "coverage.json.gz"),
          os.path.getsize(os.path.join(OUT, "coverage.json.gz")), "bytes")


def ranges(sorted_lines):
    out = []
    start = prev = sorted_lines[0]
    for n in sorted_lines[1:]:
        if n == prev + 1:
            prev = n
        else:
            out.append([start, prev])
            start = prev = n
    out.append([start, prev])
    return out


if __name__ == "__main__":
    main()