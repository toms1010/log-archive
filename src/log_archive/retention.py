"""Discovery and retention of archives created by this tool.

Safety rules, in priority order:

1. Only filenames matching this tool's own naming scheme are ever considered.
   A hand-made ``backup.tar.gz`` in the same directory is invisible here and
   can never be deleted.
2. Only entries inside the output directory are considered.
3. Source log directories are never touched at all. Nothing in this module
   accepts a source path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from .utils import CleanupError

#: ``logs_archive_20260930_065300.tar.gz`` plus the ``_01`` collision form.
_ARCHIVE_RE: Final = re.compile(
    r"^logs_archive_(\d{8})_(\d{6})(?:_(\d{2,}))?\.(tar|tar\.gz|tar\.bz2|tar\.xz|tar\.zst)$"
)


@dataclass(frozen=True)
class OwnedArchive:
    """An archive this tool is confident it created."""

    path: Path
    created: datetime
    #: Collision suffix, used only to break ordering ties within one second.
    tie_breaker: int = 0

    @property
    def checksum_path(self) -> Path:
        """Path of the ``.sha256`` sidecar, whether or not it exists."""
        return self.path.with_name(self.path.name + ".sha256")

    @property
    def has_checksum(self) -> bool:
        """Whether a checksum sidecar sits next to this archive."""
        return self.checksum_path.is_file()


def parse_name(name: str) -> tuple[datetime, int] | None:
    """Parse an archive name this tool would produce.

    Returns:
        ``(created, collision_suffix)``, or ``None`` when *name* is not one of
        ours. The date is parsed as well as the shape, so a name like
        ``logs_archive_20261301_065300.tar.gz`` - month 13 - is rejected rather
        than being carried around as a bogus timestamp.
    """
    match = _ARCHIVE_RE.match(name)
    if match is None:
        return None
    try:
        created = datetime.strptime(f"{match.group(1)}_{match.group(2)}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return created, int(match.group(3)) if match.group(3) else 0


def is_owned_archive(path: Path) -> bool:
    """Return True if *path* is named exactly like an archive this tool writes."""
    return parse_name(path.name) is not None


def discover(directory: Path) -> list[OwnedArchive]:
    """Return this tool's archives in *directory*, newest first.

    Foreign files, directories, and unparseable names are ignored rather than
    reported, so a busy output directory cannot break the listing.
    """
    found: list[OwnedArchive] = []
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return found
    except OSError as exc:
        raise CleanupError(f"Cannot read {directory}: {exc}") from exc

    for entry in entries:
        parsed = parse_name(entry.name)
        if parsed is None:
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        found.append(OwnedArchive(entry, parsed[0], parsed[1]))

    return sorted(found, key=lambda item: (item.created, item.tie_breaker), reverse=True)


def select_for_deletion(archives: Sequence[OwnedArchive], keep: int) -> list[OwnedArchive]:
    """Return the archives that fall outside the newest *keep* entries.

    Raises:
        ValueError: if *keep* is negative.
    """
    if keep < 0:
        raise ValueError("keep must be zero or greater")
    return list(archives[keep:])


@dataclass(frozen=True)
class CleanupPlan:
    """What a cleanup would do, computed without touching anything."""

    found: list[OwnedArchive]
    keep: list[OwnedArchive]
    remove: list[OwnedArchive]

    @property
    def paths_to_remove(self) -> list[Path]:
        """Every file that would be unlinked, checksums included."""
        paths: list[Path] = []
        for archive in self.remove:
            paths.append(archive.path)
            if archive.has_checksum:
                paths.append(archive.checksum_path)
        return paths


def plan_cleanup(directory: Path, keep: int) -> CleanupPlan:
    """Compute which archives *keep* would leave behind."""
    archives = discover(directory)
    return CleanupPlan(
        found=archives,
        keep=archives[:keep],
        remove=select_for_deletion(archives, keep),
    )


def apply_cleanup(directory: Path, keep: int) -> tuple[list[Path], CleanupError | None]:
    """Delete all but the newest *keep* archives in *directory*.

    Returns:
        ``(deleted_paths, error)``. Partial failures are collected rather than
        raised, so one locked file does not strand the rest of the cleanup;
        *error* summarises them, or is ``None`` on full success.
    """
    plan = plan_cleanup(directory, keep)
    deleted: list[Path] = []
    failures: list[str] = []

    # Sidecars are deleted alongside their archive, and every file actually
    # removed is reported, so the count the user sees matches what happened.
    for target in plan.paths_to_remove:
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(f"{target.name}: {exc.strerror or exc}")
            continue
        deleted.append(target)

    error = CleanupError("; ".join(failures)) if failures else None
    return deleted, error
