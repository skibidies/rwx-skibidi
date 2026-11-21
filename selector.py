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
import subprocess
import sys
from pathlib import Path

DATA = Path("/data")
APP = Path("/app")
MAPS = APP / "maps"
SRC = Path(os.environ.get("BR_SRC", "/tmp/src"))
MAX_WORKERS = 8
WORKER_COST_MS = 2_000
IMPORTER_CAP = 120  # above this, only readers are selected
KEYWORDS = {"if", "else", "elif", "for", "while", "in", "not", "and", "or",
            "return", "def", "class", "lambda", "yield", "import", "from", "as",
            "with", "try", "except", "finally", "raise", "pass", "assert", "del",
            "global", "nonlocal", "await", "async", "is", "break", "continue"}

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

        # value-flow map: obs reads a name, flows carries it to another name
        obs = json.load(gzip.open(MAPS / "obs.json.gz", "rt"))
        self.obs = obs.get("strong", obs)
        self.obs_str = obs.get("weak", {})
        self.flows = json.load(open(MAPS / "flows.json"))

        # names defined under sqlglot/: lets an added reference be verified
        # without paying for a real import
        self.symbols = set()
        sym = MAPS / "symbols.json.gz"
        if sym.exists():
            self.symbols = set(json.load(gzip.open(sym, "rt")))

        # container key observation: which keys a test actually reads
        self.keys = {}
        if (MAPS / "keys.json.gz").exists():
            self.keys = json.load(gzip.open(MAPS / "keys.json.gz", "rt"))

    def keys_at(self, path, line, keys):
        """Tests that read any of these keys from the container at path:line.
        Returns None when the container was never observed."""
        sink = self.keys.get(f"{path}:{line}")
        if not sink:
            return None
        out: set[int] = set()
        for key in keys:
            out.update(sink.get(key, ()))
        # a test that iterates the container sees every key and value
        out.update(sink.get("*", ()))
        return out

    def is_known(self, path) -> bool:
        return (path in self.scopes or path in self.file_tests or path in self.numbers_by_file)

    numbers_by_id: dict[str, list[int]] = {}


# ---------------------------------------------------------------- patch


def parse_patch(text: str):
    """-> {path: {"hunks": [...], "new_file": bool, "deleted": bool,
                  "old_path": str|None}}  (path is the post-patch path)"""
    if text.endswith("\n"):
        text = text[:-1]
    files: dict[str, dict] = {}
    cur = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            path = m.group(2) if m else line.split()[-1][2:]
            cur = {"hunks": [], "new_file": False, "deleted": False, "old_path": None}
            files[path] = cur
        elif line.startswith("rename from "):
            if cur is not None:
                cur["old_path"] = line[len("rename from "):].strip()
        elif line.startswith("--- "):
            if cur is not None:
                name = line[4:].strip()
                if name == "/dev/null":
                    cur["new_file"] = True
                elif name.startswith("a/"):
                    cur["old_path"] = name[2:]
        elif line.startswith("+++ "):
            if cur is not None and line[4:].strip() == "/dev/null":
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
        old_path = info.get("old_path") or path
        if old_path != path:
            # a rename/move: every test that imported or read the old path
            # can break, and the new path is a fresh module
            selected |= self.tests_reading(old_path)
            selected |= self.tests_importing(old_path)
            selected |= self.fixture_tests(old_path)
        if info.get("deleted"):
            # a deleted module/file fails every test that needs it
            selected |= self.tests_reading(old_path)
            selected |= self.fixture_tests(old_path)
            if old_path.endswith(".py"):
                selected |= self.tests_importing(old_path)
            elif not selected:
                selected |= self.tests_reading_dir(old_path)
            return selected

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

        old_src = SRC / old_path
        old_text = old_src.read_text() if old_src.exists() else None
        old_lines = old_text.split("\n") if old_text is not None else []
        new_lines = apply_hunks(old_lines, info["hunks"]) if old_lines else [
            t for h in info["hunks"] for t in h["newtext"]
        ]
        new_text = "\n".join(new_lines)

        if old_text is not None and not info["new_file"]:
            sa = ast_signature(old_text)
            sb = ast_signature(new_text)
            if sa is not None and sa == sb:
                return selected  # comment/blank-only: behaviour unchanged

        # a file that cannot be imported fails every test that needs it
        if old_text is not None and parse_tree(new_text) is None:
            return selected | self.tests_importing(path)

        spans = self.m.scopes.get(path, [])
        need_import = False
        add_points = []
        old_tree = parse_tree(old_text) if old_text is not None else None
        new_tree = parse_tree(new_text)
        import_unsafe = False

        for hi, h in enumerate(info["hunks"]):
            dels, adds = h["del"], h["add"]
            k = min(len(dels), len(adds))
            pairs = list(zip(dels[:k], adds[:k]))
            if self._is_data_hunk(h, old_tree, new_tree, spans):
                # a class/module-level assignment: the change is data (a dict
                # entry, a set member, a table row), not structure. Only the
                # readers of that name can notice, and only a name/attribute
                # that does not exist can break the import.
                stmt = None
                for ln, _t in adds or dels:
                    stmt = self._class_assign(new_tree if adds else old_tree, ln) or stmt
                texts = [t for _ln, t in (h["add"] + h["del"])]
                keys = self._changed_keys(h, new_tree, old_tree)
                precise = self._key_tests(path, self._container_line(h, old_tree, new_tree), keys)
                if precise is not None:
                    selected |= precise
                    if self._needs_probe(h) and not self._imports_ok(path, new_text):
                        return selected | self.tests_importing(path)
                    selected |= self.tests_reading(path)
                    continue
                if stmt is not None and self._value_line(
                        path, spans, h["old"], stmt, selected, texts):
                    if self._needs_probe(h) and not self._imports_ok(path, new_text):
                        return selected | self.tests_importing(path)
                    continue
                import_unsafe = True
            for (ln, dtext), (nln, atext) in pairs:
                if executable(dtext) and executable(atext) and old_tree is not None and new_tree is not None:
                    got = self._swap_tests(path, ln, nln, old_tree, new_tree)
                    if got is not None:
                        selected |= got
                        continue
                handled_value = False
                if executable(dtext) or executable(atext):
                    if not self._inside_function(spans, ln):
                        s_old = innermost_stmt(old_tree, ln) if old_tree is not None else None
                        s_new = innermost_stmt(new_tree, nln) if new_tree is not None else None
                        if _value_only(s_old, s_new):
                            # a literal value change: only the readers of the
                            # constant can notice it
                            handled_value = self._value_line(
                                path, spans, ln, s_old, selected, [dtext, atext])
                        if not handled_value:
                            import_unsafe = True
                if executable(dtext):
                    if not handled_value:
                        need_import |= self._old_line(path, spans, ln, dtext, selected)
                elif executable(atext):
                    add_points.append(ln)
            for ln, dtext in dels[k:]:
                if executable(dtext):
                    if not self._inside_function(spans, ln):
                        import_unsafe = True
                    need_import |= self._old_line(path, spans, ln, dtext, selected)
            if len(adds) > k:
                add_points.append(dels[-1][0] if dels else h["old"])

        # module/class-level changes can break the import (name errors, bad
        # imports). Probe the patched module: if it no longer imports, every
        # test that needs the file fails, whatever line changed.
        if import_unsafe and old_text is not None:
            if not self._imports_ok(path, new_text):
                return selected | self.tests_importing(path)

        for point in add_points:
            span = innermost_span(spans, point + 1) or innermost_span(spans, point)
            if span is not None and span[0] == "f":
                selected |= self.tests_for_span(path, span)
            else:
                need_import = True

        if need_import:
            selected |= self.tests_importing(path)
        if not selected and not self.m.is_known(path):
            selected |= self.tests_reading_dir(path)
            if not selected and UNCLASSIFIABLE_RE.search(path):
                selected |= set(self.m.all_numbers)
        return selected

    @staticmethod
    def _inside_function(spans, ln) -> bool:
        return any(s[0] == "f" and s[1] <= ln <= s[2] for s in spans)

    def _flow_closure(self, names):
        seen = set(names)
        stack = list(names)
        while stack:
            n = stack.pop()
            for h in self.m.flows.get(n, ()):
                if h not in seen:
                    seen.add(h)
                    stack.append(h)
        return seen

    @staticmethod
    def _container_line(h, old_tree, new_tree):
        """Line of the class/module-level container a data hunk edits: the
        recorder wraps the whole assigned value, so this must be the value's
        own line, not the innermost literal."""
        for tree in (new_tree, old_tree):
            if tree is None:
                continue
            for ln, _t in h["add"] + h["del"]:
                stmt = Selector._class_assign(tree, ln)
                value = getattr(stmt, "value", None) if stmt is not None else None
                if value is not None and hasattr(value, "lineno"):
                    return value.lineno
        return None

    @staticmethod
    def _changed_keys(h, new_tree, old_tree) -> list[str]:
        """Keys (or set members) added/removed by the hunk, normalized the way
        the recorder names them."""
        def names(tree, line):
            out = set()
            if tree is None:
                return out
            for node in ast.walk(tree):
                if isinstance(node, (ast.Dict, ast.Set, ast.List, ast.Tuple)):
                    s = node.lineno
                    e = getattr(node, "end_lineno", s) or s
                    if not (s <= line <= e):
                        continue
                    kids = (node.values if isinstance(node, ast.Dict) else node.elts)
                    if isinstance(node, ast.Dict):
                        kids = [k for k in node.keys if k is not None]
                    for kid in kids:
                        if getattr(kid, "lineno", None) != line:
                            continue
                        if isinstance(kid, ast.Constant):
                            out.add(str(kid.value))
                        elif isinstance(kid, ast.Attribute):
                            out.add(kid.attr)
                        elif isinstance(kid, ast.Name):
                            out.add(kid.id)
                        elif isinstance(kid, ast.Call):
                            fn = kid.func
                            out.add(getattr(fn, "attr", None) or getattr(fn, "id", ""))
            return out

        keys = set()
        for ln, _t in h["add"]:
            keys |= names(new_tree, ln)
        for ln, _t in h["del"]:
            keys |= names(old_tree, ln)
        return sorted(k for k in keys if k)

    def _key_tests(self, path, line, keys):
        """Tests reading the changed keys of a container, or None when the
        container was never observed or the key is unknown."""
        if line is None or not keys:
            return None
        got = self.m.keys_at(path, line, keys)
        if got is None:
            return None
        return got

    @staticmethod
    def _class_assign(tree, line):
        """Innermost assignment statement covering a line, if any."""
        if tree is None:
            return None
        best = None
        for n in ast.walk(tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                s = n.lineno
                e = getattr(n, "end_lineno", s) or s
                if s <= line <= e and (best is None or (e - s) < (best[2] - best[1])):
                    best = (n, s, e)
        return best[0] if best else None

    def _is_data_hunk(self, h, old_tree, new_tree, spans) -> bool:
        """A hunk that only edits class/module-level assignment contents."""
        old_lns = [ln for ln, _t in h["del"]]
        new_lns = [ln for ln, _t in h["add"]]
        if not old_lns and not new_lns:
            return False
        for ln in old_lns:
            if self._inside_function(spans, ln) or self._class_assign(old_tree, ln) is None:
                return False
        for ln in new_lns:
            if self._inside_function(spans, ln) or self._class_assign(new_tree, ln) is None:
                return False
        return True

    def _needs_probe(self, h) -> bool:
        """An added/removed reference to a name that does not exist under
        sqlglot/ can fail the import (NameError, AttributeError). Names we can
        see in the tree, and plain literals, cannot: skip the probe and its
        CPU cost."""
        for _ln, text in h["add"] + h["del"]:
            code = re.sub(r"(['\"]).*?\1", "''", text.split("#", 1)[0])
            for name in re.findall(r"[A-Za-z_]\w*", code):
                if name not in self.m.symbols and name not in KEYWORDS:
                    return True
        return False

    def _value_line(self, path, spans, ln, stmt, selected, changed_texts=()) -> bool:
        """Change of a class/module-level assignment: the tests that notice
        are the ones covering the name's readers (through the value-flow
        map). Returns False when the map cannot carry the case."""
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            return False
        if any(_has_string_literal_text(t) for t in changed_texts):
            return False  # a string is often a key/handle: use the broad rules
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        names: set[str] = set()
        for t in targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
        if not names:
            return False
        out = self.tests_for_lines(path, [ln])
        out |= self.tests_for_span(path, innermost_span(
            [s for s in spans if s[0] == "e"], ln))
        importers = self.tests_importing(path)
        import_time = False
        for name in self._flow_closure(names):
            # a bare string mention is only used when there is no real read
            hits = self.m.obs.get(name) or self.m.obs_str.get(name) or {}
            for f, lines in hits.items():
                if f == path:
                    out |= self.tests_for_lines(f, lines)
                    continue
                if any(not self._inside_function(self.m.scopes.get(f, []), l)
                       for l in lines):
                    # a module/class-level reader runs at import time, where
                    # coverage is not attributed per test: any importer can
                    # notice it
                    import_time = True
                out |= self.tests_for_lines(f, lines)
        if import_time and len(importers) <= IMPORTER_CAP:
            out |= importers
        elif not import_time:
            # a reader can only see this name if it loaded the defining
            # module: the read sites themselves are often shared and hot
            out &= importers
        if len(out) > REF_CAP:
            return False  # too broad to trust the refinement: use the old rules
        selected |= out
        return True

    @staticmethod
    def _has_string_literal_anywhere(stmt) -> bool:
        """True when the assigned value mentions a string literal (or is not a
        plain assignment): then the broad rules are used instead."""
        if isinstance(stmt, ast.Assign):
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            value = stmt.value
        else:
            return True
        return value is None or _has_string_literal(value)

    def _imports_ok(self, path, new_text) -> bool:
        """Real import probe of the patched module (module/class-level change
        that is not a plain literal value swap). Tests fail wholesale when a
        file cannot be imported, so this decides that case exactly."""
        target = SRC / path
        try:
            orig = target.read_bytes()
        except OSError:
            return True  # unknown file: keep the normal rules
        if path.startswith("tests/"):
            return False  # test modules: a broken import fails the whole file
        mod = path[:-3].replace("/", ".")
        if mod.endswith("."):
            mod = path[:-len("/__init__.py")].replace("/", ".")
        if "__main__" in mod:
            return False  # cannot probe safely: take the importers
        code = f"import importlib; importlib.import_module({mod!r})"
        try:
            target.write_text(new_text)
            r = subprocess.run(
                [sys.executable, "-c", code], cwd=str(SRC),
                capture_output=True, timeout=15,
                env={**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return r.returncode == 0
        except Exception:
            return False  # cannot prove it imports: take the importers
        finally:
            try:
                target.write_bytes(orig)
            except OSError:
                pass

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


class _NormConst(ast.NodeTransformer):
    """Replace literal values with a marker: dumps compare equal when only
    literal values changed. Dict keys are left alone (a key is a lookup
    handle, so changing it changes behaviour), and strings are treated as
    significant too."""

    def visit_Dict(self, node):
        node.values = [self.visit(v) for v in node.values]
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return node
        return ast.copy_location(ast.Constant(value="<const>"), node)


def _value_only(stmt_old, stmt_new):
    if stmt_old is None or stmt_new is None:
        return False
    try:
        return ast.dump(_NormConst().visit(stmt_old), include_attributes=False) == \
               ast.dump(_NormConst().visit(stmt_new), include_attributes=False)
    except Exception:
        return False


def _has_string_literal_text(text: str) -> bool:
    """True when a source line contains a string literal (quotes outside a
    comment)."""
    return bool(re.search(r"['\"]", text.split("#", 1)[0]))


def _has_string_literal(node) -> bool:
    """Any non-key string literal in the value expression (dict keys are
    lookup handles, handled by _NormConst keeping them significant)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Dict):
            stack.extend(cur.values)
            continue
        if isinstance(cur, ast.Constant) and isinstance(cur.value, str):
            return True
        stack.extend(ast.iter_child_nodes(cur))
    return False


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