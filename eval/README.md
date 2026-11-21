# Local evaluation

The board is a black box: one practice run per hour, and a missed failing test
costs 300 s. This directory measures the selector against ground truth we can
compute ourselves.

**Corpus.** Every non-merge commit after the pinned commit that touches
`sqlglot/` (or `tests/`) is turned into a patch against the pinned tree, so it
applies cleanly and looks like the patches the board sends.

**Truth.** `run_one.py` applies one patch to the pinned tree, runs the real
suite, and records every test pytest fails, errors or times out on — the
board's definition of a failing test.

**Score.** `eval/score.py` then applies the board's cost model:
`busiest worker + 2 s per worker`, plus 300 s per failing test we left out,
and reports the overhead over the oracle (a selection of exactly the failing
tests).

```sh
# ground truth for every patch in /tmp/eval/patches -> /tmp/eval/truth
docker run --rm -v /tmp/eval/patches:/patches:ro -v /tmp/eval/truth:/out \
  --cpus 8 blast-eval python /run_one.py <commit> /out 8
python eval/score.py
```
