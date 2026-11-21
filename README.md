# Blast Radius — RWX Buildathon entry

Test selection for sqlglot changes: pick the smallest set of tests that can
catch each patch, split across up to 8 workers. Built on the starter kit
(`score.py`, `Makefile`, `sample/`, `SCORING.md` are from the organizers'
`blast-radius-sample.tar.gz`).

## How it works

`selector.py` reads `/data/commits/*.patch` and, for every commit, selects
tests from maps built from the pinned sqlglot commit (`eb11aec`) — maps the
rules allow (coverage, imports, fixtures):

| map | built from | use |
| --- | --- | --- |
| `maps/coverage.json.gz` | one instrumented suite run with per-test line coverage | a failing test executes changed lines |
| `maps/scopes.json` | AST of the pinned commit | function/class/multi-line-expression spans |
| `maps/imports.json` | AST import graph | a module that cannot import fails every test that needs it |
| `maps/fixtures.json` | suite run + static scan | fixture files are read by specific tests |
| `maps/reads.json` | instrumented suite run | source-text readers see any edit |
| `maps/refs.json` | AST of the pinned commit | changed constants are noticed by their readers |
| `maps/src.tar.gz` | the pinned commit itself | patch application and AST comparison at run time |

Selection rules, per changed line: tests covering the line (or its innermost
multi-line expression, since short-circuit changes move execution inside one
expression); enclosing-scope tests for added lines; import-failure semantics
for module/class level and structural changes; reference-based sets for
changed constants; AST-equal patches (comment-only) select nothing. The
chosen set is packed across 1..8 workers minimizing `busiest + 2 s/worker`.

Two instrumented-suite tricks make the coverage map complete: sqlglot's
tests run fixture work in spawned processes (coverage misses them), so the
map build routes pool work through traced threads and pickles calls/results
like a real process boundary (`Expr.__reduce__` runs serde). Per-test
coverage contexts are per-thread, because the coverage writer drops
concurrent writes to one (file, context) bitmap.

## Local score

`make sample` on the 30 sample commits: **164.8 simulated s, 0 misses**
(oracle with perfect knowledge: 94.4 s). Every commit that breaks tests is
selected at exactly its oracle cost; the remainder is tests that execute
changed code on commits that happen to break nothing.

## Build

The image build is pure COPY — no downloads. `maps/` and the source snapshot
are committed; `mapbuilder/Dockerfile` regenerates them from the pinned
commit locally.

```sh
make sample   # build, run the 30 sample commits under the scorer's limits, score
make check    # output format only
```