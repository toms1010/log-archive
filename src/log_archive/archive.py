"""The streaming archive engine.

Design notes
------------

**One directory scan.** The tree is walked with :func:`os.scandir` exactly once
per directory and each entry is handed straight to ``tarfile``. Nothing is
buffered, nothing is re-listed, and file bodies are copied in 1 MiB slices, so
peak memory does not grow with the size of the archive.

**One unreadable file must not destroy the run.** The original script aborted
the entire archive on the first ``EACCES`` *and then deleted the partial file*,
so a single root-owned file in ``/var/log`` meant you got no backup at all. Here
an unreadable file is recorded, counted, and skipped; the rest of the tree is
still archived.

**No half-written archives.** The caller owns the cleanup path so that the same
``finally`` covers both failures and interrupts.

**No self-archiving.** If the output directory lives inside the source tree it
is pruned, so an archive can never contain itself or an older archive.
"""

from __future__ import annotations

import errno
import io
import os
import stat
import tarfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from .compression import CompressionFormat
from .exclusions import ExclusionRules
from .manifest import Manifest
from .statistics import ArchiveStats
from .utils import (
    MANIFEST_MEMBER,
    ArchiveError,
    LogArchiveError,
    PermissionDeniedError,
    describe_oserror,
    is_permission_error,
)

#: Slice size used when copying a file body into the archive.
COPY_BUFFER_SIZE: Final = 1024 * 1024

#: Copy buffer name used by tarfile itself, tuned to the same order.
_TAR_BUFSIZE: Final = 64 * 1024


def describe_special(mode: int) -> str:
    """Name the kind of non-archiveable file implied by *mode*."""
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character device"
    if stat.S_ISBLK(mode):
        return "block device"
    is_door = getattr(stat, "S_ISDOOR", None)
    if is_door is not None and is_door(mode):
        return "door"
    return "special file"


def _open_no_follow(path: Path) -> io.BufferedReader:
    """Open *path* for reading without traversing a final symlink component.

    ``O_NOFOLLOW`` closes the window between scanning an entry and reading it,
    during which a regular file could be swapped for a symlink pointing at,
    say, ``/etc/shadow``.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError(
                errno.ELOOP, "entry became a symlink during archiving", str(path)
            ) from exc
        raise
    return open(descriptor, "rb", closefd=True)


def _error_for(exc: OSError, path: Path) -> LogArchiveError:
    """Build the most specific error for a failed write to *path*."""
    detail = describe_oserror(exc)
    if exc.errno == errno.ENOSPC:
        return ArchiveError(
            f"Ran out of disk space while writing {path} ({detail}). "
            "Free some space or choose a different --output directory."
        )
    if exc.errno == errno.EDQUOT:
        return ArchiveError(f"Disk quota exceeded while writing {path}.")
    if is_permission_error(exc):
        return PermissionDeniedError(
            f"Permission denied while writing {path} ({detail}). Try: sudo log-archive ..."
        )
    if exc.errno == errno.EROFS:
        return ArchiveError(f"Cannot write to {path}: read-only filesystem.")
    return ArchiveError(f"Could not write archive {path}: {detail}")


@dataclass(frozen=True)
class Entry:
    """One path selected for archiving."""

    path: Path
    #: Path relative to the source root, using ``/`` separators. Empty for the
    #: root itself. This is what exclusion rules are matched against.
    relative: str
    #: Name the entry will have inside the archive.
    arcname: str
    #: ``"file"``, ``"dir"``, or ``"symlink"``.
    kind: str
    size: int = 0


class TreeScanner:
    """Yield the entries of a log tree, pruning anything unsafe.

    Args:
        source: Root of the tree. It becomes the archive's top level folder.
        rules: Exclusion rules applied to every path.
        follow_symlinks: When false (the default) symlinks are stored as
            symlinks and never opened, so a link to ``/etc/shadow`` cannot
            pull its contents into an archive. When true, only symlinks whose
            target resolves *inside* ``source`` are followed, and each real
            inode is visited at most once so cycles terminate.
        skip_paths: Resolved paths whose subtrees are never entered. This is
            how the output directory is kept out of its own archive.
    """

    def __init__(
        self,
        source: Path,
        rules: ExclusionRules,
        *,
        follow_symlinks: bool = False,
        skip_paths: Sequence[Path] = (),
        stats: ArchiveStats | None = None,
    ) -> None:
        self.source = source
        self.rules = rules
        self.follow_symlinks = follow_symlinks
        self.stats = stats if stats is not None else ArchiveStats()
        self._root_name = source.name or source.anchor.strip("/") or "archive"
        self._skip_paths = frozenset(skip_paths)
        self._source_resolved = source.resolve()
        self._seen_inodes: set[tuple[int, int]] = set()

    @property
    def root_name(self) -> str:
        """Name of the top level folder inside the archive."""
        return self._root_name

    def __iter__(self) -> Iterator[Entry]:
        """Walk the tree depth first, in sorted order."""
        try:
            root_stat = self.source.stat()
        except OSError:
            root_stat = None
        if root_stat is not None:
            # Seed with the root's inode so a symlink pointing back at the
            # source root is recognised as a loop.
            self._seen_inodes.add((root_stat.st_dev, root_stat.st_ino))
        yield Entry(self.source, "", self._root_name, "dir", 0)
        yield from self._walk(self.source, "")

    def _within_source(self, target: Path) -> bool:
        try:
            target.relative_to(self._source_resolved)
        except ValueError:
            return False
        return True

    def _walk(self, directory: Path, prefix: str) -> Iterator[Entry]:
        try:
            resolved = directory.resolve()
        except OSError:
            resolved = directory
        if resolved in self._skip_paths:
            return
        try:
            with os.scandir(directory) as scan:
                # scandir already buffers the directory; sorting here is free
                # and keeps archive order stable between runs.
                children = sorted(scan, key=lambda item: item.name)
        except PermissionError as exc:
            self.stats.note_skipped(prefix or ".", f"permission denied: {exc.strerror}")
            return
        except OSError as exc:
            self.stats.note_skipped(prefix or ".", f"unreadable directory: {exc}")
            return

        for child in children:
            yield from self._handle(child, prefix)

    def _handle(self, child: os.DirEntry[str], prefix: str) -> Iterator[Entry]:
        relative = f"{prefix}/{child.name}" if prefix else child.name
        arcname = f"{self._root_name}/{relative}"

        if self.rules.matches(relative):
            self.stats.note_excluded(relative)
            return

        child_path = Path(child.path)
        try:
            is_link = child.is_symlink()
        except OSError as exc:
            self.stats.note_skipped(relative, f"stat failed: {exc.strerror}")
            return

        if is_link:
            yield from self._handle_symlink(child, child_path, relative, arcname)
            return

        try:
            if child.is_dir(follow_symlinks=False):
                if child_path.resolve() in self._skip_paths:
                    # The output directory: keep it and everything under it
                    # out of the archive, so an archive can never contain
                    # itself or a previous run.
                    self.stats.note_excluded(relative)
                    return
                yield Entry(child_path, relative, arcname, "dir", 0)
                yield from self._walk(child_path, relative)
                return
            if child.is_file(follow_symlinks=False):
                # follow_symlinks=False hits the scandir cache, so this costs
                # no extra syscall.
                size = child.stat(follow_symlinks=False).st_size
                yield Entry(child_path, relative, arcname, "file", size)
                return
            mode = child.stat(follow_symlinks=False).st_mode
        except OSError as exc:
            self.stats.note_skipped(relative, f"stat failed: {exc.strerror}")
            return

        # Sockets, FIFOs, and device nodes carry no log data. Storing them
        # would be pointless at best; a FIFO in particular is a trap for any
        # tool that tries to read it.
        self.stats.note_skipped(relative, describe_special(mode))

    def _handle_symlink(
        self,
        child: os.DirEntry[str],
        child_path: Path,
        relative: str,
        arcname: str,
    ) -> Iterator[Entry]:
        if not self.follow_symlinks:
            # gettarinfo uses lstat, so the link target is recorded but never
            # opened and its contents cannot leak into the archive.
            yield Entry(child_path, relative, arcname, "symlink", 0)
            return

        try:
            target = child_path.resolve(strict=True)
        except OSError:
            self.stats.note_skipped(relative, "broken symlink")
            return

        if not self._within_source(target):
            self.stats.note_skipped(relative, "symlink target escapes source directory")
            return

        try:
            target_stat = target.stat()
        except OSError as exc:
            self.stats.note_skipped(relative, f"broken symlink: {exc.strerror}")
            return

        inode = (target_stat.st_dev, target_stat.st_ino)
        if inode in self._seen_inodes:
            self.stats.note_skipped(relative, "symlink loop")
            return
        self._seen_inodes.add(inode)

        if stat.S_ISDIR(target_stat.st_mode):
            yield Entry(target, relative, arcname, "dir", 0)
            yield from self._walk(target, relative)
        elif stat.S_ISREG(target_stat.st_mode):
            yield Entry(target, relative, arcname, "file", target_stat.st_size)
        else:
            self.stats.note_skipped(
                relative, f"{describe_special(target_stat.st_mode)} (via symlink)"
            )


def _add_directory(tar: tarfile.TarFile, entry: Entry, stats: ArchiveStats) -> None:
    """Store a directory entry (and therefore its permissions and mtime)."""
    try:
        tarinfo = tar.gettarinfo(str(entry.path), entry.arcname)
    except OSError as exc:
        stats.note_skipped(entry.relative, f"could not read metadata: {exc.strerror}")
        return
    tar.addfile(tarinfo)
    stats.record_directory()


def _add_symlink(tar: tarfile.TarFile, entry: Entry, stats: ArchiveStats) -> None:
    """Store a symlink as a symlink; the target is never read."""
    try:
        tarinfo = tar.gettarinfo(str(entry.path), entry.arcname)
    except OSError as exc:
        stats.note_skipped(entry.relative, f"broken symlink: {exc.strerror}")
        return
    tar.addfile(tarinfo)
    stats.record_symlink()


def _add_file(tar: tarfile.TarFile, entry: Entry, stats: ArchiveStats) -> None:
    """Stream one regular file into *tar*.

    A failure to read this file is recorded and skipped, so one bad file never
    costs you the whole backup. A failure to *write* is re-raised, because that
    invalidates everything produced so far.

    One case needs care. ``logrotate`` truncates and recreates log files while
    they are being written, so the size read from the descriptor can be larger
    than the number of bytes still available. ``tarfile.copyfileobj`` then
    raises "unexpected end of data" *after* it has already written a header
    promising the old length, which would misalign every following member.
    That cannot be repaired mid-stream, so the whole archive has to be
    abandoned - but with a message that says what actually happened.
    """
    try:
        handle = _open_no_follow(entry.path)
    except OSError as exc:
        stats.note_skipped(entry.relative, f"unreadable: {exc.strerror or exc}")
        return

    with handle:
        try:
            file_stat = os.fstat(handle.fileno())
        except OSError as exc:
            stats.note_skipped(entry.relative, f"unreadable: {exc.strerror}")
            return
        if not stat.S_ISREG(file_stat.st_mode):
            stats.note_skipped(entry.relative, "no longer a regular file")
            return

        try:
            tarinfo = tar.gettarinfo(str(entry.path), entry.arcname)
        except OSError as exc:
            stats.note_skipped(entry.relative, f"could not read metadata: {exc.strerror}")
            return

        # Trust the descriptor we actually hold rather than a stat taken
        # earlier, and force a regular-file header in case the path was
        # replaced between the scan and the open.
        tarinfo.type = tarfile.REGTYPE
        tarinfo.size = file_stat.st_size

        try:
            tar.addfile(tarinfo, handle)
        except OSError as exc:
            if _file_shrank(entry.path, file_stat.st_size):
                stats.anomalies += 1
                stats.note_skipped(entry.relative, "file shrank while being archived")
                raise ArchiveError(
                    f"{entry.relative} shrank from {file_stat.st_size} to "
                    f"{_current_size(entry.path)} bytes while it was being archived, "
                    "which would have left the archive structurally damaged, so the "
                    "archive was discarded. This normally happens when logrotate "
                    "truncates a log mid-run; running again usually succeeds."
                ) from exc
            raise
        stats.record_file(file_stat.st_size)


def _current_size(path: Path) -> int:
    """Return a path's current size, or ``-1`` if it cannot be read."""
    try:
        return os.stat(path, follow_symlinks=False).st_size
    except OSError:
        return -1


def _file_shrank(path: Path, expected: int) -> bool:
    """Return True if *path* is now smaller than *expected* bytes.

    Asked of the filesystem rather than inferred from an exception message, so
    the check stays correct if CPython changes its wording.
    """
    return _current_size(path) < expected


def _add_manifest(tar: tarfile.TarFile, manifest: Manifest, root_name: str) -> None:
    """Append the manifest as the archive's final member."""
    payload = manifest.to_bytes()
    tarinfo = tarfile.TarInfo(name=f"{root_name}/{manifest.member_name}")
    tarinfo.size = len(payload)
    tarinfo.mode = 0o644
    tarinfo.mtime = int(manifest.created.timestamp())
    tarinfo.type = tarfile.REGTYPE
    tar.addfile(tarinfo, io.BytesIO(payload))


def _open_tar(archive_path: Path, fmt: CompressionFormat) -> tarfile.TarFile:
    """Open the destination archive with the requested format.

    ``tarfile.open`` is heavily overloaded on ``Literal`` mode strings, so a
    mode chosen at runtime never satisfies the stub. The format table is
    validated by :func:`log_archive.compression.get_format`, which is why the
    cast is sound here.
    """
    opener = cast("Callable[..., tarfile.TarFile]", tarfile.open)
    try:
        if fmt.level is None:
            return opener(archive_path, fmt.tar_mode, bufsize=_TAR_BUFSIZE)
        return opener(
            archive_path,
            fmt.tar_mode,
            bufsize=_TAR_BUFSIZE,
            **{fmt.level_kwarg: fmt.level},
        )
    except OSError as exc:
        raise _error_for(exc, archive_path) from exc


def build_archive(
    source: Path,
    archive_path: Path,
    rules: ExclusionRules,
    fmt: CompressionFormat,
    *,
    follow_symlinks: bool = False,
    skip_paths: Sequence[Path] = (),
    manifest: Manifest | None = None,
    stats: ArchiveStats | None = None,
) -> ArchiveStats:
    """Write *archive_path* from the tree at *source*.

    Args:
        source: Directory to archive. It is only ever read.
        archive_path: Destination. Must already be reserved by the caller so it
            cannot collide with an existing archive.
        rules: Exclusion rules.
        fmt: Compression format.
        follow_symlinks: See :class:`TreeScanner`.
        skip_paths: Resolved paths to keep out of the archive, typically the
            output directory.
        manifest: When given, appended as ``MANIFEST.txt``.
        stats: Reuse an existing stats object, as the dry run does.

    Returns:
        The populated :class:`ArchiveStats`.

    Raises:
        ArchiveError: the archive could not be written.
        PermissionDeniedError: the archive could not be opened for writing.
    """
    stats = stats if stats is not None else ArchiveStats()
    scanner = TreeScanner(
        source,
        rules,
        follow_symlinks=follow_symlinks,
        skip_paths=skip_paths,
        stats=stats,
    )
    tar = _open_tar(archive_path, fmt)
    try:
        with tar:
            for entry in scanner:
                if entry.kind == "dir":
                    _add_directory(tar, entry, stats)
                elif entry.kind == "symlink":
                    _add_symlink(tar, entry, stats)
                else:
                    _add_file(tar, entry, stats)
            if manifest is not None:
                manifest.file_count = stats.files_archived
                _add_manifest(tar, manifest, scanner.root_name)
    except OSError as exc:
        raise _error_for(exc, archive_path) from exc
    return stats


def plan_archive(
    source: Path,
    rules: ExclusionRules,
    *,
    follow_symlinks: bool = False,
    skip_paths: Sequence[Path] = (),
    stats: ArchiveStats | None = None,
) -> ArchiveStats:
    """Walk the tree exactly as :func:`build_archive` would, writing nothing.

    Dry run and real run share this walker, so the reported plan cannot drift
    away from what the tool actually does.
    """
    stats = stats if stats is not None else ArchiveStats()
    scanner = TreeScanner(
        source,
        rules,
        follow_symlinks=follow_symlinks,
        skip_paths=skip_paths,
        stats=stats,
    )
    for entry in scanner:
        if entry.kind == "dir":
            stats.record_directory()
        elif entry.kind == "symlink":
            stats.record_symlink()
        else:
            stats.record_file(entry.size)
    return stats


__all__ = [
    "COPY_BUFFER_SIZE",
    "MANIFEST_MEMBER",
    "Entry",
    "TreeScanner",
    "build_archive",
    "describe_special",
    "plan_archive",
]
