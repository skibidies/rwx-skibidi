"""Map-build instrumentation. Injected as /src/tests/conftest.py at map build.

Records, for the pinned suite run:
  /maps/reads.json   test -> files it reads as text
  /maps/equiv.json   per comparison line: which tests ever saw the alternative
                     operators disagree with the one in the source (so an
                     operator swap is provably neutral for the other tests)

Attribution: coverage contexts and reads are credited to the test that
started the work, including thread pools and the spawned-process pools
(routed through traced threads that pickle calls/results like a real
process boundary, since Expr.__reduce__ runs serde).
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import io
import json
import os
import pickle
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

CURRENT = [""]
_TL = threading.local()
READS = {}
PREFIX = "/src/"

TRANSFORMED = set()
WRAPPED = {}   # file -> set of lines with a wrapped comparison
SKIPPED = {}   # file -> set of lines with a comparison we did not wrap
RECORD = {}    # (file, line, alt_op) -> [bad ctxs, unsure ctxs]
BUDGET = {}    # (file, line, alt_op) -> evaluations left
BUDGET_PER_KEY = 200_000


def current() -> str:
    return getattr(_TL, "id", "") or CURRENT[0]


# ---- file-read recording ------------------------------------------------

_orig_open = builtins.open
_orig_io_open = io.open


def _record(file):
    try:
        p = os.fspath(file)
        if isinstance(p, bytes):
            p = p.decode()
        if isinstance(p, str) and p.startswith(PREFIX):
            READS.setdefault(current(), set()).add(p[len(PREFIX):])
    except Exception:
        pass


def _open(file, *args, **kwargs):
    _record(file)
    return _orig_open(file, *args, **kwargs)


def _io_open(file, *args, **kwargs):
    _record(file)
    return _orig_io_open(file, *args, **kwargs)


builtins.open = _open
io.open = _io_open


# ---- comparison dual evaluation ----------------------------------------

FAMILY = {
    "is": ("is not", "==", "!="),
    "is not": ("is", "==", "!="),
    "==": ("is", "is not", "!="),
    "!=": ("is", "is not", "=="),
    "<": ("<=", ">", ">="),
    "<=": ("<", ">", ">="),
    ">": ("<", "<=", ">="),
    ">=": ("<", "<=", ">"),
    "in": ("not in",),
    "not in": ("in",),
}
ALTS = {
    "is": lambda a, b: a is b,
    "is not": lambda a, b: a is not b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
    "not in": lambda a, b: a not in b,
}


# ---- container key observation -----------------------------------------

KEYS = {}      # (relpath, line) -> {key: set(contexts)}
KEY_BUDGET = {}  # relpath -> recorded events (guards a runaway hot line)
KEY_BUDGET_MAX = 400_000


def _key_name(obj):
    """Normalize a looked-up key to the form the selector derives from the
    source: strings/bools/numeric verbatim, classes and enums by name."""
    if isinstance(obj, bool):
        return repr(obj)
    if isinstance(obj, (str, int, float)):
        return str(obj)
    name = getattr(obj, "__name__", None)
    if isinstance(name, str):
        return name
    try:
        return str(obj)
    except Exception:
        return "*"


def _record_key(key, relpath, line, star=False):
    sink = KEYS.setdefault((relpath, line), {})
    k = "*" if star else _key_name(key)
    sink.setdefault(k or "*", set()).add(current())


class _Rec:
    """Records which keys a test reads from one constant container."""

    def __init__(self, relpath, line):
        self._relpath = relpath
        self._line = line
        self._watchers = []

    def _note(self, key=None, star=False):
        _record_key(key, self._relpath, self._line, star)
        for relpath, line in self._watchers:  # a merged-in constant is read too
            _record_key(key, relpath, line, star)


class _RecDict(dict, _Rec):
    def __init__(self, *a, relpath, line, **k):
        dict.__init__(self, *a, **k)
        _Rec.__init__(self, relpath, line)

    def get(self, key, default=None):
        self._note(key)
        return dict.get(self, key, default)

    def __getitem__(self, key):
        self._note(key)
        return dict.__getitem__(self, key)

    def __contains__(self, key):
        self._note(key)
        return dict.__contains__(self, key)

    def pop(self, *a):
        self._note(a[0] if a else None, star=not a)
        return dict.pop(self, *a)

    def setdefault(self, key, default=None):
        self._note(key)
        return dict.setdefault(self, key, default)

    def keys(self):
        self._note(star=True)
        return dict.keys(self)

    def values(self):
        self._note(star=True)
        return dict.values(self)

    def items(self):
        self._note(star=True)
        return dict.items(self)

    def __iter__(self):
        self._note(star=True)
        return dict.__iter__(self)

    def __reduce__(self):
        return (_rebuild, (dict, dict(self), self._relpath, self._line))


class _RecSet(set, _Rec):
    def __init__(self, *a, relpath, line, **k):
        set.__init__(self, *a, **k)
        _Rec.__init__(self, relpath, line)

    def __contains__(self, key):
        self._note(key)
        return set.__contains__(self, key)

    def __iter__(self):
        self._note(star=True)
        return set.__iter__(self)

    def pop(self):
        self._note(star=True)
        return set.pop(self)

    def __reduce__(self):
        return (_rebuild, (set, set(self), self._relpath, self._line))


class _RecList(list, _Rec):
    def __init__(self, *a, relpath, line, **k):
        list.__init__(self, *a, **k)
        _Rec.__init__(self, relpath, line)

    def __getitem__(self, key):
        self._note(key, star=isinstance(key, slice))
        return list.__getitem__(self, key)

    def __iter__(self):
        self._note(star=True)
        return list.__iter__(self)

    def __reduce__(self):
        return (_rebuild, (list, list(self), self._relpath, self._line))


_TYPES = {dict: _RecDict, set: _RecSet, list: _RecList}


def _rebuild(base, payload, relpath, line):
    if base is dict:
        return _RecDict(payload, relpath=relpath, line=line)
    if base is set:
        return _RecSet(payload, relpath=relpath, line=line)
    return _RecList(payload, relpath=relpath, line=line)


def _br_data(value, *sources, relpath, line):
    """Wrap a class/module-level container so reads of its keys are recorded,
    and make it report the reads of anything that merges it (`{**CONST}`)."""
    if not isinstance(value, (dict, set, list)):
        return value
    rec = _TYPES.get(type(value))
    if rec is None:
        return value
    wrapped = rec(value, relpath=relpath, line=line)
    # a later copy of this value must report back here: the constant is read
    # through whatever it was merged into
    for src in sources:
        if isinstance(src, _Rec):
            src._watchers.append((relpath, line))
    return wrapped


def _br_cmp(operands, op, fpath, lo, hi):
    """Original comparison with the source operator, then dual-evaluate the
    alternatives and record any disagreement per test context."""
    a, b = operands
    out = ALTS[op](a, b)
    ctx = current()
    for alt in FAMILY[op]:
        for line in range(lo, hi + 1):
            key = (fpath, line, alt)
            budget = BUDGET.get(key)
            if budget is None:
                budget = BUDGET_PER_KEY
            if budget <= 0:
                RECORD.setdefault(key, [set(), set()])[1].add(ctx)
                continue
            BUDGET[key] = budget - 1
            try:
                other = ALTS[alt](a, b)
                ok = type(other) is bool
            except Exception:
                ok = False
            if not ok:
                RECORD.setdefault(key, [set(), set()])[1].add(ctx)
            elif bool(other) != bool(out):
                RECORD.setdefault(key, [set(), set()])[0].add(ctx)
    return out


OPNAME = {
    ast.Is: "is", ast.IsNot: "is not", ast.Eq: "==", ast.NotEq: "!=",
    ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
    ast.In: "in", ast.NotIn: "not in",
}


class _CmpWrap(ast.NodeTransformer):
    def __init__(self, relpath):
        self.relpath = relpath
        self.wrapped = set()
        self.skipped = set()
        self.depth = 0  # >0 inside a function: not a class/module-level literal
        self._in_data = 0  # >0 inside a class/module-level container literal

    def visit_Compare(self, node):
        self.generic_visit(node)
        lo = node.lineno
        hi = getattr(node, "end_lineno", lo) or lo
        if len(node.ops) != 1 or not all(type(o) in OPNAME for o in node.ops):
            self.skipped.update(range(lo, hi + 1))
            return node
        self.wrapped.update(range(lo, hi + 1))
        call = ast.copy_location(
            ast.Call(
                func=ast.Name("_br_cmp", ast.Load()),
                args=[
                    ast.Tuple([node.left, node.comparators[0]], ast.Load()),
                    ast.Constant(OPNAME[type(node.ops[0])]),
                    ast.Constant(self.relpath),
                    ast.Constant(lo),
                    ast.Constant(hi),
                ],
                keywords=[],
            ),
            node,
        )
        return call

    # -- class/module-level containers: record which keys tests read -----

    def visit_FunctionDef(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def _make_call(self, node):
        """Wrap one container literal so reads of its keys are recorded."""
        lo = node.lineno
        sources = []
        if isinstance(node, ast.Dict):
            # `**x` produces a None key with the operand in values: reads of
            # the merged-in constant must be credited to that constant too
            sources = [v for k, v in zip(node.keys, node.values) if k is None]
        call = ast.copy_location(
            ast.Call(
                func=ast.Name("_br_data", ast.Load()),
                args=[node] + sources,
                keywords=[
                    ast.keyword("relpath", ast.Constant(self.relpath)),
                    ast.keyword("line", ast.Constant(lo)),
                ],
            ),
            node,
        )
        return call

    def _wrap_data(self, node, line=None):
        """At class/module level a container literal is data: wrap it, and
        every container nested inside it, at its own line."""
        if not isinstance(node, (ast.Dict, ast.Set, ast.List)):
            return node
        self._in_data += 1
        node = self.generic_visit(node)
        self._in_data -= 1
        return self._make_call(node)

    def visit_Dict(self, node):
        return self._wrap_data(node) if self._in_data else self.generic_visit(node)

    visit_Set = visit_Dict
    visit_List = visit_Dict

    def visit_Assign(self, node):
        if self.depth == 0 and self._in_data == 0 and node.value is not None:
            node.value = self._wrap_data(node.value, node.value.lineno)
        else:
            self.generic_visit(node)
        return node

    def visit_AnnAssign(self, node):
        if self.depth == 0 and self._in_data == 0 and node.value is not None:
            node.value = self._wrap_data(node.value, node.lineno)
        else:
            self.generic_visit(node)
        return node


class _TransformLoader:
    def __init__(self, loader, relpath):
        self._loader = loader
        self._relpath = relpath

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        try:
            src = self._loader.get_source(module.__name__)
            tree = ast.parse(src, filename=self._relpath)
            tr = _CmpWrap(self._relpath)
            tree = tr.visit(tree)
            ast.fix_missing_locations(tree)
            code = compile(tree, self._relpath, "exec", dont_inherit=True)
        except Exception:
            SKIPPED.setdefault(self._relpath, set()).update(range(1, 10_000))
            TRANSFORMED.add(self._relpath)
            return self._loader.exec_module(module)
        module.__dict__["_br_cmp"] = _br_cmp
        module.__dict__["_br_data"] = _br_data
        WRAPPED.setdefault(self._relpath, set()).update(tr.wrapped)
        SKIPPED.setdefault(self._relpath, set()).update(tr.skipped)
        TRANSFORMED.add(self._relpath)
        exec(code, module.__dict__)


class _TransformFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None:
            return spec
        relpath = os.path.relpath(spec.origin, SRC_ROOT)
        spec.loader = _TransformLoader(spec.loader, relpath)
        return spec


SRC_ROOT = "/src"


def _install_finder():
    sys.meta_path.insert(0, _TransformFinder())


_install_finder()


# ---- thread context propagation ----------------------------------------

def _wrap(fn, ctx):
    def inner(*args, **kwargs):
        _TL.id = ctx
        try:
            import coverage
            cov = coverage.Coverage.current()
            if cov is not None:
                # per-thread context name: the sqlite writer loses concurrent
                # writes to the same (file, context) bitmap
                cov.switch_context(f"{ctx}|t{threading.get_ident()}")
        except Exception:
            pass
        return fn(*args, **kwargs)
    return inner


_orig_submit = ThreadPoolExecutor.submit


def _submit(self, fn, /, *args, **kwargs):
    return _orig_submit(self, _wrap(fn, current()), *args, **kwargs)


ThreadPoolExecutor.submit = _submit

# sqlglot's tests run fixture work in spawned processes, which coverage does
# not trace at all. Route that work through traced threads so it is recorded,
# honouring each pool's initializer (some set module globals per worker).
from concurrent.futures import ProcessPoolExecutor  # noqa: E402

_orig_pinit = ProcessPoolExecutor.__init__


def _pinit(self, *args, initializer=None, initargs=(), **kwargs):
    _orig_pinit(self, *args, initializer=initializer, initargs=initargs, **kwargs)
    self._mb_init = (initializer, initargs)
    self._mb_pool = ThreadPoolExecutor(max_workers=4)


def _psubmit(self, fn, /, *args, **kwargs):
    initializer, initargs = getattr(self, "_mb_init", (None, ()))

    def run(*a, **k):
        if initializer is not None:
            initializer(*initargs)
        # a real process pool pickles the call and the result (Expr.__reduce__
        # runs serde), so do the same here to record that work
        fn2, a2, k2 = pickle.loads(pickle.dumps((fn, a, k)))
        result = fn2(*a2, **k2)
        return pickle.loads(pickle.dumps(result))

    return self._mb_pool.submit(run, *args, **kwargs)


ProcessPoolExecutor.__init__ = _pinit
ProcessPoolExecutor.submit = _psubmit


# ---- pytest hooks ------------------------------------------------------

def pytest_runtest_setup(item):
    CURRENT[0] = item.nodeid
    _TL.id = ""


def pytest_runtest_teardown(item):
    CURRENT[0] = ""
    _TL.id = ""


def pytest_sessionfinish(session, exitstatus):
    out = {k: sorted(v) for k, v in READS.items() if k}
    os.makedirs("/maps", exist_ok=True)
    with _orig_open("/maps/reads.json", "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"reads.json: {len(out)} tests with file reads")

    equiv = {
        "_transformed": sorted(TRANSFORMED),
        "_wrapped": {f: sorted(s) for f, s in WRAPPED.items()},
        "_skipped": {f: sorted(s) for f, s in SKIPPED.items()},
        "lines": {},
    }
    for (f, line, alt), (bad, unsure) in RECORD.items():
        equiv["lines"].setdefault(f, {}).setdefault(str(line), {})[alt] = {
            "bad": sorted(bad),
            "unsure": sorted(unsure),
        }
    with _orig_open("/maps/equiv.json", "w") as fh:
        json.dump(equiv, fh, separators=(",", ":"))
    print(f"equiv.json: {len(equiv['lines'])} files with comparison records")

    keys = {f"{f}:{l}": {k: sorted(v) for k, v in m.items()}
            for (f, l), m in KEYS.items()}
    with _orig_open("/maps/keys.json", "w") as fh:
        json.dump(keys, fh, separators=(",", ":"))
    print(f"keys.json: {len(keys)} containers observed")