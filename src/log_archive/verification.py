"""Archive verification and tar member safety checks.

Verification deliberately reads every member body rather than just walking the
headers. A truncated gzip stream or a header whose size disagrees with the
data that follows is exactly the kind of damage that makes a backup useless
while still looking like a normal file, and it can only be caught by reading
the bytes.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final, cast

from .compression import zstd_available
from .utils import human_size

#: Slice size used while reading members back.
_CHUNK_SIZE: Final = 1024 * 1024

#: Cap on reported extraction hazards, so a hostile archive cannot flood output.
_UNSAFE_SAMPLE_LIMIT: Final = 20

#: Errors a codec may raise while decoding.
_STREAM_ERRORS: Final[tuple[type[BaseException], ...]] = (
    OSError,
    EOFError,
    ValueError,
    lzma.LZMAError,
)

#: A callable that opens a compression stream for reading.
_StreamOpener = Callable[..., IO[bytes]]


def _zstd_opener() -> _StreamOpener | None:
    """Return an ``open``-compatible zstd reader, or None if unsupported.

    Delegates the capability check to :mod:`log_archive.compression` so there
    is a single place that knows ``compression.zstd`` only exists on CPython
    3.14+, and a single place that has to guard the parent-package import.
    """
    if not zstd_available():
        return None
    from compression import zstd

    return cast("_StreamOpener", zstd.open)


def detect_stream(path: Path) -> tuple[str, _StreamOpener | None]:
    """Identify an archive's compression from its magic bytes.

    Returns:
        ``(label, opener)`` where *opener* is ``None`` for an uncompressed
        tar. Detecting by content rather than by filename means a renamed
        archive is still validated correctly.
    """
    try:
        with path.open("rb") as handle:
            magic = handle.read(6)
            handle.seek(257)
            ustar = handle.read(5)
    except OSError:
        return "unknown", None
    if magic.startswith(b"\x1f\x8b"):
        return "gzip", cast("_StreamOpener", gzip.open)
    if magic.startswith(b"BZh"):
        return "bzip2", cast("_StreamOpener", bz2.open)
    if magic.startswith(b"\xfd7zXZ\x00"):
        return "xz", cast("_StreamOpener", lzma.open)
    zstd_open = _zstd_opener()
    if zstd_open is not None and magic.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd", zstd_open
    if ustar.startswith(b"ustar"):
        # A plain tar has no magic at offset 0; its signature lives in the
        # first header block, 257 bytes in.
        return "none", None
    if not magic:
        return "empty", None
    return "unknown", None


def validate_compression_stream(path: Path) -> tuple[str, str | None]:
    """Decode the whole compression stream to force its integrity check.

    This is the step that catches silent corruption. ``tarfile`` stops at the
    two zero blocks that mark the end of a tar stream, so walking members alone
    never reaches the gzip CRC32 trailer. A file whose bytes were altered in
    the middle therefore looks fine to a member walk but fails here.

    Reading to EOF also catches truncation, which surfaces as
    ``EOFError: Compressed file ended before the end-of-stream marker``.

    Returns:
        ``(label, problem)``; *problem* is ``None`` when the stream is sound.
    """
    label, opener = detect_stream(path)
    if opener is None:
        return label, None
    try:
        with opener(path, "rb") as handle:
            while handle.read(_CHUNK_SIZE):
                pass
    except _STREAM_ERRORS as exc:
        return label, f"{label} stream is corrupt: {type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - codec specific safety net
        return label, f"{label} stream is corrupt: {type(exc).__name__}: {exc}"
    return label, None


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of re-reading an archive from disk.

    Attributes:
        ok: Integrity verdict. ``True`` only when every member was readable.
        problems: Integrity failures. Any entry here means ``ok`` is ``False``.
        unsafe_members: Members that would be dangerous to *extract*. Advisory
            only: an absolute symlink target is routine in ``/var/log`` and
            does not make an archive corrupt.
        stream: Detected compression format, e.g. ``gzip`` or ``none``.
    """

    ok: bool
    members: int = 0
    files: int = 0
    uncompressed_size: int = 0
    problems: list[str] = field(default_factory=list)
    unsafe_members: list[str] = field(default_factory=list)
    stream: str = "unknown"


def is_safe_member(member: tarfile.TarInfo) -> bool:
    """Return True if extracting *member* could not escape the destination.

    This tool never extracts archives, so this is an advisory check used to
    warn about a hostile archive - it is deliberately **not** part of integrity
    verification. An absolute symlink target is completely normal in
    ``/var/log`` and must never cause a good archive to be rejected.

    Members that would be dangerous to extract are:

    * absolute paths (``/etc/passwd``)
    * paths containing a ``..`` component
    * symlinks and hard links whose target is absolute or climbs out
    * device nodes and other entries that are neither regular files nor
      directories
    """
    name = member.name
    if name.startswith(("/", "\\")):
        return False
    if ".." in Path(name).parts:
        return False
    if member.issym() or member.islnk():
        target = member.linkname
        if target.startswith(("/", "\\")):
            return False
        return ".." not in Path(target).parts
    return member.isreg() or member.isdir()


def unsafe_members(archive_path: Path) -> list[str]:
    """Return the names of members that would be unsafe to extract."""
    found: list[str] = []
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            for member in tar:
                if not is_safe_member(member):
                    found.append(member.name)
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return found
    return found


def iter_members(archive_path: Path) -> Iterator[tarfile.TarInfo]:
    """Yield the members of *archive_path*, raising on a malformed archive."""
    with tarfile.open(archive_path, "r:*") as tar:
        yield from tar


def count_members(archive_path: Path) -> tuple[int, int, int]:
    """Return ``(members, files, uncompressed_bytes)`` from the headers alone.

    Cheap: no member body is read. Used by ``info``, where speed matters more
    than proving integrity.
    """
    members = files = size = 0
    with tarfile.open(archive_path, "r:*") as tar:
        for member in tar:
            members += 1
            if member.isfile():
                files += 1
                size += member.size
    return members, files, size


def verify_archive(archive_path: Path) -> VerifyResult:
    """Reopen *archive_path* and check it end to end.

    Two independent passes, because they catch different failures:

    1. The whole compression stream is decoded, forcing the codec's own
       integrity check (gzip CRC32 and length) and catching truncation.
    2. Every tar member is walked and every file body read, proving the
       framing is consistent and the data is really there.

    A member walk alone is not sufficient: ``tarfile`` stops at the end of
    archive marker and never validates the trailer, so silent corruption in the
    middle of a member would otherwise pass unnoticed.

    Extraction hazards are collected separately in ``unsafe_members`` so a
    normal ``/var/log`` archive is not rejected merely for containing absolute
    symlinks.

    Memory use stays constant: data is read in chunks, so this is safe on a
    multi gigabyte archive.
    """
    problems: list[str] = []
    unsafe: list[str] = []
    members = files = size = 0

    label, stream_problem = validate_compression_stream(archive_path)
    if stream_problem is not None:
        problems.append(stream_problem)
        # The stream is already known bad; walking members would only produce
        # a second, noisier error.
        return VerifyResult(ok=False, problems=problems, stream=label)

    try:
        with tarfile.open(archive_path, "r:*") as tar:
            for member in tar:
                members += 1
                if member.isfile():
                    files += 1
                    size += member.size
                if not is_safe_member(member) and len(unsafe) < _UNSAFE_SAMPLE_LIMIT:
                    unsafe.append(member.name)
                if not member.isfile():
                    continue
                stream = tar.extractfile(member)
                if stream is None:
                    problems.append(f"cannot read data for {member.name}")
                    continue
                with stream:
                    while stream.read(_CHUNK_SIZE):
                        pass
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        problems.append(f"tar structure is damaged: {type(exc).__name__}: {exc}")

    if members == 0 and not problems:
        problems.append("archive contains no members")
    return VerifyResult(
        ok=not problems,
        members=members,
        files=files,
        uncompressed_size=size,
        problems=problems,
        unsafe_members=unsafe,
        stream=label,
    )


def describe_format(archive_path: Path) -> str:
    """Guess an archive's format from its name, e.g. ``tar.gz``."""
    name = archive_path.name
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar"):
        if name.endswith(suffix):
            return suffix[5:]
    return "unknown"


def describe_size(archive_path: Path) -> str:
    """Return a human readable size for *archive_path*."""
    try:
        return human_size(archive_path.stat().st_size)
    except OSError:
        return "unknown"
