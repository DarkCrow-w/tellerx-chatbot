#!/bin/sh
set -eu

VERIFY_DATABASE="knowledge_pgvector_verify"
VERIFY_URL="postgresql+psycopg://knowledge:knowledge@postgres:5432/$VERIFY_DATABASE"
export TELLERX_ENV_FILE="${TELLERX_ENV_FILE:-.env.example}"

docker compose up -d postgres
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U knowledge -d postgres <<SQL
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = '$VERIFY_DATABASE' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS $VERIFY_DATABASE;
CREATE DATABASE $VERIFY_DATABASE OWNER knowledge;
SQL

cleanup() {
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U knowledge -d postgres <<SQL
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = '$VERIFY_DATABASE' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS $VERIFY_DATABASE;
SQL
}
trap cleanup EXIT INT TERM

docker compose run --rm --no-deps --build \
  -e "DATABASE_URL=$VERIFY_URL" migrate alembic upgrade head
docker compose run --rm --no-deps \
  -e "DATABASE_URL=$VERIFY_URL" \
  -v "$PWD/evaluation:/app/evaluation:ro" \
  migrate python -m evaluation.smoke.pgvector

echo "PostgreSQL FTS and pgvector integration verification passed."
