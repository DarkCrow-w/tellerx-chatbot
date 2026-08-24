#!/bin/sh
set -eu

CORPUS="${1:-evaluation/generated/crossdoc-20}"
VERIFY_PORT="${TELLERX_VERIFY_PORT:-18000}"
BASE_URL="http://127.0.0.1:$VERIFY_PORT"
COMPOSE_PROJECT="${TELLERX_VERIFY_PROJECT:-tellerx-pgvector-gate}"
export TELLERX_ENV_FILE="${TELLERX_ENV_FILE:-.env.example}"
export TELLERX_VERIFY_PORT="$VERIFY_PORT"

if [ ! -f "$CORPUS/manifest.jsonl" ] || [ ! -f "$CORPUS/questions.jsonl" ]; then
  echo "Cross-document corpus is missing: $CORPUS" >&2
  exit 2
fi
if [ ! -f "${QWEN_API_KEY_SECRET_FILE:-./Qwen/Qwen token.txt}" ]; then
  echo "Set QWEN_API_KEY_SECRET_FILE to the existing Qwen token file." >&2
  exit 2
fi

compose() {
  docker compose -p "$COMPOSE_PROJECT" -f docker-compose.verify.yml "$@"
}

on_exit() {
  status=$?
  if [ "$status" -ne 0 ]; then
    compose logs --tail=200 postgres migrate api worker indexer || true
  fi
}
trap on_exit EXIT
trap 'exit 130' INT TERM HUP

compose up -d --build postgres migrate api worker indexer

attempt=0
until curl -fsS "$BASE_URL/health/ready" >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 90 ]; then
    echo "Verification API did not become ready." >&2
    exit 1
  fi
  sleep 2
done

compose run --rm api qwen-diagnostics
compose run --rm \
  -v "$PWD/scripts:/app/scripts:ro" \
  -v "$PWD/evaluation:/app/evaluation" \
  migrate python scripts/upload-evaluation-corpus.py "/app/$CORPUS" \
  --base-url "http://api:8000" --concurrency 8 --timeout-seconds 1800

compose run --rm \
  -v "$PWD/evaluation:/app/evaluation" \
  api knowledge-benchmark retrieve "/app/$CORPUS"

if [ "${RUN_ANSWER_GATE:-1}" = "1" ]; then
  compose run --rm \
    -v "$PWD/evaluation:/app/evaluation" \
    api knowledge-benchmark answers "/app/$CORPUS" \
    --limit "${ANSWER_GATE_LIMIT:-20}" \
    --model "${ANSWER_GATE_MODEL:-qwen3.7-plus-2026-05-26}"
fi

curl -fsS "$BASE_URL/api/v1/index/status" > "$CORPUS/pgvector-index-status-before-restart.json"
compose restart postgres
attempt=0
until curl -fsS "$BASE_URL/health/ready" >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 90 ]; then
    echo "Verification API did not recover after PostgreSQL restart." >&2
    exit 1
  fi
  sleep 2
done
curl -fsS "$BASE_URL/api/v1/index/status" > "$CORPUS/pgvector-index-status-after-restart.json"
compose exec -T postgres psql -U knowledge -d knowledge_crossdoc_verify -P pager=off -c \
  "SELECT count(*) AS search_rows, count(embedding) AS vectors FROM chunk_search_index;"

echo "Cross-document pgvector gate passed. Results: $CORPUS"
echo "To stop the verification containers: docker compose -p $COMPOSE_PROJECT -f docker-compose.verify.yml stop"
