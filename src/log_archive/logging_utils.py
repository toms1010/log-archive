"""Activity log for the archiver.

Writes ``[LEVEL] message`` lines to ``archive.log`` beside the archives.

Two properties matter here and both were bugs in the original script:

* A failure to write the log must never crash the program or hide the real
  error, so every write is guarded.
* Log contents are *metadata only* - file names, counts, sizes. Log file
  bodies are never read into the message.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import IO

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

#: ``archive.log`` sits next to archives that are created 0600, so it gets the
#: same treatment rather than whatever the caller's umask happens to allow.
#: Its contents are metadata only, but it still names log paths and the host.
_LOG_MODE = 0o600


class ArchiveLogger:
    """Append structured lines to ``archive.log``, tolerating write failures."""

    def __init__(
        self,
        path: Path,
        *,
        verbose: bool = False,
        quiet: bool = False,
        stream: IO[str] | None = None,
    ) -> None:
        self.path = path
        self.verbose = verbose
        self.quiet = quiet
        self._stream = stream if stream is not None else sys.stderr
        self._disabled = False

    def _write(self, level: str, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{level}] {message}\n"
        try:
            # O_CREAT with an explicit mode keeps the permissions predictable;
            # a plain open("a") would defer to the umask.
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                _LOG_MODE,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            # The log is a convenience, not the product. Losing it must not
            # mask the error that actually brought us here.
            if not self._disabled:
                self._disabled = True
                print(
                    f"Warning: could not write {self.path}: {exc}",
                    file=self._stream,
                )

    def debug(self, message: str) -> None:
        """Record a message shown only in verbose mode."""
        if self.verbose:
            self._write("DEBUG", message)

    def info(self, message: str) -> None:
        """Record a normal progress message."""
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        """Record a recoverable problem, such as a file that was skipped."""
        self._write("WARNING", message)

    def error(self, message: str) -> None:
        """Record a failure."""
        self._write("ERROR", message)
