# papertrade-india Streamlit console — production image.
# Pinned to a patch tag for reproducible builds.
FROM python:3.12.7-slim

# No .pyc, unbuffered logs, no pip cache. TZ=IST so the naive datetimes
# the NSE providers produce line up with the host clock (cache-age math,
# order timestamps) regardless of where the droplet lives.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata

# tzdata for the TZ above; curl for the container HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build the package first (better layer caching). hatchling needs these.
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src
RUN pip install ".[ui]"

# App entrypoint + runtime state dir.
COPY app.py ./
RUN mkdir -p data

# Run as a non-root user that owns the writable state dir. A container
# escape or dependency RCE then runs unprivileged, not as uid 0.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8501

# Fail the container health if Streamlit stops answering (not just on
# process exit) so the orchestrator/Caddy can react to a wedged app.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

# Headless server bound to all interfaces; Caddy terminates TLS in front.
CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
