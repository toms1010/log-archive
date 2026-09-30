"""Small helpers shared by the test modules."""

from __future__ import annotations

import tarfile
from pathlib import Path


def member_names(archive: Path) -> set[str]:
    """Return the set of member names inside *archive*."""
    with tarfile.open(archive, "r:*") as tar:
        return {member.name for member in tar.getmembers()}


def read_member(archive: Path, name: str) -> bytes:
    """Return the contents of a single member."""
    with tarfile.open(archive, "r:*") as tar:
        handle = tar.extractfile(name)
        if handle is None:
            raise AssertionError(f"{name} is not a readable member")
        with handle:
            return handle.read()


def find_archive(directory: Path) -> Path:
    """Return the single archive created by the tool inside *directory*.

    Uses the production name matcher rather than duplicating it, so a change
    to the naming scheme cannot silently make the tests pass on the wrong file.
    """
    from log_archive.retention import is_owned_archive

    tarballs = sorted(p for p in directory.iterdir() if is_owned_archive(p))
    assert len(tarballs) == 1, f"expected exactly one archive, found {tarballs}"
    return tarballs[0]
