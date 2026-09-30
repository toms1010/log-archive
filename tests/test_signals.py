"""Tests for interrupt and signal handling.

Interrupting a backup must never leave a truncated archive that looks like a
valid one, because the next script to check for a file with today's date will
happily treat it as a good backup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from log_archive.signals import HANDLED_SIGNALS, OperationInterrupted, interruptible
from log_archive.utils import ExitCode, remove_quietly


def test_handled_signals_include_both() -> None:
    assert signal.SIGINT in HANDLED_SIGNALS
    assert signal.SIGTERM in HANDLED_SIGNALS


def test_interruptible_restores_previous_handlers() -> None:
    before = {sig: signal.getsignal(sig) for sig in HANDLED_SIGNALS}
    with interruptible():
        for sig in HANDLED_SIGNALS:
            assert signal.getsignal(sig) is not before[sig] or before[sig] is signal.SIG_DFL
    after = {sig: signal.getsignal(sig) for sig in HANDLED_SIGNALS}
    assert after == before


def test_interruptible_restores_handlers_on_error() -> None:
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(RuntimeError), interruptible():
        raise RuntimeError("boom")
    assert signal.getsignal(signal.SIGTERM) == before


def test_operation_interrupted_exit_code() -> None:
    assert OperationInterrupted(signal.SIGINT).exit_code == int(ExitCode.INTERRUPTED)
    assert OperationInterrupted(signal.SIGTERM).exit_code == int(ExitCode.INTERRUPTED)
    assert OperationInterrupted(signal.SIGINT).signal_name == "SIGINT"


def test_sigterm_raises_inside_the_context() -> None:
    with pytest.raises(OperationInterrupted), interruptible():
        os.kill(os.getpid(), signal.SIGTERM)


def _spawn_slow_archive(source: Path, out_dir: Path) -> subprocess.Popen[str]:
    """Start an archive of *source* in a subprocess, sized so it is slow enough."""
    for index in range(400):
        (source / f"big{index}.log").write_bytes(b"x" * 400_000)
    return subprocess.Popen(
        [sys.executable, "-m", "log_archive", str(source), "-o", str(out_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_interrupt_leaves_no_partial_archive(sig: int, tmp_path: Path) -> None:
    source = tmp_path / "logs"
    source.mkdir()
    out_dir = tmp_path / "out"

    process = _spawn_slow_archive(source, out_dir)
    time.sleep(0.4)
    process.send_signal(sig)
    _out, err = process.communicate(timeout=60)

    assert process.returncode == int(ExitCode.INTERRUPTED), err
    assert not list(out_dir.glob("*.tar.gz")), "a partial archive was left behind"
    assert not list(out_dir.glob("*.tar.gz.sha256"))
    assert "interrupted" in err.lower()


def test_keyboard_interrupt_returns_130(readable_tree: Path, out_dir: Path, run_cli) -> None:
    """A KeyboardInterrupt raised anywhere is still cleaned up and reported."""
    import log_archive.archive as archive_module

    def interrupting_build(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    real = archive_module.build_archive
    try:
        import log_archive.cli as cli_module

        cli_module.build_archive = interrupting_build  # type: ignore[assignment]
        code, _out, err = run_cli(str(readable_tree), "-o", str(out_dir))
    finally:
        import log_archive.cli as cli_module

        cli_module.build_archive = real  # type: ignore[assignment]
    assert code == int(ExitCode.INTERRUPTED)
    assert "interrupted" in err.lower()
    assert not list(out_dir.glob("*.tar.gz"))


def test_partial_archive_context_manager_removes_the_file(tmp_path: Path) -> None:
    from log_archive.cli import Printer, _partial_archive

    target = tmp_path / "logs_archive_20260930_065300.tar.gz"
    target.write_bytes(b"half written")
    printer = Printer(quiet=True)
    with pytest.raises(RuntimeError), _partial_archive(target, printer):
        raise RuntimeError("failure mid-write")
    assert not target.exists()


def test_partial_archive_context_manager_is_a_noop_on_success(tmp_path: Path) -> None:
    from log_archive.cli import Printer, _partial_archive

    target = tmp_path / "logs_archive_20260930_065300.tar.gz"
    target.write_bytes(b"complete")
    with _partial_archive(target, Printer(quiet=True)):
        pass
    assert target.exists()


def test_remove_quietly_tolerates_missing(tmp_path: Path) -> None:
    assert remove_quietly(tmp_path / "absent") is False
    target = tmp_path / "present"
    target.touch()
    assert remove_quietly(target) is True
