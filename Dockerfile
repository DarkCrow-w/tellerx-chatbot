FROM node:22-alpine AS frontend

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY frontend ./frontend
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice \
    && rm -rf /var/lib/apt/lists/*

# Keep the large LibreOffice layer stable; use BuildKit's pip cache below.
ENV PIP_NO_CACHE_DIR=0

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
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
