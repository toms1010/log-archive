"""Glob based exclusion rules for archive members.

Two flavours of pattern are supported, and the distinction is what makes the
rules predictable:

``nginx/*.old``
    Contains a ``/``, so it is matched against the *path relative to the
    source directory* (``nginx/access.old``). It matches at that depth only.

``*.old``
    Contains no ``/``, so it is matched against the *base name* of every
    entry, at any depth. This mirrors how the tool has always behaved.

``*`` and ``**`` both match across ``/`` here, so ``journal/**`` matches every
file under a journal tree. Path rules are always anchored at the source root,
never at the filesystem root, so ``journal`` cannot accidentally match a
``/var/log/journal`` on a different machine.

How a rule removes a whole subtree
-----------------------------------

A rule only ever matches a single path. What prunes a directory is the engine
seeing the directory itself excluded and then not descending into it. So the
built-in ``journal`` rule removes ``journal/`` and everything under it, while
``journal/**`` is the explicit way to say the same thing and also matches
individual files nested deeper. Both work; the first is cheaper.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence

#: Built in exclusions. These are the rules the original tool shipped with.
#:
#: ``*.gz`` / ``*.xz`` / ``*.zst`` are kept for backwards compatibility, but
#: note that rotated logs are exactly the files an archiver is usually wanted
#: for. They are already compressed, so including them is cheap. Use
#: ``-x '!*.gz'`` is not supported; instead drop them by editing the defaults
#: with ``--no-default-excludes`` and re-adding what you want.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "journal",  # /var/log/journal - use journalctl instead
    "*.sock",  # nginx / php-fpm / docker sockets
    "*.pid",  # transient pid files
    "wtmp",  # binary login records
    "btmp",  # binary failed-login records
    "*.gz",  # already compressed logs
    "*.xz",
    "*.zst",
)


class ExclusionRules:
    """Decide whether a path should be left out of the archive."""

    def __init__(self, patterns: Iterable[str] | None = None) -> None:
        self._path_patterns: list[str] = []
        self._name_patterns: list[str] = []
        for pattern in patterns or ():
            self.add(pattern)

    def add(self, pattern: str) -> None:
        """Register a single glob pattern.

        Raises:
            ValueError: if the pattern is empty or only whitespace.
        """
        cleaned = pattern.strip()
        if not cleaned:
            raise ValueError("Exclusion pattern must not be empty.")
        if "/" in cleaned:
            self._path_patterns.append(cleaned)
        else:
            self._name_patterns.append(cleaned)

    def update(self, patterns: Iterable[str]) -> None:
        """Register several glob patterns."""
        for pattern in patterns:
            self.add(pattern)

    @classmethod
    def build(
        cls,
        extra: Sequence[str] = (),
        *,
        use_defaults: bool = True,
    ) -> ExclusionRules:
        """Build a rule set from the defaults plus any user supplied patterns."""
        rules = cls(DEFAULT_EXCLUDES if use_defaults else ())
        rules.update(extra)
        return rules

    @property
    def patterns(self) -> list[str]:
        """All active patterns, in the order they were registered."""
        return [*self._path_patterns, *self._name_patterns]

    def __len__(self) -> int:
        return len(self._path_patterns) + len(self._name_patterns)

    def matches(self, relative_path: str) -> bool:
        """Return ``True`` when *relative_path* (source relative, ``/`` joined) is excluded."""
        base = relative_path.rsplit("/", 1)[-1]
        for pattern in self._path_patterns:
            if fnmatch.fnmatchcase(relative_path, pattern):
                return True
        return any(fnmatch.fnmatchcase(base, pattern) for pattern in self._name_patterns)

    def describe(self) -> str:
        """Human readable, single line summary of the active rules."""
        if not len(self):
            return "none"
        return ", ".join(self.patterns)
