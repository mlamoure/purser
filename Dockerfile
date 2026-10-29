# syntax=docker/dockerfile:1.28
# One image, multiple roles: `purser serve` (default) / `purser worker` /
# `purser migrate`. Node exists only in the frontend build stage — the
# production image serves the static bundle with no Node runtime.

# Static assets are architecture-independent; build them on the native builder.
FROM --platform=$BUILDPLATFORM node:24-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build
# Self-contained MCP Apps widget bundles (issue #72) — served as ui://purser/*
RUN npm run build:widgets && npm run check:widgets


FROM python:3.15-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY purser/ purser/
COPY migrations/ migrations/
COPY alembic.ini README.md LICENSE ./
RUN uv sync --frozen --no-dev


FROM python:3.15-slim

# Dependencies arrive in the builder's virtualenv. Runtime pip/ensurepip are
# unused and carry a separate set of vendored libraries outside our lockfile.
RUN python -m pip uninstall --yes pip \
    && python -c "import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)"

# Fixed UID/GID: the Helm chart's runAsUser/runAsGroup name them (Kubernetes
# cannot verify runAsNonRoot for a named USER), tests/test_helm_chart.py pins both.
RUN groupadd -r -g 999 purser && useradd -r -u 999 -g purser purser

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY purser/ purser/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY --from=frontend /build/dist /app/static
# Widget bundles live OUTSIDE /app/static: reachable only through
# authenticated MCP resources/read (ui://purser/*), never the SPA mount.
COPY --from=frontend /build/dist-widgets /app/widgets
# Licence and third-party notices (#429), where Debian keeps each package's
# copyright file. Bundled web code's licences also ship beside the SPA
# (/app/static/third-party-licenses.txt) and inside each widget.
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/purser/

ENV PATH="/app/.venv/bin:$PATH"
USER purser
EXPOSE 8000

ENTRYPOINT ["purser"]
CMD ["serve"]
