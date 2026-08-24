FROM node:22-alpine AS frontend

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY frontend ./frontend
RUN npm run build


# Shared Python application layer. API, migration, and indexer images do not
# need office-conversion binaries, so keeping them here would add hundreds of
# megabytes and unnecessary CVE/patch surface to every production container.
FROM python:3.12-slim AS backend-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=0

WORKDIR /app
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import subprocess, sys, tomllib; dependencies = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]; subprocess.check_call([sys.executable, "-m", "pip", "install", *dependencies])'

COPY app ./app
COPY --from=frontend /build/app/static ./app/static
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-deps "."

COPY alembic.ini ./
COPY alembic ./alembic
COPY config ./config
COPY docs ./docs

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/knowledge \
    && chown -R appuser:appuser /app /data


# Only the ingestion worker converts legacy .doc/.xls files. Writer and Calc
# are sufficient for those conversions; the full LibreOffice meta-package
# would also install presentation, database, and drawing applications.
FROM backend-base AS worker-runtime

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-calc \
    && rm -rf /var/lib/apt/lists/partial

USER appuser
CMD ["knowledge-worker"]


# Lightweight default runtime shared by API, migrations, and the indexer.
FROM backend-base AS runtime

USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
