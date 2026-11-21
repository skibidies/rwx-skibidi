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