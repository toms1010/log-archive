"""Tests for SHA-256 checksum generation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from helpers import find_archive
from log_archive.checksum import read_checksum_file, sha256_file, write_checksum_file
from log_archive.cli import ExitCode


def test_sha256_matches_hashlib(tmp_path: Path) -> None:
    target = tmp_path / "payload.bin"
    target.write_bytes(b"log data" * 10_000)
    assert sha256_file(target) == hashlib.sha256(b"log data" * 10_000).hexdigest()


def test_sha256_handles_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "empty"
    target.write_bytes(b"")
    assert sha256_file(target) == hashlib.sha256(b"").hexdigest()


def test_checksum_file_is_sha256sum_compatible(tmp_path: Path) -> None:
    """The file must be checkable with the standard tool, not just ours."""
    archive = tmp_path / "logs_archive_20260930_065300.tar.gz"
    archive.write_bytes(b"pretend this is compressed")

    digest, checksum_path = write_checksum_file(archive)
    assert checksum_path.name == "logs_archive_20260930_065300.tar.gz.sha256"
    assert read_checksum_file(checksum_path) == digest

    body = checksum_path.read_text()
    assert body == f"{digest}  logs_archive_20260930_065300.tar.gz\n"


def test_checksum_is_streamed_not_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A checksum of a huge archive must not read it all into memory."""
    import log_archive.checksum as checksum_module

    target = tmp_path / "big.bin"
    with target.open("wb") as handle:
        for _ in range(8):
            handle.write(b"x" * (1024 * 1024))

    sizes: list[int] = []
    real_open = Path.open

    def tracking_open(self: Path, *args: object, **kwargs: object):
        handle = real_open(self, *args, **kwargs)  # type: ignore[arg-type]
        real_read = handle.read

        def read(size: int = -1) -> bytes:
            data = real_read(size)
            sizes.append(len(data))
            return data

        handle.read = read  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)
    sha256_file(target)
    assert max(sizes) <= checksum_module._CHUNK_SIZE


def test_cli_writes_checksum_with_flag(readable_tree: Path, out_dir: Path, run_cli) -> None:
    code, out, _err = run_cli(str(readable_tree), "-o", str(out_dir), "--checksum")
    assert code == int(ExitCode.SUCCESS)

    archive = find_archive(out_dir)
    sidecar = archive.with_name(archive.name + ".sha256")
    assert sidecar.is_file()
    assert read_checksum_file(sidecar) == sha256_file(archive)
    assert "SHA-256" in out
    assert "Checksum" in out


def test_cli_does_not_write_checksum_without_flag(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q")[0] == 0
    assert not list(out_dir.glob("*.sha256"))


def test_checksum_write_failure_exits_with_checksum_code(
    readable_tree: Path, out_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    import log_archive.checksum as checksum_module

    def boom(path: Path) -> tuple[str, Path]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(checksum_module, "write_checksum_file", boom)
    # The CLI imports the symbol directly, so patch there too.
    monkeypatch.setattr("log_archive.cli.write_checksum_file", boom)
    code, _out, err = run_cli(str(readable_tree), "-o", str(out_dir), "--checksum")
    assert code == int(ExitCode.CHECKSUM)
    assert "checksum" in err.lower()


def test_checksum_detects_tampering(tmp_path: Path) -> None:
    archive = tmp_path / "logs_archive_20260930_065300.tar.gz"
    archive.write_bytes(b"original bytes")
    digest, checksum_path = write_checksum_file(archive)
    archive.write_bytes(b"tampered bytes")
    assert sha256_file(archive) != digest
    assert checksum_path.is_file()
