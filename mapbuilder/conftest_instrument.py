"""Map-build instrumentation: attribute coverage contexts and file reads to
the test that started the work, including work on thread pools.

Injected as /src/tests/conftest.py during the map build only. Writes
/maps/reads.json: {"<nodeid>": ["sqlglot/x.py", ...]} for reads under /src.
"""

import builtins
import io
import json
import os
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor

CURRENT = [""]
_TL = threading.local()
READS = {}
PREFIX = "/src/"


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

_orig_run = threading.Thread.run


def _run(self):
    _TL.id = current()
    try:
        import coverage
        cov = coverage.Coverage.current()
        if cov is not None and _TL.id:
            cov.switch_context(f"{_TL.id}|t{threading.get_ident()}")
    except Exception:
        pass
    return _orig_run(self)


threading.Thread.run = _run


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