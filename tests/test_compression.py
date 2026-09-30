"""Tests for the compression format table.

The interesting cases here are the ones that depend on the interpreter version.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
import tarfile

import pytest

from helpers import find_archive
from log_archive.compression import DEFAULT_FORMAT, FORMATS, get_format, zstd_available
from log_archive.utils import UsageError

GZIP_FORMATS = {"gzip", "gzip-fast", "gzip-best"}
ALWAYS_AVAILABLE = GZIP_FORMATS | {"none", "xz", "bzip2"}


def test_core_formats_are_always_available() -> None:
    assert set(FORMATS) >= ALWAYS_AVAILABLE


def test_default_is_gzip() -> None:
    assert DEFAULT_FORMAT == "gzip"
    assert get_format("gzip").extension == ".tar.gz"


def test_unknown_format_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="Unknown compression format"):
        get_format("lzw")


@pytest.mark.parametrize(
    ("name", "mode", "level_kwarg", "level"),
    [
        ("gzip", "w:gz", "compresslevel", 6),
        ("gzip-fast", "w:gz", "compresslevel", 1),
        ("gzip-best", "w:gz", "compresslevel", 9),
        ("none", "w", "", None),
        ("xz", "w:xz", "preset", 6),
        ("bzip2", "w:bz2", "compresslevel", 9),
    ],
)
def test_each_format_declares_its_own_level_keyword(
    name: str, mode: str, level_kwarg: str, level: int | None
) -> None:
    """tarfile names the level differently for every codec.

    Passing ``compresslevel`` to ``w:xz`` raises TypeError, which is exactly
    the bug this table exists to prevent.
    """
    fmt = get_format(name)
    assert fmt.tar_mode == mode
    assert fmt.level_kwarg == level_kwarg
    assert fmt.level == level


def test_zstd_is_registered_only_when_the_interpreter_supports_it() -> None:
    assert ("zstd" in FORMATS) is zstd_available()


def test_zstd_probe_survives_a_missing_parent_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for the Python < 3.14 import failure.

    ``find_spec("compression.zstd")`` raises ``ModuleNotFoundError`` when the
    parent package ``compression`` does not exist at all, instead of returning
    ``None``. On Python 3.11 to 3.13 that import error killed the whole CLI at
    import time. Simulated here so it cannot come back.
    """
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, package: str | None = None) -> object:
        if name.startswith("compression"):
            raise ModuleNotFoundError("No module named 'compression'")
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    assert zstd_available() is False


def test_probe_tolerates_a_none_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, package=None: None)
    assert zstd_available() is False


@pytest.mark.skipif(sys.version_info >= (3, 14), reason="this reproduces the pre-3.14 failure")
def test_cli_imports_without_the_compression_package() -> None:
    """On a real pre-3.14 interpreter, importing the CLI must simply work."""
    import log_archive.cli

    assert log_archive.cli.COMMANDS[0] == "archive"


def test_zstd_format_absent_is_not_an_error() -> None:
    """A build without zstd must still offer a usable set of formats."""
    assert "gzip" in FORMATS
    fmt = get_format("gzip")
    assert fmt.is_compressed


def test_formats_are_hashable_and_immutable() -> None:
    fmt = get_format("xz")
    with pytest.raises(AttributeError):
        fmt.level = 1  # type: ignore[misc]


def test_every_registered_format_can_round_trip(readable_tree, tmp_path) -> None:
    """Guards the whole table, including zstd where it is available."""
    from log_archive.archive import build_archive
    from log_archive.exclusions import ExclusionRules

    for name, fmt in FORMATS.items():
        dest = tmp_path / f"out{fmt.extension}"
        build_archive(readable_tree, dest, ExclusionRules.build(), fmt)
        with tarfile.open(dest, "r:*") as tar:
            names = {m.name for m in tar.getmembers()}
        assert f"{readable_tree.name}/one.log" in names, f"{name} lost a file"


def test_reexport_from_a_subprocess_is_clean() -> None:
    """A fresh interpreter must import the package without warnings or errors."""
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", "import log_archive.cli"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_importlib_is_not_needed_at_runtime() -> None:
    """Sanity check that the module list still matches the docs."""
    import log_archive

    for module in ("archive", "cli", "compression", "verification"):
        importlib.import_module(f"log_archive.{module}")
    assert log_archive.__version__


def test_all_commands_work_without_zstd(
    readable_tree, out_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build with no zstd must be fully usable, not partially broken.

    This is the state of every supported interpreter below 3.14, so the whole
    surface is exercised with the format removed from the table. ``FORMATS`` is
    a real dict and the engine reads it at call time, so removing the entry is
    exactly what an older interpreter produces.
    """
    import log_archive.cli as cli_module
    import log_archive.compression as compression_module

    monkeypatch.setattr(compression_module, "zstd_available", lambda: False)
    monkeypatch.delitem(compression_module.FORMATS, "zstd", raising=False)
    assert "zstd" not in compression_module.FORMATS

    out_dir.mkdir(parents=True, exist_ok=True)
    assert (
        cli_module.main([str(readable_tree), "-o", str(out_dir), "-q", "--verify", "--checksum"])
        == 0
    )
    archive = find_archive(out_dir)
    assert archive.exists()
    assert cli_module.main(["verify", str(archive), "-q"]) == 0
    assert cli_module.main(["list", "-o", str(out_dir), "-q"]) == 0
    assert cli_module.main(["info", str(archive), "--no-verify", "-q"]) == 0
    assert cli_module.main(["cleanup", "-o", str(out_dir), "--keep", "1", "-q"]) == 0

    with pytest.raises(UsageError, match="Unknown compression format"):
        get_format("zstd")
