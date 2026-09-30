"""Tests for archive retention and cleanup."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from log_archive.cli import ExitCode
from log_archive.retention import (
    apply_cleanup,
    discover,
    is_owned_archive,
    plan_cleanup,
    select_for_deletion,
)
from log_archive.utils import ARCHIVE_PREFIX


def _make(directory: Path, stamp: datetime, suffix: str = ".tar.gz", tie: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ARCHIVE_PREFIX}{stamp:%Y%m%d_%H%M%S}{tie}{suffix}"
    path.write_bytes(b"payload")
    return path


def test_recognises_its_own_naming_scheme(tmp_path: Path) -> None:
    assert is_owned_archive(Path("logs_archive_20260930_065300.tar.gz"))
    assert is_owned_archive(Path("logs_archive_20260930_065300_01.tar.gz"))
    assert is_owned_archive(Path("logs_archive_20260930_065300.tar.xz"))
    # Anything else is not ours.
    assert not is_owned_archive(Path("backup.tar.gz"))
    assert not is_owned_archive(Path("logs_archive.tar.gz"))
    assert not is_owned_archive(Path("logs_archive_2026093_065300.tar.gz"))
    assert not is_owned_archive(Path("logs_archive_20260930_065300.tar.gz.bak"))
    assert not is_owned_archive(Path("my-logs_archive_20260930_065300.tar.gz"))
    assert not is_owned_archive(Path("logs_archive_20261301_065300.tar.gz"))


def test_discovers_newest_first(tmp_path: Path) -> None:
    base = datetime(2026, 9, 30, 6, 0, 0)
    old = _make(tmp_path, base)
    new = _make(tmp_path, base + timedelta(hours=2))
    mid = _make(tmp_path, base + timedelta(hours=1))
    assert [item.path for item in discover(tmp_path)] == [new, mid, old]


def test_collision_suffix_breaks_ties(tmp_path: Path) -> None:
    stamp = datetime(2026, 9, 30, 6, 53, 0)
    first = _make(tmp_path, stamp, tie="")
    second = _make(tmp_path, stamp, tie="_01")
    order = [item.path.name for item in discover(tmp_path)]
    assert order.index(second.name) < order.index(first.name)


def test_cleanup_keeps_the_newest(tmp_path: Path) -> None:
    base = datetime(2026, 9, 30, 6, 0, 0)
    for index in range(5):
        _make(tmp_path, base + timedelta(minutes=index))
    plan = plan_cleanup(tmp_path, keep=2)
    assert len(plan.found) == 5
    assert len(plan.keep) == 2
    assert len(plan.remove) == 3


def test_cleanup_never_touches_foreign_files(tmp_path: Path) -> None:
    """The most important retention property.

    Retention is keyed off this tool's own filename pattern, so unrelated files
    in the same directory are invisible to it.
    """
    base = datetime(2026, 9, 30, 6, 0, 0)
    for index in range(3):
        _make(tmp_path, base + timedelta(minutes=index))

    precious = tmp_path / "my-important-backup.tar.gz"
    precious.write_text("do not delete me")
    other = tmp_path / "notes.txt"
    other.write_text("keep")
    near_miss = tmp_path / f"{ARCHIVE_PREFIX}20260930_065300.tar.gz.bak"
    near_miss.write_text("also keep")

    deleted, error = apply_cleanup(tmp_path, keep=1)
    assert error is None
    assert len(deleted) == 2
    assert precious.read_text() == "do not delete me"
    assert other.exists()
    assert near_miss.exists()


def test_cleanup_removes_checksum_sidecars(tmp_path: Path) -> None:
    base = datetime(2026, 9, 30, 6, 0, 0)
    for index in range(3):
        archive = _make(tmp_path, base + timedelta(minutes=index))
        archive.with_name(archive.name + ".sha256").write_text("deadbeef  x\n")
    deleted, error = apply_cleanup(tmp_path, keep=1)
    assert error is None
    # Two archives plus their two sidecars; the newest pair is kept.
    assert len(deleted) == 4
    assert len(list(tmp_path.glob("*.tar.gz"))) == 1
    assert len(list(tmp_path.glob("*.sha256"))) == 1


def test_cleanup_keeps_the_newest_checksum(tmp_path: Path) -> None:
    base = datetime(2026, 9, 30, 6, 0, 0)
    for index in range(3):
        archive = _make(tmp_path, base + timedelta(minutes=index))
        archive.with_name(archive.name + ".sha256").write_text("x\n")
    apply_cleanup(tmp_path, keep=2)
    assert len(list(tmp_path.glob("*.sha256"))) == 2


def test_select_for_deletion_rejects_negative_keep(tmp_path: Path) -> None:
    archives = discover(tmp_path)
    try:
        select_for_deletion(archives, -1)
    except ValueError as exc:
        assert "zero or greater" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_directories_named_like_archives_are_ignored(tmp_path: Path) -> None:
    decoy = tmp_path / f"{ARCHIVE_PREFIX}20260930_065300.tar.gz"
    decoy.mkdir()
    assert discover(tmp_path) == []


def test_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert discover(tmp_path / "absent") == []


def test_cleanup_command_dry_run_changes_nothing(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    base = datetime(2026, 9, 30, 6, 0, 0)
    for index in range(3):
        _make(out_dir, base + timedelta(minutes=index))
    before = sorted(p.name for p in out_dir.iterdir())

    code, out, _err = run_cli("cleanup", "-o", str(out_dir), "--keep", "1", "--dry-run")
    assert code == 0
    assert "Cleanup preview" in out
    assert "No files were deleted." in out
    assert sorted(p.name for p in out_dir.iterdir()) == before


def test_cleanup_command_removes_old_archives(out_dir: Path, run_cli) -> None:
    base = datetime(2026, 9, 30, 6, 0, 0)
    for index in range(4):
        _make(out_dir, base + timedelta(minutes=index))
    code, _out, _err = run_cli("cleanup", "-o", str(out_dir), "--keep", "2")
    assert code == int(ExitCode.SUCCESS)
    assert len(discover(out_dir)) == 2


def test_keep_flag_on_archive_applies_retention(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    base = datetime(2026, 9, 30, 6, 0, 0)
    for index in range(3):
        _make(out_dir, base + timedelta(minutes=index))
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--keep", "2")[0] == 0
    assert len(discover(out_dir)) == 2


def test_negative_keep_is_a_usage_error(out_dir: Path, run_cli) -> None:
    code, _out, err = run_cli("cleanup", "-o", str(out_dir), "--keep", "-1")
    assert code == int(ExitCode.USAGE)
    assert "zero or greater" in err


def test_unreadable_parent_is_reported(tmp_path: Path) -> None:
    from log_archive.retention import discover as _discover
    from log_archive.utils import CleanupError

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        if os.geteuid() == 0:
            return
        try:
            _discover(locked)
        except CleanupError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CleanupError")
    finally:
        locked.chmod(0o700)
