"""Run all 170 non-alias questions through real query understanding and generation."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from evaluation.stress50 import run
from evaluation.stress50.scenarios import cases as content_cases
from evaluation.stress50.upgrade_holdout import questions as holdout_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-model", default="qwen3.7-flash-2026-07-15")
    parser.add_argument(
        "--report", required=True, help="New JSONL filename under generated/stress50"
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--ids", help="Comma-separated case IDs for a separate retry report")
    args = parser.parse_args()
    destination = run.OUTPUT / args.report
    if destination.exists():
        raise FileExistsError("Choose a new report name to preserve existing results")
    run.configure(
        SimpleNamespace(
            model_env=Path("/Users/cliff/Documents/TellerxChatBot/.env"),
            connection_doc=Path("/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md"),
            key_file=Path("/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt"),
            embedding_model="qwen3.7-text-embedding-flash",
            chat_model=args.chat_model,
        )
    )
    standard = [
        case
        for case in json.loads((run.OUTPUT / "questions.json").read_text())
        if case["kind"] != "global_english"
    ] + content_cases()
    cases = standard + holdout_cases()
    assert len(cases) == len({case["id"] for case in cases}) == 170
    if args.ids:
        selected = set(args.ids.split(","))
        cases = [case for case in cases if case["id"] in selected]
        if len(cases) != len(selected):
            raise ValueError("Unknown or duplicate selection")
    metadata = {
        "started_at": datetime.now(UTC).isoformat(),
        "model": args.chat_model,
        "standard_count": sum(not case["id"].startswith("h") for case in cases),
        "total": len(cases),
        "workers": args.workers,
        "scope": "all accessible projects",
        "entrypoint": "chat_application.answer; same service as /api/v1/chat",
        "source_sha256": {
            str(path.relative_to(run.ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (run.ROOT / "app").rglob("*.py")
        },
        "cases": cases,
    }
    destination.with_suffix(".meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2)
    )
    run.run_jobs(
        cases, lambda case: run.answer_one(case, None, True), destination, args.workers, False
    )


if __name__ == "__main__":
    main()
