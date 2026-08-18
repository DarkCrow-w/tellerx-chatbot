#!/bin/sh
set -eu

CORPUS_DIR=${1:-evaluation/generated/benchmark-1k}
ANSWER_LIMIT=${ANSWER_LIMIT:-20}
PLUS_MODEL=${PLUS_MODEL:-qwen3.7-plus-2026-05-26}
MAX_MODEL=${MAX_MODEL:-qwen3.7-max-2026-05-20}

if [ ! -f "$CORPUS_DIR/manifest.jsonl" ] || [ ! -f "$CORPUS_DIR/questions.jsonl" ]; then
  echo "Benchmark corpus is missing: $CORPUS_DIR" >&2
  exit 2
fi

# This must pass before any bulk or answer workload is allowed to consume API
# quota. qwen-diagnostics never prints the API key or provider response text.
qwen-diagnostics

knowledge-benchmark index-existing "$CORPUS_DIR"
knowledge-benchmark retrieve "$CORPUS_DIR"
knowledge-benchmark answers "$CORPUS_DIR" --limit "$ANSWER_LIMIT" --model "$PLUS_MODEL"
knowledge-benchmark answers "$CORPUS_DIR" --limit "$ANSWER_LIMIT" --model "$MAX_MODEL"

echo "Qwen 1K gate completed successfully."
