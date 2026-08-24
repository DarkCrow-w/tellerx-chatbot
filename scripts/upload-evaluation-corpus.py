#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import time
from pathlib import Path

import httpx


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def upload_one(
    client: httpx.AsyncClient,
    corpus: Path,
    row: dict,
    semaphore: asyncio.Semaphore,
) -> dict:
    file_path = corpus / row["path"]
    data = {
        "project": row["project"],
        "document_type": row["document_type"],
        "lifecycle_status": row["lifecycle_status"],
        "version_label": row.get("version_label") or "",
        "logical_key": row.get("logical_key") or row["logical_filename"],
        "owner": row.get("owner") or "",
    }
    if row.get("effective_at"):
        data["effective_at"] = row["effective_at"]
    async with semaphore:
        with file_path.open("rb") as stream:
            response = await client.post(
                "/api/v1/documents",
                data=data,
                files={
                    "file": (
                        row["logical_filename"],
                        stream,
                        mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
                    )
                },
            )
        response.raise_for_status()
        return {**response.json(), "filename": row["logical_filename"]}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a benchmark manifest through the public document API")
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--format",
        dest="formats",
        action="append",
        help="Only upload this manifest format; repeat to select multiple formats",
    )
    args = parser.parse_args()

    rows = read_jsonl(args.corpus / "manifest.jsonl")
    if args.formats:
        selected_formats = {value.casefold().lstrip(".") for value in args.formats}
        rows = [row for row in rows if str(row.get("format", "")).casefold() in selected_formats]
    if not rows:
        raise ValueError("No manifest rows matched the requested format filter")
    semaphore = asyncio.Semaphore(args.concurrency)
    timeout = httpx.Timeout(120.0, connect=10.0)
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout) as client:
        # Establish the project with one request before fan-out. The public API
        # also protects project creation transactionally, but this keeps the
        # load generator compatible with older running images.
        first = await upload_one(client, args.corpus, rows[0], semaphore)
        uploads = [first]
        uploads.extend(
            await asyncio.gather(
                *(upload_one(client, args.corpus, row, semaphore) for row in rows[1:])
            )
        )
        pending = {row["job_id"]: row for row in uploads}
        statuses: dict[str, int] = {}
        while pending:
            if time.monotonic() - started > args.timeout_seconds:
                raise TimeoutError(f"Timed out waiting for {len(pending)} ingestion jobs")
            completed = []
            for job_id, row in list(pending.items()):
                response = await client.get(f"/api/v1/ingestion-jobs/{job_id}")
                response.raise_for_status()
                job = response.json()
                if job["status"] in {"succeeded", "failed"}:
                    statuses[job["status"]] = statuses.get(job["status"], 0) + 1
                    if job["status"] == "failed":
                        raise RuntimeError(f"Ingestion failed for {row['filename']}: {job.get('error_message')}")
                    completed.append(job_id)
            for job_id in completed:
                pending.pop(job_id, None)
            if pending:
                await asyncio.sleep(2)

    report = {
        "documents": len(rows),
        "accepted": len(uploads),
        "duplicates": sum(bool(row.get("duplicate")) for row in uploads),
        "job_statuses": statuses,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "formats": args.formats or ["all"],
    }
    report_path = args.corpus / "upload-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
