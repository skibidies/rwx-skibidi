"""Blast Radius selector: print the tests to run for every commit.

One JSON line per commit to stdout, logs to stderr. See SCORING.md.

For each patch we select tests from maps built from the pinned commit:
  coverage   test -> lines it executed
  scopes     function ('f', with body start), class ('c') and multi-line
             expression ('e') spans
  imports    test file -> modules it imports (transitively)
  fixtures   fixture file -> tests that load it
  reads      test -> files it reads as text
  refs       constant name -> lines that mention it
A failing test must execute a changed line, or fail because a module it needs
cannot import, or read changed data. We cover each case, then pack the
selection across 1..8 workers and keep the cheapest split.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path("/data")
APP = Path("/app")
MAPS = APP / "maps"
SRC = Path(os.environ.get("BR_SRC", "/tmp/src"))
MAX_WORKERS = 8
WORKER_COST_MS = 2_000
REF_CAP = 600  # fall back to importers when a constant's readers are everywhere
UNCLASSIFIABLE_RE = re.compile(
    r"(^|/)(setup\.py|pyproject\.toml|MANIFEST\.in|Makefile|requirements\.lock)$"
    r"|\.(py|pyi|rs|c|h|cpp|toml|cfg|in|lock)$"
)


def unpack_source() -> None:
    """Unpack the pinned sqlglot snapshot (built from the pinned commit)."""
    import tarfile

    if SRC.exists():
        return
    SRC.mkdir(parents=True, exist_ok=True)
    with tarfile.open(MAPS / "src.tar.gz") as tf:
        tf.extractall(SRC, filter="data")


# ---------------------------------------------------------------- maps

class Maps:
    def __init__(self):
        with gzip.open(MAPS / "coverage.json.gz", "rt") as fh:
            self.cov = {int(k): v for k, v in json.load(fh)["tests"].items()}
        self.all_numbers = set(self.cov)
        imports = json.load(open(MAPS / "imports.json"))
        self.fixtures = {k: set(v) for k, v in json.load(open(MAPS / "fixtures.json")).items()}
        self.scopes = json.load(open(MAPS / "scopes.json"))
        self.refs = json.load(open(MAPS / "refs.json"))

        self.file_tests: dict[str, list] = {}
        for num, fmap in self.cov.items():
            for f, ranges in fmap.items():
                self.file_tests.setdefault(f, []).append((num, ranges))

        file_nums: dict[str, list[int]] = {}
        for nodeid, nums in self.numbers_by_id.items():
            file_nums.setdefault(nodeid.split("::")[0], []).extend(nums)

        # a module that cannot import fails every test that touches it:
        # static importers (collection time) and lazy importers (run time,
        # visible as coverage of the module)
        self.import_tests: dict[str, set[int]] = {}
        for tf, mods in imports.items():
            for m in mods:
                self.import_tests.setdefault(m, set()).update(file_nums.get(tf, ()))

        self.reader_tests: dict[str, set[int]] = {}
        reads = json.load(open(MAPS / "reads.json"))
        for nodeid, files in reads.items():
            nums = self.numbers_by_id.get(nodeid, file_nums.get(nodeid.split("::")[0], []))
            for f in files:
                self.reader_tests.setdefault(f, set()).update(nums)
        self.numbers_by_file = file_nums

        # comparison dual-evaluation behaviour map
        equiv = json.load(gzip.open(MAPS / "equiv.json.gz", "rt"))
        self.equiv_lines = equiv.get("lines", {})
        self.equiv_transformed = set(equiv.get("_transformed", []))
        self.equiv_wrapped = {f: set(s) for f, s in equiv.get("_wrapped", {}).items()}
        self.equiv_skipped = {f: set(s) for f, s in equiv.get("_skipped", {}).items()}

    def is_known(self, path) -> bool:
        return (path in self.scopes or path in self.file_tests or path in self.numbers_by_file)

    numbers_by_id: dict[str, list[int]] = {}


# ---------------------------------------------------------------- patch


def parse_patch(text: str):
    """-> {path: {"hunks": [...], "new_file": bool, "deleted": bool}}"""
    if text.endswith("\n"):
        text = text[:-1]
    files: dict[str, dict] = {}
    cur = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            path = m.group(2) if m else line.split()[-1][2:]
            cur = {"hunks": [], "new_file": False, "deleted": False}
            files[path] = cur
        elif line.startswith("--- "):
            if cur is not None and "/dev/null" in line:
                cur["new_file"] = True
        elif line.startswith("+++ "):
            if cur is not None and "/dev/null" in line:
                cur["deleted"] = True
        elif line.startswith("@@") and cur is not None:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                continue
            h = {
                "old": int(m.group(1)),
                "old_n": int(m.group(2) or 1),
                "new": int(m.group(3)),
                "del": [],   # (old lineno, text)
                "add": [],   # (new lineno, text)
                "newtext": [],  # new-side text of the hunk
            }
            cur["hunks"].append(h)
            cur["_ol"] = h["old"]
            cur["_nl"] = h["new"]
        elif cur is not None and cur["hunks"]:
            h = cur["hunks"][-1]
            if line.startswith("+"):
                h["add"].append((cur["_nl"], line[1:]))
                h["newtext"].append(line[1:])
                cur["_nl"] += 1
            elif line.startswith("-"):
                h["del"].append((cur["_ol"], line[1:]))
                cur["_ol"] += 1
            elif line.startswith("\\"):
                pass
            else:  # context (a dropped trailing space is context too)
                h["newtext"].append(line[1:] if line.startswith(" ") else line)
                cur["_ol"] += 1
                cur["_nl"] += 1
    return files


def apply_hunks(old_lines: list[str], hunks: list[dict]) -> list[str]:
    out: list[str] = []
    pos = 1
    for h in hunks:
        while pos < h["old"] and pos <= len(old_lines):
            out.append(old_lines[pos - 1])
            pos += 1
        out.extend(h["newtext"])
        pos += h["old_n"]
    while pos <= len(old_lines):
        out.append(old_lines[pos - 1])
        pos += 1
    return out


# ---------------------------------------------------------------- selection


def executable(text: str) -> bool:
    s = text.strip()
    return bool(s) and not s.startswith("#")


def tokens_of(text: str) -> set[str]:
    out = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", text))
    out |= set(re.findall(r"""(['"])([A-Za-z_][A-Za-z_0-9 ]{2,})\1""", text) and
               [m[1] for m in re.findall(r"""(['"])([A-Za-z_][A-Za-z_0-9 ]{2,})\1""", text)])
    return out


def innermost_span(spans, line):
    best = None
    for span in spans:
        s, e = span[1], span[2]
        if s <= line <= e and (best is None or (e - s) < (best[2] - best[1])):
            best = span
    return best


class Selector:
    def __init__(self, maps: Maps):
        self.m = maps

    # -- primitive lookups -------------------------------------------------

    def tests_for_lines(self, path, lines):
        out = set()
        for num, ranges in self.m.file_tests.get(path, ()):
            for ln in lines:
                if any(s <= ln <= e for s, e in ranges):
                    out.add(num)
                    break
        return out

    def tests_for_span(self, path, span):
        out = set()
        if span is None:
            return out
        lo, hi = span[1], span[2]
        for num, ranges in self.m.file_tests.get(path, ()):
            if any(s <= hi and e >= lo for s, e in ranges):
                out.add(num)
        return out

    def tests_importing(self, path):
        out = set(self.m.import_tests.get(path, ()))
        out |= self.tests_covering_file(path)  # lazy, run-time imports
        out |= set(self.m.numbers_by_file.get(path, ()))  # tests in the file itself
        return out

    def tests_covering_file(self, path):
        return {num for num, _ in self.m.file_tests.get(path, ())}

    def tests_reading(self, path):
        return set(self.m.reader_tests.get(path, ()))

    def tests_reading_dir(self, path):
        prefix = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        out = set()
        for f, nums in self.m.reader_tests.items():
            if f.startswith(prefix):
                out |= nums
        return out

    def tests_referencing(self, name):
        """Tests covering any line that mentions a constant's name."""
        out = set()
        for f, lines in self.m.refs.get(name, {}).items():
            out |= self.tests_for_lines(f, lines)
        return out

    def fixture_tests(self, path):
        out = set(self.m.fixtures.get(path, ()))
        if not out:
            prefix = path.rsplit("/", 1)[0] + "/"
            for fx, nums in self.m.fixtures.items():
                if fx.startswith(prefix):
                    out |= nums
        return out

    # -- per commit -------------------------------------------------------

    def select_commit(self, patch_text):
        selected: set[int] = set()
        for path, info in parse_patch(patch_text).items():
            selected |= self.select_file(path, info)
        return selected

    def select_file(self, path, info):
        selected: set[int] = set()
        selected |= self.tests_reading(path)  # text readers see any edit

        if path.startswith("tests/fixtures/") or "/fixtures/" in path:
            selected |= self.fixture_tests(path)
            return selected

        if not path.endswith(".py"):
            if "/fixtures/" in path or path.startswith("tests/fixtures/"):
                return selected
            selected |= self.tests_importing(re.sub(r"\.[^.]+$", ".py", path))
            if not selected:
                selected |= self.tests_reading_dir(path)
            return selected

        old_src = SRC / path
        old_text = old_src.read_text() if old_src.exists() else None
        old_lines = old_text.split("\n") if old_text is not None else []
        new_lines = apply_hunks(old_lines, info["hunks"]) if old_lines else [
            t for h in info["hunks"] for t in h["newtext"]
        ]

        if old_text is not None and not info["new_file"]:
            sa = ast_signature(old_text)
            sb = ast_signature("\n".join(new_lines))
            if sa is not None and sa == sb:
                return selected  # comment/blank-only: behaviour unchanged

        spans = self.m.scopes.get(path, [])
        need_import = False
        add_points = []
        old_tree = parse_tree(old_text) if old_text is not None else None
        new_tree = parse_tree("\n".join(new_lines))

        for h in info["hunks"]:
            dels, adds = h["del"], h["add"]
            k = min(len(dels), len(adds))
            pairs = list(zip(dels[:k], adds[:k]))
            for (ln, dtext), (nln, atext) in pairs:
                if executable(dtext) and executable(atext) and old_tree is not None and new_tree is not None:
                    got = self._swap_tests(path, ln, nln, old_tree, new_tree)
                    if got is not None:
                        selected |= got
                        continue
                if executable(dtext):
                    need_import |= self._old_line(path, spans, ln, dtext, selected)
                elif executable(atext):
                    add_points.append(ln)
            for ln, dtext in dels[k:]:
                if executable(dtext):
                    need_import |= self._old_line(path, spans, ln, dtext, selected)
            if len(adds) > k:
                add_points.append(dels[-1][0] if dels else h["old"])

        for point in add_points:
            span = innermost_span(spans, point + 1) or innermost_span(spans, point)
            if span is not None and span[0] == "f":
                selected |= self.tests_for_span(path, span)
            else:
                need_import = True

        if need_import:
            selected |= self.tests_importing(path)
        if not selected and not self.m.is_known(path):
            # no map data for this file (e.g. sqlglotc sources, build config):
            # take the tests that read around it, and if we still have no
            # signal at all, run everything rather than risk a 300 s miss
            selected |= self.tests_reading_dir(path)
            if not selected and UNCLASSIFIABLE_RE.search(path):
                selected |= set(self.m.all_numbers)
        return selected

    def _swap_tests(self, path, ln, nln, old_tree, new_tree):
        """If the changed line pair is a pure comparison-operator swap, the
        behaviour map says which tests ever saw the two operators disagree:
        only those can notice. Returns None when unproven (fall back)."""
        stmt_old = innermost_stmt(old_tree, ln)
        stmt_new = innermost_stmt(new_tree, nln)
        new_op = detect_swap(stmt_old, stmt_new)
        if new_op is None:
            return None
        if path not in self.m.equiv_transformed:
            return None  # source was not instrumented: no proof
        wrapped = self.m.equiv_wrapped.get(path, set())
        skipped = self.m.equiv_skipped.get(path, set())
        lines = set()
        for n in ast.walk(stmt_old):
            if isinstance(n, ast.Compare) and n.lineno <= ln <= (getattr(n, "end_lineno", n.lineno) or n.lineno):
                lines.update(range(n.lineno, (getattr(n, "end_lineno", n.lineno) or n.lineno) + 1))
        if not lines:
            return None
        file_map = self.m.equiv_lines.get(path, {})
        out: set[int] = set()
        for l in lines:
            if l in skipped or l not in wrapped:
                return None  # comparison not instrumented at this line
            ent = file_map.get(str(l), {}).get(new_op)
            if ent is None:
                continue  # never executed: nothing can notice
            bad, unsure = ent
            if -1 in bad or -1 in unsure:
                return None  # import-time effects: keep the safe selection
            out |= set(bad) | set(unsure)
        return out

    def _old_line(self, path, spans, ln, text, selected) -> bool:
        """Classify one changed old line. Returns True if import-relevant."""
        span = innermost_span(spans, ln)

        if span is None or span[0] == "c":
            # module or class level: runs at import, or inside a lambda/
            # comprehension whose span we did not narrow
            got = self.tests_for_lines(path, [ln])
            got |= self.tests_for_span(path, innermost_span(
                [s for s in spans if s[0] == "e"], ln))
            selected |= got
            # constants: the tests that notice are the ones covering readers
            refs = set()
            for tok in tokens_of(text):
                refs |= self.tests_referencing(tok)
                if len(refs) > REF_CAP:
                    return True
            selected |= refs
            return False

        if ln == span[1] or ln < span[3]:
            # def / decorator / signature line: import-relevant structure
            selected |= self.tests_for_span(path, span)
            return True

        got = self.tests_for_lines(path, [ln])
        if got:
            selected |= got
        else:
            selected |= self.tests_for_span(path, span)  # handlers, else, ...
        # multi-line expressions: short-circuit changes move execution between
        # lines of the same expression, so take the whole expression
        selected |= self.tests_for_span(path, innermost_span(
            [s for s in spans if s[0] == "e"], ln))
        return False


def ast_signature(src: str):
    try:
        return ast.dump(ast.parse(src), include_attributes=False)
    except (SyntaxError, ValueError, TypeError):
        return None


def parse_tree(src: str):
    try:
        return ast.parse(src)
    except (SyntaxError, ValueError, TypeError):
        return None


# ---- comparison-operator swap detection --------------------------------

OP_UNIVERSE = {
    "Eq": "==", "NotEq": "!=", "Is": "is", "IsNot": "is not",
    "Lt": "<", "LtE": "<=", "Gt": ">", "GtE": ">=",
    "In": "in", "NotIn": "not in",
}


class _NormOps(ast.NodeTransformer):
    """Replace operator nodes with their category so dumps compare equal
    when only the operator changed."""


def _mk_visit(category):
    def visit(self, node):
        return ast.copy_location(ast.Constant(value=category), node)
    return visit


for _cat, _names in {
    "cmpop": ("Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In", "NotIn"),
    "boolop": ("And", "Or"),
    "unaryop": ("Not", "UAdd", "USub", "Invert"),
    "operator": ("Add", "Sub", "Mult", "MatMult", "Div", "Mod", "Pow",
                  "LShift", "RShift", "BitOr", "BitXor", "BitAnd", "FloorDiv"),
}.items():
    for _name in _names:
        setattr(_NormOps, f"visit_{_name}", _mk_visit(_cat))


def _op_names(node):
    return [type(n).__name__ for n in ast.walk(node)
            if isinstance(n, (ast.cmpop, ast.boolop, ast.unaryop, ast.operator))]


def detect_swap(stmt_old, stmt_new):
    """If the two statements differ only in one operator, return the new
    operator's comparison name (e.g. '=='), else None."""
    if stmt_old is None or stmt_new is None:
        return None
    o_ops, n_ops = _op_names(stmt_old), _op_names(stmt_new)
    if len(o_ops) != len(n_ops):
        return None
    pairs = [(o, n) for o, n in zip(o_ops, n_ops) if o != n]
    if len(pairs) != 1:
        return None
    o_op, n_op = pairs[0]
    if o_op not in OP_UNIVERSE or n_op not in OP_UNIVERSE:
        return None
    try:
        if ast.dump(_NormOps().visit(stmt_old), include_attributes=False) != \
           ast.dump(_NormOps().visit(stmt_new), include_attributes=False):
            return None
    except Exception:
        return None
    return OP_UNIVERSE[n_op]


def innermost_stmt(tree, line):
    best = None
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt):
            s = n.lineno
            e = getattr(n, "end_lineno", s) or s
            if s <= line <= e and (best is None or (e - s) < (best[2] - best[1])):
                best = (n, s, e)
    return best[0] if best else None


# ---------------------------------------------------------------- packing


def pack(selected: list[int], ms: dict[int, int]):
    if not selected:
        return []
    best = None
    for w in range(1, MAX_WORKERS + 1):
        workers = [[] for _ in range(w)]
        loads = [0] * w
        for num in sorted(selected, key=lambda n: -ms[n]):
            i = loads.index(min(loads))
            workers[i].append(num)
            loads[i] += ms[num]
        cost = max(loads) + WORKER_COST_MS * w
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1]


# ---------------------------------------------------------------- main


def main() -> None:
    unpack_source()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    for t in tests:
        Maps.numbers_by_id.setdefault(t["id"], []).append(t["number"])

    maps = Maps()
    sel = Selector(maps)

    patches = sorted((DATA / "commits").glob("*.patch"))
    total = 0
    for patch in patches:
        selected = sorted(sel.select_commit(patch.read_text()))
        workers = pack(selected, ms)
        cost = 0
        if workers:
            loads = [sum(ms[n] for n in w) for w in workers]
            cost = max(loads) + WORKER_COST_MS * len(workers)
        total += cost
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"{len(patches)} commits, {total / 1000:.1f} simulated s", file=sys.stderr)


if __name__ == "__main__":
    main()