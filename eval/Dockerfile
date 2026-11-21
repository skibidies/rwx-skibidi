# Testbed for local evaluation: the pinned suite, exactly as it is upstream.
# Ground truth for a patch = the tests pytest fails on the patched tree.
# Build from the repository root: docker build -f eval/Dockerfile -t blast-eval .
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends git patch \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone https://github.com/tobymao/sqlglot.git /src \
    && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY eval/run_one.py /run_one.py
