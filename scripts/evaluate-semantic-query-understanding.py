#!/usr/bin/env python3
"""Run real semantic planning and retrieval against a labelled phrasing set."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from app.db import SessionLocal
from app.dependencies import query_understanding, retriever


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args()
    all_cases = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = all_cases[args.start :]
    if args.limit is not None:
        cases = cases[: args.limit]
    understanding_service = query_understanding()
    retrieval_service = retriever()
    results = []
    if args.append and args.output.exists():
        results = json.loads(args.output.read_text(encoding="utf-8")).get("results", [])
    with SessionLocal() as db:
        for case in cases:
            started = time.perf_counter()
            plan = understanding_service.understand(db, case["question"], pinned_model=args.model)
            evidence = retrieval_service.search(case["question"], [args.project_id], query_plan=plan)
            corpus = "\n".join(f"{item.filename}\n{item.heading_path or ''}\n{item.content}" for item in evidence).casefold()
            missing = [term for term in case["required_terms"] if term.casefold() not in corpus]
            passed = bool(evidence) == bool(case["expect_evidence"]) and not missing and plan.strategy == "semantic-qwen-v1"
            result = {"id":case["id"],"passed":passed,"strategy":plan.strategy,"subjects":list(plan.subjects),"requested_facts":list(plan.requested_facts),"constraints":list(plan.constraints),"retrieval_queries":list(plan.retrieval_queries),"evidence_count":len(evidence),"evidence_files":list(dict.fromkeys(item.filename for item in evidence)),"missing_terms":missing,"latency_ms":round((time.perf_counter()-started)*1000,3)}
            results.append(result)
            print(json.dumps(result,ensure_ascii=False))
    latencies=[row["latency_ms"] for row in results]
    report={"cases":len(results),"passed":sum(row["passed"] for row in results),"pass_rate":sum(row["passed"] for row in results)/len(results),"latency_ms":{"mean":round(statistics.mean(latencies),3),"max":round(max(latencies),3)},"results":results}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({key:report[key] for key in ("cases","passed","pass_rate","latency_ms")},ensure_ascii=False))
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
