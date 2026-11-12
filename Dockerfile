# syntax=docker/dockerfile:1
# Multi-stage Dockerfile for RUPIKA / StockForge (SF-014A)

# Stage 1: Dependency resolution and virtual environment creation
FROM python:3.12-slim-bookworm AS builder

# Install uv binary from official distribution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy dependency definition and lockfile
COPY pyproject.toml uv.lock README.md ./

# Install locked dependencies into /app/.venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy application source code and migrations
COPY stockforge/ stockforge/
COPY alembic/ alembic/
COPY alembic.ini ./
COPY config/ config/

# Install the project into the virtual environment
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# Stage 2: Hardened, minimal runtime container
FROM python:3.12-slim-bookworm AS runtime

# Install system dependencies (ca-certificates for HTTPS providers, sqlite3 for CLI checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged non-root user (UID 10001:10001)
RUN groupadd -g 10001 rupika && \
    useradd -u 10001 -g 10001 -m -s /bin/sh -d /home/rupika rupika

WORKDIR /app

# Ensure /data volume directory exists with non-root ownership
RUN mkdir -p /data && chown -R 10001:10001 /data && chmod 750 /data

# Copy pre-built virtual environment and application files
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR="/data" \
    CONFIG_DIR="/app/config" \
    DATABASE_URL="sqlite:////data/stockforge.db"

# Enforce non-root execution
USER 10001:10001

ENTRYPOINT ["rupika"]
CMD ["doctor"]

