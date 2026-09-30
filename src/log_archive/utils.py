"""Shared helpers: exit codes, error types, formatting, and path safety.

Everything here is dependency free and deliberately small. The exit codes are
part of the public contract of the tool, because administrators write things
like ``log-archive /var/log && echo "backup ok"`` in cron jobs and need the
status to be meaningful.
"""

from __future__ import annotations

import enum
import errno
import os
import sys
from pathlib import Path
from typing import Final

#: Filename prefix for every archive this tool creates. Retention keys off
#: this prefix so unrelated ``.tar.gz`` files in the same directory are never
#: considered for deletion.
ARCHIVE_PREFIX: Final = "logs_archive_"

#: Filename of the activity log written beside the archives.
LOG_FILENAME: Final = "archive.log"

#: Name of the in-archive manifest.
MANIFEST_MEMBER: Final = "MANIFEST.txt"


class ExitCode(enum.IntEnum):
    """Stable process exit codes.

    These values are documented in the README and must not be renumbered.
    """

    SUCCESS = 0
    ERROR = 1
    USAGE = 2
    PERMISSION = 3
    ARCHIVE = 4
    VERIFICATION = 5
    CONFIG = 6
    CHECKSUM = 7
    CLEANUP = 8
    INTERRUPTED = 130


class LogArchiveError(Exception):
    """Base class for expected failures.

    Carries the exit code the CLI should return, so ``main`` never has to
    guess how serious a problem was.
    """

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UsageError(LogArchiveError):
    """The command line itself was wrong."""

    exit_code = ExitCode.USAGE


class PermissionDeniedError(LogArchiveError):
    """Access to the source or the output location was refused."""

    exit_code = ExitCode.PERMISSION


class ArchiveError(LogArchiveError):
    """Creating the archive failed, or it was left in an unusable state."""

    exit_code = ExitCode.ARCHIVE


class VerificationError(LogArchiveError):
    """An existing archive failed verification."""

    exit_code = ExitCode.VERIFICATION


class ConfigError(LogArchiveError):
    """A configuration file was unreadable or semantically invalid."""

    exit_code = ExitCode.CONFIG


class ChecksumError(LogArchiveError):
    """A checksum could not be written, read, or matched."""

    exit_code = ExitCode.CHECKSUM


class CleanupError(LogArchiveError):
    """Retention could not be applied."""

    exit_code = ExitCode.CLEANUP


def human_size(num_bytes: float) -> str:
    """Format a byte count as a human readable string.

    >>> human_size(0)
    '0.0 B'
    >>> human_size(1024)
    '1.0 KB'
    >>> human_size(245.7 * 1024 * 1024)
    '245.7 MB'
    """
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def human_duration(seconds: float) -> str:
    """Format a duration in seconds compactly.

    >>> human_duration(12.44)
    '12.4 seconds'
    >>> human_duration(75)
    '1m 15.0s'
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {seconds % 60:.0f}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60):02d}m"


def compression_ratio(original: int, compressed: int) -> float:
    """Return the percentage of space saved, clamped to ``[0.0, 100.0]``."""
    if original <= 0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - compressed / original) * 100.0))


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """Format a count with a pluralised noun.

    The plural form is passed explicitly because English is not regular:
    ``directory`` becomes ``directories``, not ``directorys``.

    >>> plural(1, "file")
    '1 file'
    >>> plural(2, "file")
    '2 files'
    >>> plural(13, "directory", "directories")
    '13 directories'
    """
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural_form or singular + 's'}"


def _supports_unicode(stream: object) -> bool:
    """Return True if *stream*'s encoding can represent the check mark."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        "✓".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def success_mark(stream: object = None) -> str:
    """Return a status marker that is safe on *stream*'s encoding.

    Under ``LC_ALL=C`` or a redirect into a non UTF-8 file, a literal check
    mark would raise ``UnicodeEncodeError`` and turn a successful backup into
    a crash. This degrades to plain ASCII instead.
    """
    target = stream if stream is not None else sys.stdout
    return "✓" if _supports_unicode(target) else "[ok]"


def failure_mark(stream: object = None) -> str:
    """Return a failure marker that is safe on *stream*'s encoding."""
    target = stream if stream is not None else sys.stdout
    return "✗" if _supports_unicode(target) else "[!!]"


def expand_path(value: str | Path) -> Path:
    """Expand ``~`` and environment variables in *value* and return a Path."""
    return Path(os.path.expandvars(str(value))).expanduser()


def remove_quietly(path: Path) -> bool:
    """Delete *path*, tolerating a missing file.

    Returns:
        ``True`` when this call removed the file.
    """
    try:
        path.unlink()
    except OSError:
        return False
    return True


def is_within_directory(target: Path, directory: Path) -> bool:
    """Return True if *target* resolves to a path inside *directory*.

    Used to keep symlink following and extraction confined to the tree the
    user actually asked about.
    """
    try:
        target.relative_to(directory)
    except ValueError:
        return False
    return True


def describe_oserror(exc: OSError) -> str:
    """Return a short human explanation of an OSError."""
    text = exc.strerror or str(exc)
    if exc.errno == errno.ENOSPC:
        return f"no space left on device ({text})"
    if exc.errno == errno.EDQUOT:
        return "disk quota exceeded"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return f"permission denied ({text})"
    if exc.errno == errno.EROFS:
        return "read-only filesystem"
    return text


def is_permission_error(exc: OSError) -> bool:
    """Return True if *exc* represents a permission problem."""
    return exc.errno in (errno.EACCES, errno.EPERM)


def raise_for_oserror(exc: OSError, path: Path, *, action: str) -> LogArchiveError:
    """Build the most specific :class:`LogArchiveError` for a failed *action*."""
    detail = describe_oserror(exc)
    if is_permission_error(exc):
        return PermissionDeniedError(f"Permission denied {action} {path}: {detail}")
    return ArchiveError(f"Could not {action} {path}: {detail}")


def reserve_unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Atomically create an unused ``<stem><suffix>`` path inside *directory*.

    The file is really created with ``O_EXCL`` before being returned, so two
    ``log-archive`` processes racing inside the same second can never write to
    the same archive - a failure mode that silently destroyed the previous
    backup in the original script. The returned file is empty and is meant to
    be overwritten.

    Raises:
        ArchiveError: if a thousand colliding names were produced.
    """
    base = directory / f"{stem}{suffix}"
    for attempt in range(1000):
        candidate = base if attempt == 0 else directory / f"{stem}_{attempt:02d}{suffix}"
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        else:
            os.close(descriptor)
            return candidate
    raise ArchiveError(f"Could not find an unused archive name in {directory} after 1000 attempts.")
