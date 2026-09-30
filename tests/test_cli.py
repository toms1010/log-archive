"""Tests for the command line interface, including backwards compatibility."""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from helpers import find_archive, member_names
from log_archive import __version__
from log_archive.cli import (
    ExitCode,
    _first_positional,
    default_output_dir,
    main,
    normalise_argv,
)

# --------------------------------------------------------------------------
# Backwards compatible argument handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["/var/log"], ["archive", "/var/log"]),
        (["/var/log", "-q"], ["archive", "/var/log", "-q"]),
        (["-o", "/tmp/out", "/var/log"], ["archive", "-o", "/tmp/out", "/var/log"]),
        (["--output=/tmp/out", "/var/log"], ["archive", "--output=/tmp/out", "/var/log"]),
        (["-x", "*.old", "/var/log"], ["archive", "-x", "*.old", "/var/log"]),
        (["-x*.old", "/var/log"], ["archive", "-x*.old", "/var/log"]),
        (["--keep", "5", "/var/log"], ["archive", "--keep", "5", "/var/log"]),
        (["-X", "/var/log"], ["archive", "-X", "/var/log"]),
        (["--dry-run", "/var/log"], ["archive", "--dry-run", "/var/log"]),
        # Explicit subcommands are left alone.
        (["archive", "/var/log"], ["archive", "/var/log"]),
        (["verify", "a.tar.gz"], ["verify", "a.tar.gz"]),
        (["list"], ["list"]),
        (["cleanup", "--keep", "3"], ["cleanup", "--keep", "3"]),
        (["info", "a.tar.gz"], ["info", "a.tar.gz"]),
        # A path that merely looks like a command is still a path.
        (["list"], ["list"]),
        (["/var/log/list"], ["archive", "/var/log/list"]),
        # Top level flags are never preceded by an injected subcommand.
        (["--version"], ["--version"]),
        (["--help"], ["--help"]),
        (["-h"], ["-h"]),
        # No positional at all: inject so the error is about the missing path.
        (["-q"], ["archive", "-q"]),
        ([], []),
        # Everything after -- is positional.
        (["--", "-weird-dir"], ["archive", "--", "-weird-dir"]),
    ],
)
def test_normalise_argv(argv: list[str], expected: list[str]) -> None:
    assert normalise_argv(argv) == expected


def test_first_positional_skips_option_values() -> None:
    assert _first_positional(["-o", "/tmp", "-x", "p", "/var/log"]) == "/var/log"
    assert _first_positional(["-o", "/tmp"]) is None
    assert _first_positional(["-o=/tmp", "src"]) == "src"
    assert _first_positional([]) is None


def test_legacy_invocation_behaves_like_the_subcommand(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    legacy = run_cli(str(readable_tree), "-o", str(out_dir), "-q")
    assert legacy[0] == int(ExitCode.SUCCESS)
    first = find_archive(out_dir)

    explicit = run_cli("archive", str(readable_tree), "-o", str(out_dir), "-q")
    assert explicit[0] == int(ExitCode.SUCCESS)

    archives = sorted(p.name for p in out_dir.glob("*.tar.gz"))
    assert len(archives) == 2
    assert all(name.startswith("logs_archive_") for name in archives)
    assert first.is_file()


# --------------------------------------------------------------------------
# Basic behaviour
# --------------------------------------------------------------------------


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert f"log-archive v{__version__}" in capsys.readouterr().out

    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    for command in ("archive", "verify", "list", "cleanup", "info"):
        assert command in out


def test_no_arguments_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == int(ExitCode.USAGE)
    assert "usage" in capsys.readouterr().err.lower()


def test_default_output_directory_is_under_home() -> None:
    assert default_output_dir() == Path.home() / "log-archives"


def test_archive_reports_statistics(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, out, _err = run_cli(str(readable_tree), "-o", str(out_dir))
    assert code == 0
    for label in ("Archive", "Files", "Original", "Compressed", "Ratio", "Duration"):
        assert label in out
    assert "Archive completed" in out
    assert str(out_dir) in out


def test_quiet_suppresses_normal_output(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, out, err = run_cli(str(readable_tree), "-o", str(out_dir), "-q")
    assert code == 0
    assert out == ""
    assert err == ""


def test_quiet_still_reports_errors(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, out, err = run_cli(str(readable_tree / "absent"), "-o", str(out_dir), "-q")
    assert code == int(ExitCode.USAGE)
    assert out == ""
    assert "Error" in err


def test_verbose_shows_details(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, out, _err = run_cli(str(readable_tree), "-o", str(out_dir), "-v")
    assert code == 0
    assert "Exclusions:" in out
    assert "Scanning" in out


def test_activity_log_is_written(readable_tree: Path, out_dir: Path, run_cli) -> None:
    run_cli(str(readable_tree), "-o", str(out_dir), "-q")
    log = out_dir / "archive.log"
    assert log.is_file()
    body = log.read_text()
    assert "[INFO] Starting archive of" in body
    assert "[INFO] Archive completed:" in body
    assert "[INFO] Files archived:" in body


def test_activity_log_records_skips(
    readable_tree: Path, out_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = readable_tree / "blocked.log"
    blocked.write_text("secret\n")
    blocked.chmod(0o000)
    if os.geteuid() == 0:  # pragma: no cover
        pytest.skip("root ignores mode 000")
    try:
        run_cli(str(readable_tree), "-o", str(out_dir), "-q")
    finally:
        blocked.chmod(0o600)
    body = (out_dir / "archive.log").read_text()
    assert "[WARNING] Skipped blocked.log: unreadable" in body


def test_activity_log_never_contains_log_contents(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    """The manifest and log must carry metadata only, never log data."""
    (readable_tree / "secret-value.log").write_text("MY_SUPER_SECRET_TOKEN\n")
    run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--manifest")
    assert "MY_SUPER_SECRET_TOKEN" not in (out_dir / "archive.log").read_text()
    with tarfile.open(find_archive(out_dir)) as tar:
        stream = tar.extractfile(f"{readable_tree.name}/MANIFEST.txt")
        assert stream is not None
        with stream:
            assert b"MY_SUPER_SECRET_TOKEN" not in stream.read()


# --------------------------------------------------------------------------
# dry-run
# --------------------------------------------------------------------------


def test_dry_run_creates_nothing(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, out, _err = run_cli(str(readable_tree), "-o", str(out_dir), "--dry-run")
    assert code == 0
    assert "DRY RUN" in out
    assert "No files were created or modified." in out
    assert not out_dir.exists() or not list(out_dir.iterdir())


def test_dry_run_reports_the_plan(log_tree: Path, out_dir: Path, run_cli) -> None:
    code, out, _err = run_cli(str(log_tree), "-o", str(out_dir), "--dry-run")
    assert code == 0
    assert "Source:" in out
    assert "Destination:" in out
    assert "Compression:" in out
    assert "Would archive:" in out
    assert "Would exclude:" in out
    assert "Estimated size:" in out


def test_dry_run_counts_match_a_real_run(log_tree: Path, out_dir: Path, run_cli) -> None:
    _code, dry_out, _err = run_cli(str(log_tree), "-o", str(out_dir), "--dry-run")
    planned = int(next(line for line in dry_out.splitlines() if "files" in line).split()[0])
    run_cli(str(log_tree), "-o", str(out_dir), "-q")
    with tarfile.open(find_archive(out_dir)) as tar:
        actual = sum(1 for m in tar.getmembers() if m.isfile())
    # The real run also skips the socket and FIFO, which the plan cannot know
    # about without opening every file, so it may archive slightly fewer.
    assert actual <= planned


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


def test_manifest_flag_adds_manifest(readable_tree: Path, out_dir: Path, run_cli) -> None:
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--manifest")[0] == 0
    names = member_names(find_archive(out_dir))
    assert f"{readable_tree.name}/MANIFEST.txt" in names


def test_manifest_absent_without_flag(readable_tree: Path, out_dir: Path, run_cli) -> None:
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q")[0] == 0
    names = member_names(find_archive(out_dir))
    assert not any(name.endswith("MANIFEST.txt") for name in names)


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


def test_list_on_empty_directory(out_dir: Path, run_cli) -> None:
    code, out, _err = run_cli("list", "-o", str(out_dir))
    assert code == 0
    assert "No archives found" in out


def test_list_shows_archives(readable_tree: Path, out_dir: Path, run_cli) -> None:
    run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--checksum")
    code, out, _err = run_cli("list", "-o", str(out_dir))
    assert code == 0
    assert "ARCHIVE" in out
    assert "1 archive(s)" in out


def test_list_limit(readable_tree: Path, out_dir: Path, run_cli) -> None:
    for _ in range(3):
        run_cli(str(readable_tree), "-o", str(out_dir), "-q")
    code, out, _err = run_cli("list", "-o", str(out_dir), "--limit", "2")
    assert code == 0
    assert "2 archive(s)" in out


def test_info_reports_details(readable_tree: Path, out_dir: Path, run_cli) -> None:
    run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--manifest", "--checksum")
    archive = find_archive(out_dir)
    code, out, _err = run_cli("info", str(archive))
    assert code == 0
    assert "Archive Information" in out
    assert "Members:" in out
    assert "Checksum:" in out
    assert "Manifest:" in out
    assert "present" in out
    assert "passed" in out


def test_info_no_verify(readable_tree: Path, out_dir: Path, run_cli) -> None:
    run_cli(str(readable_tree), "-o", str(out_dir), "-q")
    code, out, _err = run_cli("info", str(find_archive(out_dir)), "--no-verify")
    assert code == 0
    assert "not checked" in out


def test_verify_command(readable_tree: Path, out_dir: Path, run_cli) -> None:
    run_cli(str(readable_tree), "-o", str(out_dir), "-q")
    code, out, _err = run_cli("verify", str(find_archive(out_dir)))
    assert code == int(ExitCode.SUCCESS)
    assert "verification successful" in out


# --------------------------------------------------------------------------
# Error handling and exit codes
# --------------------------------------------------------------------------


def test_missing_source_is_usage_error(out_dir: Path, run_cli) -> None:
    code, _out, err = run_cli(str(out_dir / "nope"), "-o", str(out_dir))
    assert code == int(ExitCode.USAGE)
    assert "does not exist" in err


def test_source_is_a_file_is_usage_error(tmp_path: Path, out_dir: Path, run_cli) -> None:
    target = tmp_path / "afile"
    target.write_text("x")
    code, _out, err = run_cli(str(target), "-o", str(out_dir))
    assert code == int(ExitCode.USAGE)
    assert "not a directory" in err


def test_output_path_that_is_a_file_is_usage_error(
    readable_tree: Path, tmp_path: Path, run_cli
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    code, _out, err = run_cli(str(readable_tree), "-o", str(blocker))
    assert code == int(ExitCode.USAGE)
    assert "not a directory" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_source_is_permission_error(readable_tree: Path, out_dir: Path, run_cli) -> None:
    locked = readable_tree / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        code, _out, err = run_cli(str(locked), "-o", str(out_dir))
    finally:
        locked.chmod(0o700)
    assert code == int(ExitCode.PERMISSION)
    assert "sudo" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_output_is_permission_error(
    readable_tree: Path, tmp_path: Path, run_cli
) -> None:
    locked = tmp_path / "locked-out"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        code, _out, _err = run_cli(str(readable_tree), "-o", str(locked / "sub"))
    finally:
        locked.chmod(0o700)
    assert code == int(ExitCode.PERMISSION)


def test_unknown_compression_is_usage_error(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, _out, err = run_cli(str(readable_tree), "-o", str(out_dir), "--compression", "lzw")
    assert code == int(ExitCode.USAGE)
    assert "Unknown compression format" in err


def test_negative_keep_is_usage_error(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, _out, err = run_cli(str(readable_tree), "-o", str(out_dir), "--keep", "-1")
    assert code == int(ExitCode.USAGE)
    assert "zero or greater" in err


def test_mutually_exclusive_symlink_flags(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, _out, _err = run_cli(
        str(readable_tree),
        "-o",
        str(out_dir),
        "--follow-symlinks",
        "--no-follow-symlinks",
    )
    assert code == int(ExitCode.USAGE)


def test_unknown_option_is_usage_error(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, _out, _err = run_cli(str(readable_tree), "-o", str(out_dir), "--nonsense")
    assert code == int(ExitCode.USAGE)


def test_output_is_script_friendly_when_redirected(
    readable_tree: Path, out_dir: Path, tmp_path: Path
) -> None:
    """No ANSI escapes, no progress bars: the output must stay parseable."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "log_archive", str(readable_tree), "-o", str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "\x1b[" not in result.stdout
    assert "Archive completed" in result.stdout


def test_output_survives_a_non_utf8_terminal(readable_tree: Path, out_dir: Path, run_cli) -> None:
    """A status marker must not raise UnicodeEncodeError under LC_ALL=C."""
    import subprocess
    import sys

    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONIOENCODING="ascii")
    result = subprocess.run(
        [sys.executable, "-m", "log_archive", str(readable_tree), "-o", str(out_dir)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    assert "[ok]" in result.stdout


# --------------------------------------------------------------------------
# Compression selection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["gzip", "gzip-fast", "gzip-best", "none", "xz", "bzip2"])
def test_compression_formats(readable_tree: Path, out_dir: Path, run_cli, fmt: str) -> None:
    code, _out, _err = run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--compression", fmt)
    assert code == 0
    archive = find_archive(out_dir)
    with tarfile.open(archive, "r:*") as tar:
        assert f"{readable_tree.name}/one.log" in {m.name for m in tar.getmembers()}


def test_no_compression_produces_plain_tar(readable_tree: Path, out_dir: Path, run_cli) -> None:
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--compression", "none")[0] == 0
    archive = next(p for p in out_dir.iterdir() if p.suffix == ".tar")
    assert archive.read_bytes()[:5] != b"\x1f\x8b"


def test_archives_and_log_are_not_world_readable(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    """Archive permissions must not depend on the caller's umask.

    A log archive records sensitive file paths and host details, so both the
    archive and archive.log are created 0600 regardless of umask.
    """
    import stat as stat_module

    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q")[0] == 0
    archive = find_archive(out_dir)
    assert stat_module.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat_module.S_IMODE((out_dir / "archive.log").stat().st_mode) == 0o600


def test_archive_log_stays_private_under_a_permissive_umask(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    import os
    import stat as stat_module

    previous = os.umask(0o000)
    try:
        assert run_cli(str(readable_tree), "-o", str(out_dir), "-q")[0] == 0
    finally:
        os.umask(previous)
    assert stat_module.S_IMODE((out_dir / "archive.log").stat().st_mode) == 0o600


def test_dry_run_uses_correct_pluralisation(log_tree: Path, out_dir: Path, run_cli) -> None:
    code, out, _err = run_cli(str(log_tree), "-o", str(out_dir), "--dry-run")
    assert code == 0
    # "13 directories", never "directoryies" or a bare "1 directories".
    for line in out.splitlines():
        assert "directoryies" not in line
        assert " 1 directories" not in line
    assert "directories" in out or "directory" in out


def test_dry_run_explains_that_readability_is_not_checked(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    """The plan cannot open files, so it must say so rather than look wrong."""
    code, out, _err = run_cli(str(readable_tree), "-o", str(out_dir), "--dry-run", "-v")
    assert code == 0
    assert "does not open files" in out
    assert "unreadable" in out
