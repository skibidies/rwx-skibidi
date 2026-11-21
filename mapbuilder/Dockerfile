# Builds the test-selection maps from the pinned sqlglot commit.
# Run:  docker build -f mapbuilder/Dockerfile --target export --output type=local,dest=./maps mapbuilder
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone https://github.com/tobymao/sqlglot.git /src \
    && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
    && pip install --no-cache-dir coverage pytest-cov
ENV SKIP_INTEGRATION=0
COPY conftest_instrument.py /src/tests/conftest.py
# One suite run with per-test coverage contexts. Slow but it is a one-time build.
RUN python -m pytest tests -q -p no:cacheprovider \
    --cov=sqlglot --cov=tests --cov-context=test --cov-report= \
    || echo "SOME TESTS FAILED (logged above); maps still built"
COPY extract.py static_maps.py tests.json /mapbuilder/
RUN python /mapbuilder/extract.py && python /mapbuilder/static_maps.py \
    && tar czf /maps/src.tar.gz --exclude='*.gz' --exclude='__pycache__' \
         -C /src sqlglot tests sqlglotc setup.py pyproject.toml MANIFEST.in setup.cfg Makefile \
    && ls -la /maps

FROM scratch AS export
COPY --from=builder /maps /