"""SHA-256 checksum generation, streamed so archives never enter RAM."""

from __future__ import annotations

import hashlib
from pathlib import Path

#: Archives are read in 1 MiB slices; a log archiver must stay streaming.
_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of *path*, reading it in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum_file(archive_path: Path) -> tuple[str, Path]:
    """Write ``<archive>.sha256`` next to *archive_path*.

    The file uses the standard ``sha256sum`` layout so it can be checked with
    ``sha256sum -c``.

    Returns:
        A ``(digest, checksum_path)`` pair.
    """
    digest = sha256_file(archive_path)
    checksum_path = archive_path.with_name(archive_path.name + ".sha256")
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    return digest, checksum_path


def read_checksum_file(checksum_path: Path) -> str:
    """Return the digest recorded in a ``sha256sum`` style checksum file."""
    first_line = checksum_path.read_text(encoding="utf-8").splitlines()[0]
    return first_line.split()[0]
