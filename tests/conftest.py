"""Shared fixtures.

Every test works inside ``tmp_path``. Nothing in this suite reads or writes
``/var/log``, and no test depends on the machine it runs on.
"""

from __future__ import annotations

import os
import socket
import tarfile
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from log_archive import cli


@pytest.fixture(autouse=True)
def _isolate_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop tests from reading the developer's real config files.

    ``HOME`` is redirected so ``~/.config/log-archive/config.toml`` cannot leak
    in, and ``log_archive.config.SYSTEM_CONFIG`` points at a non-existent path
    so a real ``/etc/log-archive/config.toml`` is never consulted.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr("log_archive.config.SYSTEM_CONFIG", home / "etc-absent.toml")
    monkeypatch.delenv("LOG_ARCHIVE_CONFIG", raising=False)


@pytest.fixture
def run_cli(capsys: pytest.CaptureFixture[str]) -> Callable[..., tuple[int, str, str]]:
    """Run the CLI in-process and return ``(exit_code, stdout, stderr)``."""

    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


@pytest.fixture
def log_tree(tmp_path: Path) -> Path:
    """A representative log directory.

    Contains nested directories, a rotated log, a name matched by a default
    exclusion, a pid file, a socket, a FIFO, a broken symlink, and a symlink
    pointing outside the tree.
    """
    root = tmp_path / "logs"
    (root / "nginx").mkdir(parents=True)
    (root / "app").mkdir(parents=True)
    (root / "journal").mkdir()

    (root / "syslog").write_text("boot\n")
    (root / "messages").write_text("hello\n" * 50)
    (root / "nginx" / "access.log").write_text("1.2.3.4 - -\n")
    (root / "nginx" / "access.log.1").write_text("rotated\n")
    (root / "app" / "service.log").write_text("running\n")
    (root / "app" / "service.log.gz").write_bytes(b"\x1f\x8b already compressed")
    (root / "app.pid").write_text("1234\n")
    (root / "journal" / "system.journal").write_text("binaryish\n")
    (root / "queue.sock").touch()
    os.mkfifo(root / "pipe.fifo")
    os.symlink("nowhere", root / "broken.link")
    os.symlink("/etc/passwd", root / "escape.link")
    return root


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """An archive destination that does not exist yet."""
    return tmp_path / "archives"


def member_names(archive: Path) -> set[str]:
    """Return the set of member names inside *archive*."""
    with tarfile.open(archive, "r:*") as tar:
        return {member.name for member in tar.getmembers()}


def sockets_available() -> bool:  # pragma: no cover - environment probe
    """Whether this platform can create unix sockets (always true on Linux)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(Path(os.devnull)))
    except OSError:
        return False
    return True


@pytest.fixture
def empty_dir(tmp_path: Path) -> Path:
    """An existing but empty directory."""
    path = tmp_path / "empty"
    path.mkdir()
    return path


@pytest.fixture
def readable_tree(tmp_path: Path) -> Iterator[Path]:
    """A small tree of readable files, restored to readable mode on teardown.

    Tests that remove read permission must put it back, otherwise pytest's own
    cleanup of ``tmp_path`` can fail and mask the real assertion.
    """
    root = tmp_path / "readable"
    (root / "sub").mkdir(parents=True)
    (root / "one.log").write_text("one\n")
    (root / "sub" / "two.log").write_text("two\n")
    try:
        yield root
    finally:
        for path in root.rglob("*"):
            with suppress(OSError):
                path.chmod(0o700 if path.is_dir() else 0o600)
        root.chmod(0o700)
