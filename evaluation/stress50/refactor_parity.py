"""Compare retrieval with a trusted Git baseline using identical adapter responses.

Reads the existing synthetic evaluation corpus. Calls PostgreSQL and embeddings,
but never calls query understanding or answer generation. Each baseline request
records adapter results; the refactored retriever must consume those same calls.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from evaluation.stress50 import run


class RecordedCalls:
    """Record external calls once, then reject any unexpected call during replay."""

    def __init__(self, target):
        self.target = target
        self.responses = {}
        self.replaying = False

    def __getattr__(self, name):
        def call(*args, **kwargs):
            key = (name, repr(args), repr(sorted(kwargs.items())))
            if key not in self.responses:
                if self.replaying:
                    raise AssertionError(f"Refactor changed adapter call: {name}")
                self.responses[key] = getattr(self.target, name)(*args, **kwargs)
            return copy.deepcopy(self.responses[key])

        return call


def load_baseline(ref: str):
    """Load only trusted repository source, keeping its imports isolated."""
    modules = {}
    for filename in ("integrations/search", "services/retrieval"):
        code = subprocess.check_output(
            ["git", "show", f"{ref}:app/{filename}.py"], text=True, cwd=run.ROOT
        )
        name = "_refactor_baseline_" + filename.rsplit("/", 1)[1]
        code = code.replace(
            "from app.integrations.search import", "from _refactor_baseline_search import"
        )
        module = types.ModuleType(name)
        sys.modules[name] = module
        exec(compile(code, f"{ref}:app/{filename}.py", "exec"), module.__dict__)  # noqa: S102 - explicitly selected trusted repository revision
        modules[filename] = module
    return modules["services/retrieval"].Retriever


def saved_cases():
    records = {}
    for name in (
        "upgrade-answers.jsonl",
        "upgrade-extra-answers.jsonl",
        "upgrade-dated-answers.jsonl",
        "upgrade-holdout-answers.jsonl",
    ):
        for line in (run.OUTPUT / name).read_text().splitlines():
            record = json.loads(line)
            plan = record.get("retrieval", {}).get("query_understanding", {})
            if plan and plan.get("fallback_reason") != "NoModelAvailable":
                records[record["id"]] = record
    return list(records.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Trusted pre-refactor Git commit")
    parser.add_argument("--report", default="refactor-parity.jsonl")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    run.configure(
        SimpleNamespace(
            model_env=Path("/Users/cliff/Documents/TellerxChatBot/.env"),
            connection_doc=Path("/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md"),
            key_file=Path("/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt"),
            embedding_model="qwen3.7-text-embedding-flash",
            chat_model="qwen3.7-flash-2026-07-15",
        )
    )
    from app.services.query_understanding import QueryPlan
    from app.services.retrieval import Retriever

    baseline_retriever = load_baseline(args.baseline)

    def compare(record):
        data = record["retrieval"]["query_understanding"]
        plan = QueryPlan(
            **{
                field.name: data[field.name]
                for field in dataclasses.fields(QueryPlan)
                if field.name in data
            }
        )
        configured = run.container().retrieval
        index = RecordedCalls(configured.index)
        model = RecordedCalls(configured.model_client)
        before = baseline_retriever(configured.settings, index, model)
        after = Retriever(configured.settings, index, model)
        expected = before.search_with_scope(record["question"], [], query_plan=plan)
        index.replaying = model.replaying = True
        actual = after.search_with_scope(record["question"], [], query_plan=plan)
        unchanged = dataclasses.asdict(expected) == dataclasses.asdict(actual)
        return {
            "id": record["id"],
            "pass": unchanged,
            "baseline": args.baseline,
            "evidence_count": len(actual.evidence),
            "scope": actual.resolved_scope,
            "answer_generation_tested": False,
        }

    run.run_jobs(saved_cases(), compare, run.OUTPUT / args.report, 4, args.resume)


if __name__ == "__main__":
    main()
