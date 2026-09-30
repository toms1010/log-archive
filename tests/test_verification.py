"""Tests for archive verification and integrity detection."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from log_archive.archive import build_archive
from log_archive.cli import ExitCode
from log_archive.compression import get_format
from log_archive.exclusions import ExclusionRules
from log_archive.utils import reserve_unique_path
from log_archive.verification import (
    count_members,
    detect_stream,
    is_safe_member,
    validate_compression_stream,
    verify_archive,
)

GZIP = get_format("gzip")


def _make_archive(source: Path, out_dir: Path, **kwargs: object) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = reserve_unique_path(out_dir, "logs_archive_20260930_065300", ".tar.gz")
    build_archive(source, dest, ExclusionRules.build(), GZIP, **kwargs)  # type: ignore[arg-type]
    return dest


def test_good_archive_verifies(readable_tree: Path, out_dir: Path) -> None:
    archive = _make_archive(readable_tree, out_dir)
    result = verify_archive(archive)
    assert result.ok
    assert result.problems == []
    # root, sub/, one.log, sub/two.log
    assert result.members == 4
    assert result.files == 2
    assert result.stream == "gzip"


def test_truncated_archive_is_detected(readable_tree: Path, out_dir: Path) -> None:
    """Truncation is the failure that matters most for a backup tool."""
    archive = _make_archive(readable_tree, out_dir)
    with archive.open("r+b") as handle:
        handle.truncate(archive.stat().st_size - 64)
    result = verify_archive(archive)
    assert not result.ok
    assert any("EOFError" in problem or "corrupt" in problem for problem in result.problems)


def test_single_bit_flip_is_detected(readable_tree: Path, out_dir: Path) -> None:
    """A member walk alone misses this: tarfile stops before the gzip CRC."""
    archive = _make_archive(readable_tree, out_dir)
    size = archive.stat().st_size
    with archive.open("r+b") as handle:
        handle.seek(size // 2)
        handle.write(b"\xff\xff\xff\xff")
    result = verify_archive(archive)
    assert not result.ok


def test_garbage_file_is_rejected(tmp_path: Path) -> None:
    junk = tmp_path / "logs_archive_20260930_065300.tar.gz"
    junk.write_bytes(b"this is not an archive at all" * 100)
    result = verify_archive(junk)
    assert not result.ok


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "logs_archive_20260930_065300.tar.gz"
    empty.write_bytes(b"")
    result = verify_archive(empty)
    assert not result.ok


def test_appended_garbage_is_reported_as_corruption(readable_tree: Path, out_dir: Path) -> None:
    archive = _make_archive(readable_tree, out_dir)
    with archive.open("ab") as handle:
        handle.write(b"not gzip data at all")
    result = verify_archive(archive)
    assert not result.ok


def test_zero_padding_appended_to_a_valid_archive_is_tolerated(
    readable_tree: Path, out_dir: Path
) -> None:
    """Documents a deliberate limitation.

    CPython's gzip decoder silently ignores trailing zero padding, and its
    read-ahead buffering makes the decoder's file position useless for
    detecting leftover bytes. Rejecting that case would risk false positives on
    legitimate multi-member gzip files, so the padding is accepted and the
    behaviour is called out in the README instead.
    """
    archive = _make_archive(readable_tree, out_dir)
    with archive.open("ab") as handle:
        handle.write(b"\x00" * 32)
    result = verify_archive(archive)
    assert result.ok, result.problems
    assert result.members == 4


def test_detect_stream_identifies_formats(readable_tree: Path, tmp_path: Path) -> None:
    from log_archive.compression import FORMATS

    for name, fmt in FORMATS.items():
        dest = reserve_unique_path(tmp_path, f"probe_{name}", fmt.extension)
        build_archive(readable_tree, dest, ExclusionRules.build(use_defaults=False), fmt)
        label, _opener = detect_stream(dest)
        # Detection is by container magic, so the gzip levels all read as
        # "gzip" regardless of which level produced them.
        expected = "gzip" if name.startswith("gzip") else name
        assert label == expected, f"{name} detected as {label}"


def test_validate_stream_passes_for_good_archive(readable_tree: Path, out_dir: Path) -> None:
    archive = _make_archive(readable_tree, out_dir)
    label, problem = validate_compression_stream(archive)
    assert label == "gzip"
    assert problem is None


def test_count_members_reads_headers_only(readable_tree: Path, out_dir: Path) -> None:
    archive = _make_archive(readable_tree, out_dir)
    members, files, size = count_members(archive)
    assert members == 4
    assert files == 2
    assert size == len("one\n") + len("two\n")


def test_absolute_symlinks_are_advisory_not_a_failure(readable_tree: Path, out_dir: Path) -> None:
    """An absolute symlink target is normal in /var/log.

    It must be reported as an extraction hazard without causing a perfectly
    good archive to be rejected - which is what an earlier version did, and
    which deleted working backups.
    """
    (readable_tree / "escape").symlink_to("/etc/shadow")
    archive = _make_archive(readable_tree, out_dir)
    result = verify_archive(archive)
    assert result.ok, result.problems
    assert any(name.endswith("escape") for name in result.unsafe_members)


def test_is_safe_member_rejects_traversal(tmp_path: Path) -> None:
    def member(name: str, **kwargs: object) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        for key, value in kwargs.items():
            setattr(info, key, value)
        return info

    assert is_safe_member(member("logs/app.log", type=tarfile.REGTYPE, size=1))
    assert is_safe_member(member("logs", type=tarfile.DIRTYPE))
    assert not is_safe_member(member("/etc/passwd", type=tarfile.REGTYPE))
    assert not is_safe_member(member("../../etc/passwd", type=tarfile.REGTYPE))
    assert not is_safe_member(member("logs/../../etc/passwd", type=tarfile.REGTYPE))
    assert not is_safe_member(member("logs/link", type=tarfile.SYMTYPE, linkname="/etc/shadow"))
    assert not is_safe_member(
        member("logs/link", type=tarfile.SYMTYPE, linkname="../../etc/shadow")
    )
    assert is_safe_member(member("logs/link", type=tarfile.SYMTYPE, linkname="app.log"))
    assert not is_safe_member(member("logs/dev", type=tarfile.CHRTYPE))


def test_verify_command_exit_codes(readable_tree: Path, out_dir: Path, run_cli) -> None:
    archive = _make_archive(readable_tree, out_dir)
    code, out, _err = run_cli("verify", str(archive))
    assert code == int(ExitCode.SUCCESS)
    assert "tar structure valid" in out

    with archive.open("r+b") as handle:
        handle.truncate(10)
    code, _out, err = run_cli("verify", str(archive))
    assert code == int(ExitCode.VERIFICATION)
    assert "Error" in err


def test_verify_on_missing_file_is_usage_error(out_dir: Path, run_cli) -> None:
    code, _out, err = run_cli("verify", str(out_dir / "absent.tar.gz"))
    assert code == int(ExitCode.USAGE)
    assert "is not a file" in err


def test_archive_verify_flag_removes_a_bad_archive(
    readable_tree: Path, out_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed --verify must not leave an untrustworthy archive behind."""
    import log_archive.cli as cli_module
    from log_archive.verification import VerifyResult

    monkeypatch.setattr(
        cli_module,
        "verify_archive",
        lambda path: VerifyResult(ok=False, problems=["simulated corruption"]),
    )
    code, _out, err = run_cli(str(readable_tree), "-o", str(out_dir), "--verify")
    assert code == int(ExitCode.VERIFICATION)
    assert not list(out_dir.glob("*.tar.gz"))
    assert "removed" in err


def test_verify_failure_also_removes_the_checksum(
    readable_tree: Path, out_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    import log_archive.cli as cli_module
    from log_archive.verification import VerifyResult

    monkeypatch.setattr(
        cli_module,
        "verify_archive",
        lambda path: VerifyResult(ok=False, problems=["simulated corruption"]),
    )
    run_cli(str(readable_tree), "-o", str(out_dir), "--verify", "--checksum")
    assert not list(out_dir.glob("*.tar.gz*"))
