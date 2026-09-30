"""Compression formats, kept behind one small abstraction.

The archive engine asks for a :class:`CompressionFormat` and gets a tar mode,
a level, and a filename extension. Adding zstd, bzip2, or a new level is a
matter of adding one entry to :data:`FORMATS`; nothing else in the codebase
needs to change.

Only the standard library is used. zstd is registered when the interpreter
provides :mod:`compression.zstd` (CPython 3.14+), which is why it is
conditional rather than a hard requirement.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from .utils import UsageError


@dataclass(frozen=True)
class CompressionFormat:
    """Everything the engine needs to know about one compression choice."""

    name: str
    extension: str
    #: tarfile open mode. ``"w"`` means an uncompressed tar.
    tar_mode: str
    #: Compression level, or ``None`` when the mode takes no level.
    level: int | None
    #: Keyword ``tarfile.open`` uses for the level.
    #:
    #: There is no single name for this. gzip and bzip2 call it
    #: ``compresslevel``, xz calls it ``preset``, and zstd calls it ``level``.
    #: Keeping the keyword next to the format is the whole reason this is a
    #: table rather than a hard coded ``if compression == "gzip"``.
    level_kwarg: str
    description: str
    #: Human label used in output, e.g. ``gzip``.
    label: str

    @property
    def is_compressed(self) -> bool:
        """Whether this format actually compresses."""
        return self.tar_mode != "w"


def zstd_available() -> bool:
    """Return True when this interpreter can write zstd tarballs.

    ``compression.zstd`` only exists on CPython 3.14+. Probing it with
    :func:`importlib.util.find_spec` is not sufficient on its own: for a dotted
    name, ``find_spec`` imports the *parent* package first, and raises
    ``ModuleNotFoundError`` when that parent does not exist rather than
    returning ``None``. On Python 3.11 to 3.13 the entire ``compression``
    package is absent, so that error has to be caught here or importing this
    module fails outright.
    """
    try:
        return importlib.util.find_spec("compression.zstd") is not None
    except (ImportError, ValueError):
        return False


_GZIP = "gzip"
_XZ = "xz"
_NONE = "none"


def _build_formats() -> dict[str, CompressionFormat]:
    formats: dict[str, CompressionFormat] = {
        _GZIP: CompressionFormat(
            name=_GZIP,
            extension=".tar.gz",
            tar_mode="w:gz",
            level=6,
            level_kwarg="compresslevel",
            description="gzip compression, default level",
            label="gzip",
        ),
        "gzip-fast": CompressionFormat(
            name="gzip-fast",
            extension=".tar.gz",
            tar_mode="w:gz",
            level=1,
            level_kwarg="compresslevel",
            description="gzip compression, fastest (largest output)",
            label="gzip -1",
        ),
        "gzip-best": CompressionFormat(
            name="gzip-best",
            extension=".tar.gz",
            tar_mode="w:gz",
            level=9,
            level_kwarg="compresslevel",
            description="gzip compression, smallest (slowest)",
            label="gzip -9",
        ),
        _NONE: CompressionFormat(
            name=_NONE,
            extension=".tar",
            tar_mode="w",
            level=None,
            level_kwarg="",
            description="no compression, plain tar",
            label="none",
        ),
        _XZ: CompressionFormat(
            name=_XZ,
            extension=".tar.xz",
            tar_mode="w:xz",
            level=6,
            level_kwarg="preset",
            description="xz compression, slowest but smallest",
            label="xz",
        ),
        "bzip2": CompressionFormat(
            name="bzip2",
            extension=".tar.bz2",
            tar_mode="w:bz2",
            level=9,
            level_kwarg="compresslevel",
            description="bzip2 compression",
            label="bzip2",
        ),
    }
    if zstd_available():
        formats["zstd"] = CompressionFormat(
            name="zstd",
            extension=".tar.zst",
            tar_mode="w:zst",
            level=3,
            level_kwarg="level",
            description="zstd compression, fast with a good ratio",
            label="zstd",
        )
    return formats


#: Every compression choice this build understands.
FORMATS: Final[Mapping[str, CompressionFormat]] = _build_formats()

#: The compression used when nothing else is specified.
DEFAULT_FORMAT: Final = _GZIP


def get_format(name: str) -> CompressionFormat:
    """Look up a compression format by name.

    Raises:
        UsageError: if the name is unknown in this build.
    """
    try:
        return FORMATS[name]
    except KeyError:
        raise UsageError(
            f"Unknown compression format: {name!r}. Choose one of: {', '.join(FORMATS)}."
        ) from None


def available_formats() -> list[str]:
    """Return the names of all supported compression formats."""
    return list(FORMATS)
