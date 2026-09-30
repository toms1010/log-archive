"""Optional TOML configuration.

Settings are read from, in increasing order of priority:

1. ``/etc/log-archive/config.toml``  (system wide)
2. ``~/.config/log-archive/config.toml``  (per user)
3. explicit ``--config PATH``
4. command line flags

Nothing is required. With no config file the tool behaves exactly as it always
has. Unknown keys and wrong value types are hard errors rather than silent
no-ops, so a typo in a config file is reported instead of quietly ignored.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .compression import FORMATS
from .utils import ConfigError, expand_path

#: System wide configuration file.
SYSTEM_CONFIG: Final = Path("/etc/log-archive/config.toml")

#: Per user configuration file.
USER_CONFIG: Final = Path("~/.config/log-archive/config.toml")

_BOOL_KEYS: Final = ("verify", "checksum", "manifest", "follow_symlinks", "no_default_excludes")
_STR_KEYS: Final = ("output", "compression")
_INT_KEYS: Final = ("keep",)
_LIST_KEYS: Final = ("exclude",)
_ALL_KEYS: Final = (*_BOOL_KEYS, *_STR_KEYS, *_INT_KEYS, *_LIST_KEYS)


@dataclass(frozen=True)
class Config:
    """Effective configuration.

    ``None`` means "not specified anywhere", which lets the CLI distinguish
    between an unset option and an explicit one.
    """

    output: str | None = None
    compression: str | None = None
    verify: bool | None = None
    checksum: bool | None = None
    manifest: bool | None = None
    follow_symlinks: bool | None = None
    no_default_excludes: bool | None = None
    keep: int | None = None
    exclude: tuple[str, ...] = field(default_factory=tuple)
    #: Files that contributed, most general first. Useful for ``--verbose``.
    sources: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """Whether the config contributed nothing at all."""
        return not any(
            (
                self.output,
                self.compression,
                self.verify,
                self.checksum,
                self.manifest,
                self.follow_symlinks,
                self.no_default_excludes,
                self.keep,
                self.exclude,
            )
        )


def _read_one(path: Path) -> dict[str, Any]:
    """Parse a single TOML file into a validated dictionary."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Config {path} is not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    return _validate(data, path)


def _validate(data: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Check keys and value types, returning only the keys we understand."""
    unknown = sorted(set(data) - set(_ALL_KEYS))
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in {path}: {', '.join(unknown)}. Valid keys: {', '.join(_ALL_KEYS)}."
        )

    clean: dict[str, Any] = {}
    for key in _BOOL_KEYS:
        if key in data:
            value = data[key]
            if not isinstance(value, bool):
                raise ConfigError(f"{key} in {path} must be true or false, got {value!r}.")
            clean[key] = value

    for key in _STR_KEYS:
        if key in data:
            value = data[key]
            if not isinstance(value, str):
                raise ConfigError(f"{key} in {path} must be a string, got {value!r}.")
            clean[key] = value

    for key in _INT_KEYS:
        if key in data:
            value = data[key]
            # bool is a subclass of int; `keep = true` is a mistake, not a 1.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{key} in {path} must be an integer, got {value!r}.")
            if value < 0:
                raise ConfigError(f"{key} in {path} must not be negative, got {value}.")
            clean[key] = value

    for key in _LIST_KEYS:
        if key in data:
            value = data[key]
            if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
                raise ConfigError(f"{key} in {path} must be a list of strings, got {value!r}.")
            clean[key] = list(value)

    compression = clean.get("compression")
    if compression is not None and compression not in FORMATS:
        raise ConfigError(
            f"Unknown compression in {path}: {compression!r}. Choose one of: {', '.join(FORMATS)}."
        )
    return clean


def load_config(explicit: str | None = None, *, use_config: bool = True) -> Config:
    """Load and merge configuration.

    Args:
        explicit: Path given via ``--config``. When set, it is the *only* file
            read, and a missing file is an error.
        use_config: Set False by ``--no-config`` to ignore configuration
            entirely. This wins over *explicit*, so ``--no-config -c PATH``
            does the obvious thing instead of quietly reading PATH anyway.

    Returns:
        The merged configuration.

    Raises:
        ConfigError: on unreadable, malformed, or invalid configuration.
    """
    if not use_config:
        return Config()

    if explicit is not None:
        path = expand_path(explicit)
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        if not path.is_file():
            raise ConfigError(f"Config path is not a file: {path}")
        return _build(_read_one(path), (path,))

    merged: dict[str, Any] = {}
    sources: list[Path] = []
    for path in (SYSTEM_CONFIG, expand_path(str(USER_CONFIG))):
        data = _read_one(path)
        if data:
            sources.append(path)
        merged.update(data)
    return _build(merged, tuple(sources))


def _build(data: Mapping[str, Any], sources: tuple[Path, ...]) -> Config:
    """Turn a validated dictionary into a :class:`Config`."""
    return Config(
        output=data.get("output"),
        compression=data.get("compression"),
        verify=data.get("verify"),
        checksum=data.get("checksum"),
        manifest=data.get("manifest"),
        follow_symlinks=data.get("follow_symlinks"),
        no_default_excludes=data.get("no_default_excludes"),
        keep=data.get("keep"),
        exclude=tuple(data.get("exclude", ())),
        sources=sources,
    )
