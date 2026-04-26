FROM python:3.12-slim AS base

# System deps minime
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Bucharest \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

# Copy requirements first (cache layer)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy app code
COPY . .

# Persistent state (bot_state.json) — mount un volum aici in productie.
RUN mkdir -p /data
VOLUME ["/data"]

# Default port — se poate override prin CHART_PORT env var
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -sf http://localhost:${CHART_PORT:-8090}/api/status || exit 1

# `-u` pt unbuffered logs (dublu insurance peste PYTHONUNBUFFERED=1)
CMD ["python", "-u", "main.py"]
