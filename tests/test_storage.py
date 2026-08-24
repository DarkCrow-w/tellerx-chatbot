from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import pytest

from app.integrations.storage import LocalObjectStorage


def test_vector_round_trip_and_checksum(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    vector = [0.125, -0.5, 1.0]
    uri, checksum, _ = storage.save_vector("model-fingerprint", "content-hash", vector)

    restored = storage.load_vector(uri, 3, checksum)

    assert restored == pytest.approx(vector)
    assert Path(uri).is_absolute() is False


def test_vector_checksum_prevents_silent_corruption(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    uri, _, _ = storage.save_vector("model", "hash", [1.0, 2.0])
    target = storage.resolve(uri)
    target.write_bytes(zlib.compress(struct.pack("<2f", 9.0, 9.0)))

    with pytest.raises(ValueError, match="checksum"):
        storage.load_vector(uri, 2, hashlib.sha256(b"wrong").hexdigest())


def test_immutable_object_path_rejects_different_bytes(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    storage.save_bytes("artifacts/version/normalized.json.zlib", b"first")

    with pytest.raises(ValueError, match="Immutable object collision"):
        storage.save_bytes("artifacts/version/normalized.json.zlib", b"second")
