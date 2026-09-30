"""The ``MANIFEST.txt`` member that describes an archive.

The manifest is written *into the archive only*. The source directory is never
touched, so archiving a directory never mutates it.

The manifest records metadata about the run - host, tool version, counts,
exclusion rules. It never records log contents, because a manifest travels
with the archive and would otherwise copy sensitive data into a second place.

Position note: the manifest is appended as the final tar member rather than the
first. That lets the engine report an exact file count without a second full
directory scan, which matters on a tree the size of ``/var/log``. Tar members
are order independent, so extraction is unaffected.
"""

from __future__ import annotations

import getpass
import platform
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .utils import MANIFEST_MEMBER


@dataclass
class Manifest:
    """Descriptive metadata captured at archive creation time."""

    created: datetime
    source: Path
    compression: str
    #: Manifest schema version, independent of the tool version.
    version: str = "1.0"
    excludes: Sequence[str] = field(default_factory=tuple)
    file_count: int = 0
    hostname: str = ""
    username: str = ""
    kernel: str = ""
    system: str = ""
    python_version: str = ""
    tool_version: str = __version__

    @property
    def member_name(self) -> str:
        """Bare name of the manifest inside the archive."""
        return MANIFEST_MEMBER

    @classmethod
    def collect(
        cls,
        *,
        created: datetime,
        source: Path,
        compression: str,
        excludes: Sequence[str],
        file_count: int = 0,
    ) -> Manifest:
        """Build a manifest, filling in host and runtime details.

        Host and user lookups are best effort: the tool must still work inside
        a minimal container that has no ``/etc/passwd`` entry for the current
        uid.
        """
        try:
            username = getpass.getuser()
        except (KeyError, OSError):
            username = "unknown"
        return cls(
            created=created,
            source=source,
            compression=compression,
            excludes=tuple(excludes),
            file_count=file_count,
            hostname=platform.node() or "unknown",
            username=username,
            kernel=platform.release() or "unknown",
            system=platform.system() or "unknown",
            python_version=platform.python_version(),
        )

    def render(self) -> str:
        """Return the manifest body as text."""
        excluded = "\n".join(f"  {item}" for item in self.excludes) or "  (none)"
        return (
            "LOG-ARCHIVE MANIFEST\n"
            "===================\n\n"
            f"Version:     {self.version}\n"
            f"Tool:        log-archive {self.tool_version}\n"
            f"Created:     {self.created.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Hostname:    {self.hostname}\n"
            f"User:        {self.username}\n"
            f"Source:      {self.source}\n"
            f"Compression: {self.compression}\n"
            f"Kernel:      {self.kernel}\n"
            f"System:      {self.system}\n"
            f"Python:      {self.python_version}\n"
            f"Interpreter: {sys.executable}\n"
            f"Files:       {self.file_count}\n"
            f"Excluded:\n{excluded}\n"
        )

    def to_bytes(self) -> bytes:
        """Return the manifest body encoded as UTF-8."""
        return self.render().encode("utf-8")

    @staticmethod
    def parse(text: str) -> dict[str, str]:
        """Parse a rendered manifest back into a mapping.

        Used by ``log-archive info``. Tolerant by design: a manifest written
        by a different version may be missing keys, and ``info`` should still
        show whatever is readable.
        """
        fields: dict[str, str] = {}
        in_excluded = False
        for line in text.splitlines():
            if line.startswith("Excluded:"):
                in_excluded = True
                continue
            if in_excluded:
                if line.startswith("  "):
                    fields.setdefault("Excluded", "")
                    fields["Excluded"] += line.strip() + "\n"
                    continue
                in_excluded = False
            if ":" in line and not line.startswith(" "):
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        return fields
