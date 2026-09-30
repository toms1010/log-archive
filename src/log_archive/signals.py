"""Interrupt handling.

``Ctrl+C`` raises :class:`KeyboardInterrupt` on its own, but ``SIGTERM`` - what
``systemctl stop`` and ``docker stop`` send - would kill the process outright
and leave a truncated ``.tar.gz`` sitting in the output directory. That file
looks exactly like a good backup to every script that checks for its
existence.

Both signals are therefore funnelled into one exception so a single cleanup
path can remove the partial archive and exit 130.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from types import FrameType

from .utils import ExitCode

#: Signals treated as "stop what you are doing and clean up".
HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)

#: A previously installed handler: a ``signal.SIG_*`` constant, a callable, or
#: ``None`` when the handler was never set from Python.
_Handler = int | Callable[[int, FrameType | None], object] | None


class OperationInterrupted(Exception):
    """Raised in the main thread when SIGINT or SIGTERM arrives."""

    def __init__(self, signum: int) -> None:
        name = signal.Signals(signum).name
        super().__init__(f"Interrupted by {name}")
        self.signum = signum
        self.signal_name = name

    @property
    def exit_code(self) -> int:
        """Exit status for an interrupted run.

        130 is the conventional shell status for SIGINT (128 + 2). SIGTERM
        reports as 130 as well so scripts have a single case to handle.
        """
        return int(ExitCode.INTERRUPTED)


def _raise_interrupt(signum: int, _frame: FrameType | None) -> None:
    """Signal handler that converts a signal into :class:`OperationInterrupted`."""
    raise OperationInterrupted(signum)


@contextmanager
def interruptible() -> Iterator[None]:
    """Turn SIGINT and SIGTERM into :class:`OperationInterrupted`.

    Previous handlers are restored on exit. Signal handlers can only be
    installed from the main thread; elsewhere (tests, worker threads) this
    context manager quietly does nothing, which is why it never raises.
    """
    previous: dict[signal.Signals, _Handler] = {}
    for sig in HANDLED_SIGNALS:
        try:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _raise_interrupt)
        except (ValueError, OSError, RuntimeError):
            # Not the main thread, or the platform disallows it. Leave the
            # default behaviour in place rather than failing the run.
            previous.pop(sig, None)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            with suppress(ValueError, OSError, RuntimeError):
                signal.signal(sig, handler)
