# Testbed for local evaluation: the pinned suite, exactly as the board runs it.
# Ground truth for a patch = the tests pytest fails on the patched tree.
# Build from the repository root: docker build -f eval/Dockerfile -t blast-eval .
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends git patch && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY eval/run_one.py /run_one.py
COPY maps/src.tar.gz /src.tar.gz
RUN mkdir -p /src && tar xzf /src.tar.gz -C /src && rm /src.tar.gz
WORKDIR /src
