# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
#
# The build is pure COPY: no downloads at all. maps/ holds the selection
# maps and a source snapshot of sqlglot at the pinned commit, both produced
# by mapbuilder/Dockerfile from that commit (run locally, results committed).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector.py /app/selector.py
COPY maps /app/maps
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]