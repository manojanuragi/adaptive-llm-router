# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# Non-root user — never run the app as root in a container.
RUN useradd --create-home --uid 1000 alr

WORKDIR /app

# Install git — needed for the Tier-0 git_status/git_diff/git_log tools
# to actually work if you route against a repo mounted into the container.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alr/ ./alr/
COPY demo.py pipeline_demo.py ./

# Workspace root for Tier-0 filesystem tools (git/grep/ls) — mount your
# actual repo here at runtime with -v $(pwd):/workspace if you want the
# tools to operate on real code rather than this empty default.
RUN mkdir -p /workspace && chown -R alr:alr /workspace /app
ENV ALR_WORKSPACE_ROOT=/workspace

USER alr

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1

# Default to a single worker; scale replicas at the orchestrator level
# rather than uvicorn --workers, since the in-memory rate limiter and
# SQLite trace store are per-process (see README deployment notes).
CMD ["uvicorn", "alr.api:app", "--host", "0.0.0.0", "--port", "8000"]
