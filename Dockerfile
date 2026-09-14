# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so application edits do not invalidate the wheel-build layer.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY evals ./evals

# Writable location for the wallet journal and memory store. When a Render disk is
# mounted at /data, set MEMORY_PATH=/data/memory to make it survive restarts.
RUN mkdir -p /app/var/memory \
    && useradd --create-home --uid 10001 agent \
    && chown -R agent:agent /app
USER agent

ENV PORT=10000 \
    MEMORY_PATH=/app/var/memory
EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",10000)}/healthz', timeout=4).status==200 else 1)"

# Single worker: the wallet ledger is in-process, so multiple workers would each hold
# their own view of the spend envelope. Scale by raising caps, not replicas, until the
# ledger is moved to shared storage.
# No --log-config: uvicorn's CLI rejects an empty config file, and the lifespan's
# configure_logging() already reassigns uvicorn's handlers to the JSON formatter.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
