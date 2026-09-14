"""Byte-integrity boundary for formal approved-analysis artifacts."""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from typing import BinaryIO

from services.storage_paths import (
    ManagedReadFile,
    _file_generation,
    open_managed_file_for_read,
)


class FormalArtifactIntegrityError(ValueError):
    """A formal artifact's registered bytes cannot be proven."""


@dataclass
class VerifiedArtifactSnapshot:
    """Immutable response snapshot with the ManagedReadFile interface."""

    stream: BinaryIO
    stat_result: os.stat_result
    size: int

    def close(self) -> None:
        self.stream.close()


def _hash_stream(stream: BinaryIO, *, sink: BinaryIO | None = None) -> tuple[str, int]:
    hasher = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        hasher.update(chunk)
        total += len(chunk)
        if sink is not None:
            sink.write(chunk)
    return hasher.hexdigest(), total


def sha256_managed_generation(
    root_value: str,
    stored_path: str,
    expected: os.stat_result | None,
) -> str:
    """Hash exactly the output generation recorded by its managed writer.

    The generated-file registrar still performs the authoritative commit-window
    generation check. This helper binds formal metadata to the same generation
    before registration; a change before or during hashing fails closed.
    """
    if expected is None:
        raise FormalArtifactIntegrityError("formal artifact has no recorded written generation")

    opened = open_managed_file_for_read(root_value, stored_path)
    try:
        if _file_generation(opened.stat_result) != _file_generation(expected):
            raise FormalArtifactIntegrityError("formal artifact changed before content hashing")
        digest, total = _hash_stream(opened.stream)
        after = os.fstat(opened.stream.fileno())
        if (
            _file_generation(after) != _file_generation(expected)
            or total != int(expected.st_size)
        ):
            raise FormalArtifactIntegrityError("formal artifact changed while content hashing")
        return digest
    finally:
        opened.close()


def verified_artifact_snapshot(
    opened: ManagedReadFile,
    expected_sha256: str,
    *,
    spool_limit: int = 8 * 1024 * 1024,
) -> VerifiedArtifactSnapshot:
    """Copy pinned bytes into an immutable snapshot while verifying SHA-256.

    The original managed handle is always closed here. The returned snapshot is
    independent of later in-place writes to the managed output file, which matters
    on POSIX where a second process can mutate an open inode.
    """
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        opened.close()
        raise FormalArtifactIntegrityError("formal artifact SHA-256 metadata is missing or invalid")

    source_stat = opened.stat_result
    snapshot = tempfile.SpooledTemporaryFile(max_size=int(spool_limit), mode="w+b")
    try:
        digest, total = _hash_stream(opened.stream, sink=snapshot)
        after = os.fstat(opened.stream.fileno())
        if _file_generation(after) != _file_generation(source_stat):
            raise FormalArtifactIntegrityError("formal artifact changed while snapshotting")
        if total != int(source_stat.st_size):
            raise FormalArtifactIntegrityError("formal artifact size changed while snapshotting")
        if digest != expected:
            raise FormalArtifactIntegrityError("formal artifact bytes do not match the registered SHA-256")
        snapshot.seek(0)
        return VerifiedArtifactSnapshot(
            stream=snapshot,
            stat_result=source_stat,
            size=total,
        )
    except Exception:
        snapshot.close()
        raise
    finally:
        opened.close()
