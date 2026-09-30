"""Tests for the archive engine itself."""

from __future__ import annotations

import os
import socket
import tarfile
from pathlib import Path

import pytest

from helpers import member_names
from log_archive.archive import build_archive, describe_special, plan_archive
from log_archive.compression import get_format
from log_archive.exclusions import ExclusionRules
from log_archive.manifest import Manifest
from log_archive.utils import ArchiveError

GZIP = get_format("gzip")


def _make_unix_socket(path: Path) -> None:
    """Bind a unix domain socket and close it, leaving the socket node on disk."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()


def _build(source: Path, dest: Path, **kwargs: object):
    """Archive *source* into *dest* with default-friendly options."""
    return build_archive(
        source,
        dest,
        ExclusionRules.build(use_defaults=True),
        GZIP,
        **kwargs,  # type: ignore[arg-type]
    )


def test_archives_regular_files_and_directories(readable_tree: Path, tmp_path: Path) -> None:
    stats = _build(readable_tree, tmp_path / "out.tar.gz")
    names = member_names(tmp_path / "out.tar.gz")

    assert stats.files_archived == 2
    assert stats.original_size == len("one\n") + len("two\n")
    assert f"{readable_tree.name}/one.log" in names
    assert f"{readable_tree.name}/sub/two.log" in names
    assert readable_tree.name in names


def test_preserves_file_contents(readable_tree: Path, tmp_path: Path) -> None:
    archive = tmp_path / "out.tar.gz"
    _build(readable_tree, archive)
    with tarfile.open(archive) as tar:
        handle = tar.extractfile(f"{readable_tree.name}/one.log")
        assert handle is not None
        with handle:
            assert handle.read() == b"one\n"


def test_does_not_modify_the_source(readable_tree: Path, tmp_path: Path) -> None:
    before = {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(readable_tree.rglob("*"))
        if path.is_file()
    }
    _build(readable_tree, tmp_path / "out.tar.gz")
    after = {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(readable_tree.rglob("*"))
        if path.is_file()
    }
    assert before == after


def test_empty_directory_produces_valid_archive(empty_dir: Path, tmp_path: Path) -> None:
    archive = tmp_path / "out.tar.gz"
    stats = _build(empty_dir, archive)
    assert stats.files_archived == 0
    assert member_names(archive) == {empty_dir.name}


def test_manifest_is_included_and_accurate(readable_tree: Path, tmp_path: Path) -> None:
    from datetime import datetime

    archive = tmp_path / "out.tar.gz"
    manifest = Manifest.collect(
        created=datetime(2026, 9, 30, 6, 53, 0),
        source=readable_tree,
        compression="gzip",
        excludes=["*.sock"],
    )
    build_archive(readable_tree, archive, ExclusionRules.build(), GZIP, manifest=manifest)
    assert manifest.file_count == 2

    with tarfile.open(archive) as tar:
        handle = tar.extractfile(f"{readable_tree.name}/MANIFEST.txt")
        assert handle is not None
        with handle:
            body = handle.read().decode()
    assert "Files:       2" in body
    assert "Compression: gzip" in body
    assert str(readable_tree) in body


def test_manifest_does_not_touch_the_source(readable_tree: Path, tmp_path: Path) -> None:
    from datetime import datetime

    manifest = Manifest.collect(
        created=datetime(2026, 1, 1), source=readable_tree, compression="gzip", excludes=()
    )
    _build(readable_tree, tmp_path / "out.tar.gz", manifest=manifest)
    assert not (readable_tree / "MANIFEST.txt").exists()


def test_unreadable_file_is_skipped_not_fatal(readable_tree: Path, tmp_path: Path) -> None:
    """A file we cannot read must not cost us the whole backup.

    This is the single most important behavioural fix over the original tool,
    which aborted the run and then deleted the partial archive.
    """
    blocked = readable_tree / "blocked.log"
    blocked.write_text("secret\n")
    blocked.chmod(0o000)
    try:
        stats = _build(readable_tree, tmp_path / "out.tar.gz")
    finally:
        blocked.chmod(0o600)

    assert stats.files_archived == 2
    assert stats.files_skipped == 1
    assert any("unreadable" in entry.reason for entry in stats.skipped_entries)
    names = member_names(tmp_path / "out.tar.gz")
    assert f"{readable_tree.name}/one.log" in names


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode 000")
def test_unreadable_subdirectory_is_skipped(readable_tree: Path, tmp_path: Path) -> None:
    locked = readable_tree / "locked"
    locked.mkdir()
    (locked / "hidden.log").write_text("data\n")
    locked.chmod(0o000)
    try:
        stats = _build(readable_tree, tmp_path / "out.tar.gz")
    finally:
        locked.chmod(0o700)

    assert stats.files_archived == 2
    assert stats.files_skipped >= 1


def test_special_files_are_skipped(readable_tree: Path, tmp_path: Path) -> None:
    """FIFOs and sockets carry no log data and must never be read."""
    _make_unix_socket(readable_tree / "queue.sock")
    os.mkfifo(readable_tree / "pipe.fifo")
    (readable_tree / "dev-null").write_text("not a device, but a regular file\n")

    # No default excludes, so the socket is classified as a socket rather than
    # being filtered out by the built in *.sock rule.
    stats = build_archive(
        readable_tree, tmp_path / "out.tar.gz", ExclusionRules.build(use_defaults=False), GZIP
    )
    reasons = {entry.reason for entry in stats.skipped_entries}
    assert "FIFO" in reasons
    assert "socket" in reasons

    names = member_names(tmp_path / "out.tar.gz")
    assert not any(name.endswith(".fifo") for name in names)
    assert not any(name.endswith(".sock") for name in names)
    assert f"{readable_tree.name}/dev-null" in names


def test_default_excludes_filter_the_socket_and_journal(log_tree: Path, tmp_path: Path) -> None:
    archive = tmp_path / "out.tar.gz"
    _build(log_tree, archive)
    names = member_names(archive)
    assert not any("journal" in name for name in names)
    assert not any(name.endswith(".sock") for name in names)
    assert not any(name.endswith(".pid") for name in names)
    assert not any(name.endswith(".gz") for name in names)
    assert f"{log_tree.name}/syslog" in names


def test_broken_symlink_is_stored_as_a_link(log_tree: Path, tmp_path: Path) -> None:
    archive = tmp_path / "out.tar.gz"
    _build(log_tree, archive)
    with tarfile.open(archive) as tar:
        member = tar.getmember(f"{log_tree.name}/broken.link")
    assert member.issym()
    assert member.linkname == "nowhere"


def test_symlink_outside_source_is_not_read(log_tree: Path, tmp_path: Path) -> None:
    """A symlink to /etc/passwd must be stored as a link, never dereferenced."""
    archive = tmp_path / "out.tar.gz"
    _build(log_tree, archive)
    with tarfile.open(archive) as tar:
        member = tar.getmember(f"{log_tree.name}/escape.link")
    assert member.issym()
    assert member.size == 0
    assert member.linkname == "/etc/passwd"


def test_follow_symlinks_still_refuses_to_escape(log_tree: Path, tmp_path: Path) -> None:
    stats = _build(log_tree, tmp_path / "out.tar.gz", follow_symlinks=True)
    reasons = {entry.reason for entry in stats.skipped_entries}
    assert "symlink target escapes source directory" in reasons
    assert "broken symlink" in reasons


def test_output_directory_inside_source_is_pruned(tmp_path: Path) -> None:
    """An archive must never contain itself or a previous run's archive."""
    source = tmp_path / "logs"
    (source / "archives").mkdir(parents=True)
    (source / "app.log").write_text("data\n")
    (source / "archives" / "logs_archive_20200101_000000.tar.gz").write_text("old")

    dest = source / "archives" / "new.tar.gz"
    build_archive(
        source,
        dest,
        ExclusionRules.build(),
        GZIP,
        skip_paths=[(source / "archives").resolve()],
    )
    names = member_names(dest)
    assert not any("archives" in name for name in names)


def test_symlink_loop_terminates(tmp_path: Path) -> None:
    source = tmp_path / "loop"
    source.mkdir()
    (source / "a.log").write_text("a\n")
    (source / "self").symlink_to(source)

    stats = _build(source, tmp_path / "out.tar.gz", follow_symlinks=True)
    assert stats.files_archived == 1
    assert any("loop" in entry.reason for entry in stats.skipped_entries)


def test_plan_matches_a_real_run(readable_tree: Path, tmp_path: Path) -> None:
    rules = ExclusionRules.build()
    plan = plan_archive(readable_tree, rules)
    stats = _build(readable_tree, tmp_path / "out.tar.gz")
    assert plan.files_archived == stats.files_archived
    assert plan.original_size == stats.original_size
    assert plan.directories_archived == stats.directories_archived


def test_each_compression_format_round_trips(readable_tree: Path, tmp_path: Path) -> None:
    from log_archive.compression import FORMATS

    for name in FORMATS:
        fmt = get_format(name)
        archive = tmp_path / f"out{fmt.extension}"
        build_archive(readable_tree, archive, ExclusionRules.build(), fmt)
        assert archive.is_file()
        with tarfile.open(archive, "r:*") as tar:
            assert f"{readable_tree.name}/one.log" in {m.name for m in tar.getmembers()}


def test_disk_full_raises_a_clear_error(
    readable_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC mid-write must be reported as a disk space problem."""
    import errno

    import log_archive.archive as archive_module

    def raising_addfile(tar: tarfile.TarFile, tarinfo: tarfile.TarInfo, fileobj=None) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(archive_module.tarfile.TarFile, "addfile", raising_addfile)
    with pytest.raises(ArchiveError, match="disk space"):
        _build(readable_tree, tmp_path / "out.tar.gz")


def test_permission_error_on_output_reports_permission(
    readable_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import log_archive.archive as archive_module
    from log_archive.utils import PermissionDeniedError

    def raising_open(*args: object, **kwargs: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(archive_module.tarfile, "open", raising_open)
    with pytest.raises(PermissionDeniedError):
        _build(readable_tree, tmp_path / "out.tar.gz")


def test_describe_special_names_modes() -> None:
    import stat as stat_module

    assert describe_special(stat_module.S_IFIFO) == "FIFO"
    assert describe_special(stat_module.S_IFSOCK) == "socket"
    assert describe_special(stat_module.S_IFCHR) == "character device"
    assert describe_special(stat_module.S_IFBLK) == "block device"


def test_shrinking_file_is_detected_and_archive_is_discarded(
    readable_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log that shrinks mid-read would misalign the whole tar stream.

    The engine cannot repair the framing once a header has been written, so it
    must abort with an explanation instead of writing a plausible looking but
    structurally broken archive.
    """
    import log_archive.archive as archive_module

    real_fstat = os.fstat

    def inflated_fstat(fd: int) -> os.stat_result:
        real = real_fstat(fd)
        values = list(real)
        values[6] = real.st_size + 4096  # claim more bytes than the file holds
        return os.stat_result(values)

    monkeypatch.setattr(archive_module.os, "fstat", inflated_fstat)
    with pytest.raises(ArchiveError, match="shrank"):
        _build(readable_tree, tmp_path / "out.tar.gz")


def test_large_file_is_streamed_not_buffered(
    readable_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No single read from a source file may exceed a sane slice size."""
    big = readable_tree / "big.log"
    with big.open("wb") as handle:
        handle.write(b"x" * (5 * 1024 * 1024))
    (readable_tree / "small.log").write_text("s\n")

    sizes: list[int] = []
    import log_archive.archive as archive_module

    original = archive_module._open_no_follow

    def tracking(path: Path) -> object:
        handle = original(path)
        real_read = handle.read

        def read(size: int = -1) -> bytes:
            data = real_read(size)
            sizes.append(len(data))
            return data

        handle.read = read  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(archive_module, "_open_no_follow", tracking)
    _build(readable_tree, tmp_path / "out.tar.gz")
    assert max(sizes) <= archive_module.COPY_BUFFER_SIZE
