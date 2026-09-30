"""Statistics collected while walking and writing an archive.

The counters live in their own module because both the dry run and the real
archive need them, and because the report rendering belongs next to the
counters it renders. Nothing here touches the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .utils import compression_ratio, human_duration, human_size

#: Cap on how many example paths are remembered, so reporting stays cheap on
#: a directory with hundreds of thousands of entries.
SAMPLE_LIMIT = 200


@dataclass(frozen=True)
class SkippedEntry:
    """A path deliberately left out, with the reason why."""

    relative: str
    reason: str


@dataclass
class ArchiveStats:
    """Counters describing one archive operation."""

    #: Regular files that entered the archive.
    files_archived: int = 0
    #: Directories that entered the archive.
    directories_archived: int = 0
    #: Symlinks stored as links.
    symlinks_archived: int = 0
    #: Bytes of log data before compression.
    original_size: int = 0
    #: Size of the finished archive, filled in once it is closed.
    archive_size: int = 0
    #: Wall clock seconds, filled in when the run finishes.
    duration: float = 0.0
    #: Paths removed by an exclusion rule.
    files_excluded: int = 0
    #: Paths that could not be archived (unreadable, special, broken, ...).
    files_skipped: int = 0
    #: Files that changed underneath us. A non-zero value means the tar
    #: framing may be unsound, so the caller must not keep the archive.
    anomalies: int = 0
    #: Bounded samples, for human readable reports.
    excluded_samples: list[str] = field(default_factory=list)
    skipped_entries: list[SkippedEntry] = field(default_factory=list)

    @property
    def files_found(self) -> int:
        """Regular files that were candidates for archiving."""
        return self.files_archived + self.files_skipped

    @property
    def files_considered(self) -> int:
        """Everything the scanner decided about: kept, excluded, or skipped."""
        return self.files_archived + self.files_excluded + self.files_skipped

    @property
    def ratio(self) -> float:
        """Percentage of space saved by compression."""
        return compression_ratio(self.original_size, self.archive_size)

    def note_excluded(self, relative: str) -> None:
        """Record a path removed by an exclusion rule."""
        self.files_excluded += 1
        if len(self.excluded_samples) < SAMPLE_LIMIT:
            self.excluded_samples.append(relative)

    def note_skipped(self, relative: str, reason: str) -> None:
        """Record a path that could not be archived safely."""
        self.files_skipped += 1
        if len(self.skipped_entries) < SAMPLE_LIMIT:
            self.skipped_entries.append(SkippedEntry(relative, reason))

    def record_file(self, size: int) -> None:
        """Count a regular file that was successfully archived."""
        self.files_archived += 1
        self.original_size += size

    def record_directory(self) -> None:
        """Count a directory that was archived."""
        self.directories_archived += 1

    def record_symlink(self) -> None:
        """Count a symlink that was stored as a link."""
        self.symlinks_archived += 1

    def sample_limit_reached(self) -> bool:
        """Whether the recorded samples were truncated."""
        return self.files_excluded > len(self.excluded_samples) or (
            self.files_skipped > len(self.skipped_entries)
        )


def format_report(stats: ArchiveStats, *, archive_name: str) -> str:
    """Render the end-of-run statistics block."""
    lines = [
        f"Archive     {archive_name}",
        f"Files       {stats.files_archived}",
        f"Skipped     {stats.files_skipped}",
    ]
    if stats.files_excluded:
        lines.insert(2, f"Excluded    {stats.files_excluded}")
    lines += [
        f"Original    {human_size(stats.original_size)}",
        f"Compressed  {human_size(stats.archive_size)}",
        f"Ratio       {stats.ratio:.1f}%",
        f"Duration    {human_duration(stats.duration)}",
    ]
    if stats.directories_archived:
        lines.append(f"Directories {stats.directories_archived}")
    if stats.symlinks_archived:
        lines.append(f"Symlinks    {stats.symlinks_archived}")
    return "\n".join(lines)
