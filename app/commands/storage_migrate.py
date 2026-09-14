"""Offline, resumable migration of database-referenced objects with full SHA-256 verification."""

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from app.core.config import Settings
from app.db.models import DocumentArtifact, DocumentVersion, EmbeddingCache, IngestionJob
from app.db.session import SessionLocal
from app.integrations.s3_storage import S3ObjectStorage
from app.integrations.storage import LocalObjectStorage

SOURCES = (
    (DocumentVersion, "storage_path", "sha256"),
    (DocumentArtifact, "object_uri", "sha256"),
    (EmbeddingCache, "object_uri", "checksum"),
)


def inventory(db, local):
    """Validate every reference before any copy; retain old/new mapping for rollback."""
    entries = []
    for model, field, hash_field in SOURCES:
        for row in db.scalars(select(model)):
            old = getattr(row, field)
            path = local.resolve(old)
            key = path.relative_to(local.root.resolve()).as_posix()
            if not path.is_file():
                raise FileNotFoundError(f"Missing source object: {key}")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != getattr(row, hash_field):
                raise ValueError(f"Source checksum mismatch: {key}")
            entries.append(
                {
                    "table": model.__tablename__,
                    "id": row.id,
                    "field": field,
                    "old": old,
                    "key": key,
                    "sha256": digest,
                    "size": path.stat().st_size,
                }
            )
    return entries


def transfer(entries, source, target, copy):
    """Check downloaded content, not metadata or ETags."""
    seen = set()
    for entry in entries:
        key = entry["key"]
        if key in seen:
            continue
        seen.add(key)
        if copy:
            with source.open(entry["old"]) as stream:
                # Source objects are bounded by the existing upload/artifact limits.
                target.save_bytes(key, stream.read())
        with target.open(key) as stream:
            digest, size = hashlib.sha256(), 0
            while data := stream.read(1024 * 1024):
                digest.update(data)
                size += len(data)
        if digest.hexdigest() != entry["sha256"] or size != entry["size"]:
            raise ValueError(f"Target checksum mismatch: {key}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "copy", "verify", "normalize", "export"])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--offline", action="store_true", help="Confirm all chatbot writers stopped"
    )
    parser.add_argument("--local-root", type=Path)
    args = parser.parse_args()
    if args.mode != "check" and not args.offline:
        parser.error("Stop all chatbot writers and pass --offline")
    settings = Settings()
    local = LocalObjectStorage(args.local_root or settings.storage_root)
    with SessionLocal() as db:
        active = db.scalar(
            select(IngestionJob.id)
            .where(IngestionJob.status.in_(["running", "queued", "retrying"]))
            .limit(1)
        )
        if active:
            raise RuntimeError("Unfinished ingestion jobs; drain them before migration")
        if args.mode == "export":
            remote = S3ObjectStorage(settings)
            entries = []
            for model, field, hash_field in SOURCES:
                for row in db.scalars(select(model)):
                    key = remote.key(getattr(row, field))
                    data = remote.read_bytes(key)
                    sha = hashlib.sha256(data).hexdigest()
                    if sha != getattr(row, hash_field):
                        raise ValueError(f"Remote checksum mismatch: {key}")
                    local.save_bytes(key, data)
                    entries.append({"key": key, "sha256": sha, "size": len(data)})
        else:
            entries = inventory(db, local)
            if args.mode != "check":
                remote = S3ObjectStorage(settings)
                transfer(entries, local, remote, args.mode == "copy")
            if args.mode == "normalize":
                models = {model.__tablename__: model for model, _, _ in SOURCES}
                for entry in entries:
                    row = db.get(models[entry["table"]], entry["id"])
                    setattr(row, entry["field"], entry["key"])
                # Save mapping before committing any changed references.
                write_manifest(args.manifest, entries)
                db.commit()
        write_manifest(args.manifest, entries)
        print(f"{args.mode}: verified {len(entries)} references")


def write_manifest(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(entries, ensure_ascii=False, indent=2))
    temp.replace(path)


if __name__ == "__main__":
    main()
