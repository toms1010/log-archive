"""Command line interface for ``log-archive``.

Command structure::

    log-archive archive /var/log     # create an archive
    log-archive verify ARCHIVE       # check an existing archive
    log-archive list                 # list archives in the output directory
    log-archive cleanup --keep 10    # apply retention
    log-archive info ARCHIVE         # show details about an archive

For backwards compatibility with the original single command tool, a bare
source path is rewritten into the ``archive`` subcommand, so both of these are
equivalent::

    log-archive /var/log
    log-archive archive /var/log

Exit codes are part of the public contract; see :class:`log_archive.utils.ExitCode`.
"""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, TypeVar

from . import __version__
from .archive import build_archive, plan_archive
from .checksum import read_checksum_file, write_checksum_file
from .compression import DEFAULT_FORMAT, FORMATS, CompressionFormat, get_format
from .config import Config, load_config
from .exclusions import ExclusionRules
from .logging_utils import ArchiveLogger
from .manifest import Manifest
from .retention import apply_cleanup, discover, plan_cleanup
from .signals import OperationInterrupted, interruptible
from .statistics import ArchiveStats, format_report
from .usage_text import EXAMPLES
from .utils import (
    ARCHIVE_PREFIX,
    LOG_FILENAME,
    ChecksumError,
    CleanupError,
    ExitCode,
    LogArchiveError,
    PermissionDeniedError,
    UsageError,
    VerificationError,
    expand_path,
    failure_mark,
    human_size,
    plural,
    raise_for_oserror,
    remove_quietly,
    reserve_unique_path,
    success_mark,
)
from .verification import count_members, describe_format, verify_archive

#: Type variable for option values that may come from CLI, config, or neither.
T = TypeVar("T")

PROGRAM = "log-archive"

#: Subcommand names. Anything else in the first positional slot is treated as
#: a source directory, which is what makes the legacy invocation work.
COMMANDS = ("archive", "verify", "list", "cleanup", "info")

#: Options that consume the following token as their value. Needed to locate
#: the first positional argument when deciding whether to inject the ``archive``
#: subcommand.
_VALUE_OPTIONS = frozenset(
    {
        "-o",
        "--output",
        "-x",
        "--exclude",
        "--keep",
        "--compression",
        "-c",
        "--config",
        "--limit",
    }
)

#: Top level flags that must not be preceded by an injected subcommand.
_TOPLEVEL_FLAGS = frozenset({"--version", "--help", "-h"})


def default_output_dir() -> Path:
    """Return the default archive directory, ``~/log-archives``."""
    return Path.home() / "log-archives"


# --------------------------------------------------------------------------
# Backwards compatible argument normalisation
# --------------------------------------------------------------------------


def _first_positional(argv: Sequence[str]) -> str | None:
    """Return the first token in *argv* that is not an option or option value."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token.startswith("-") and token != "-":
            name = token.split("=", 1)[0]
            # ``--output=/tmp`` carries its value inline, so only the
            # space separated form consumes the next token.
            if name in _VALUE_OPTIONS and "=" not in token:
                index += 2
                continue
            index += 1
            continue
        return token
    return None


def normalise_argv(argv: Sequence[str]) -> list[str]:
    """Rewrite a legacy invocation into the subcommand form.

    ``log-archive /var/log -q`` becomes ``log-archive archive /var/log -q``.
    ``--version`` and ``--help`` are left alone so they keep working without a
    subcommand.
    """
    tokens = list(argv)
    if not tokens:
        return tokens
    first = _first_positional(tokens)
    if first is not None:
        return tokens if first in COMMANDS else ["archive", *tokens]
    if any(token in _TOPLEVEL_FLAGS for token in tokens):
        return tokens
    return ["archive", *tokens]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


class Printer:
    """Console output with ``--quiet`` and ``--verbose`` handled in one place.

    Informational lines are flushed as they are written, so redirecting stdout
    and stderr to separate files keeps messages in the order they happened.
    The original script printed its error before its progress banner because of
    stream buffering; this avoids that.
    """

    def __init__(
        self,
        *,
        quiet: bool = False,
        verbose: bool = False,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
    ) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr

    def info(self, message: str = "") -> None:
        """Print a normal progress line, unless quiet."""
        if self.quiet:
            return
        print(message, file=self.stdout, flush=True)

    def detail(self, message: str) -> None:
        """Print a line that only appears in verbose mode."""
        if self.quiet or not self.verbose:
            return
        print(message, file=self.stdout, flush=True)

    def warn(self, message: str) -> None:
        """Report a recoverable problem. Suppressed by ``--quiet``."""
        if self.quiet:
            return
        print(f"Warning: {message}", file=self.stderr, flush=True)

    def error(self, message: str) -> None:
        """Report a failure. Always shown, even when quiet."""
        print(f"{failure_mark(self.stderr)} Error: {message}", file=self.stderr, flush=True)

    def ok(self, message: str) -> None:
        """Print a success line with a status marker."""
        self.info(f"{success_mark(self.stdout)} {message}")


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Add options shared by every subcommand."""
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=None,
        help="suppress normal output; only errors are shown",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=None,
        help="show individual skips, active exclusions, and extra detail",
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        default=None,
        help="read configuration from PATH instead of the default locations",
    )
    parser.add_argument(
        "--no-config",
        action="store_true",
        default=None,
        help="ignore all configuration files",
    )


def _add_output_argument(parser: argparse.ArgumentParser) -> None:
    """Add the output directory option."""
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        default=None,
        help="directory to store archives in (default: ~/log-archives)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Create, verify, and manage compressed archives of log directories.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"{PROGRAM} v{__version__}",
        help="show the version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    archive = subparsers.add_parser(
        "archive",
        help="create an archive from a log directory",
        description="Create a timestamped compressed archive of a log directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    archive.add_argument("log_directory", help="log directory to archive")
    _add_output_argument(archive)
    archive.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=None,
        metavar="PATTERN",
        help=(
            "extra glob to exclude, repeatable. A pattern containing '/' is matched "
            "against the path relative to the source ('nginx/*.old'); otherwise it is "
            "matched against the base name at any depth ('*.log.1')"
        ),
    )
    archive.add_argument(
        "-X",
        "--no-default-excludes",
        action="store_true",
        default=None,
        help="do not apply the built-in exclusion list",
    )
    archive.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="report what would be archived without creating anything",
    )
    archive.add_argument(
        "--verify",
        action="store_true",
        default=None,
        help="re-open and fully read the archive after creating it",
    )
    archive.add_argument(
        "--checksum",
        action="store_true",
        default=None,
        help="write a SHA-256 checksum file next to the archive",
    )
    archive.add_argument(
        "--manifest",
        action="store_true",
        default=None,
        help="include a MANIFEST.txt describing the run inside the archive",
    )
    archive.add_argument(
        "--keep",
        type=int,
        metavar="N",
        default=None,
        help="after archiving, delete all but the N newest archives made by this tool",
    )
    archive.add_argument(
        "--compression",
        metavar="FORMAT",
        default=None,
        help=f"compression format: {', '.join(FORMATS)} (default: {DEFAULT_FORMAT})",
    )
    symlinks = archive.add_mutually_exclusive_group()
    symlinks.add_argument(
        "--follow-symlinks",
        dest="follow_symlinks",
        action="store_true",
        default=None,
        help="follow symlinks that resolve inside the source directory",
    )
    symlinks.add_argument(
        "--no-follow-symlinks",
        dest="follow_symlinks",
        action="store_false",
        default=None,
        help="store symlinks as links (default; never reads link targets)",
    )
    _add_common_arguments(archive)
    archive.set_defaults(handler=command_archive)

    verify = subparsers.add_parser(
        "verify",
        help="verify that an existing archive is intact",
        description=(
            "Re-open an archive, validate its compression and tar structure, and read "
            "every member. Detects truncation and corruption."
        ),
    )
    verify.add_argument("archive_path", help="archive to verify")
    _add_common_arguments(verify)
    verify.set_defaults(handler=command_verify)

    listing = subparsers.add_parser(
        "list",
        help="list archives created by this tool",
        description="List archives in the output directory, newest first.",
    )
    _add_output_argument(listing)
    listing.add_argument("--limit", type=int, metavar="N", default=None, help="show at most N")
    _add_common_arguments(listing)
    listing.set_defaults(handler=command_list)

    cleanup = subparsers.add_parser(
        "cleanup",
        help="delete old archives, keeping the newest N",
        description=(
            "Apply retention. Only files matching this tool's own naming scheme "
            f"({ARCHIVE_PREFIX}YYYYMMDD_HHMMSS.*) are ever considered for deletion."
        ),
    )
    _add_output_argument(cleanup)
    cleanup.add_argument(
        "--keep",
        type=int,
        metavar="N",
        default=None,
        help="number of archives to keep (default: 10)",
    )
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="show what would be deleted without deleting it",
    )
    _add_common_arguments(cleanup)
    cleanup.set_defaults(handler=command_cleanup)

    info = subparsers.add_parser(
        "info",
        help="show details about an archive",
        description="Display size, format, member count, checksum, and verification status.",
    )
    info.add_argument("archive_path", help="archive to inspect")
    info.add_argument(
        "--no-verify",
        action="store_true",
        default=None,
        help="skip the integrity check (faster, reports 'not checked')",
    )
    _add_common_arguments(info)
    info.set_defaults(handler=command_info)

    return parser


# --------------------------------------------------------------------------
# Option and path resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Command line options merged with configuration file values."""

    config: Config
    quiet: bool
    verbose: bool

    def resolve(self, cli_value: T | None, config_value: T | None, default: T) -> T:
        """Return the CLI value, else the config value, else *default*.

        Every option defaults to ``None`` in argparse precisely so that "the
        user did not pass this" stays distinguishable from "the user passed the
        default value".
        """
        if cli_value is not None:
            return cli_value
        if config_value is not None:
            return config_value
        return default


def _prepare(args: argparse.Namespace) -> tuple[Settings, Printer]:
    """Load configuration, apply it, and return a configured printer."""
    config = load_config(args.config, use_config=not bool(args.no_config))
    settings = Settings(
        config=config,
        quiet=bool(args.quiet),
        verbose=bool(args.verbose),
    )
    return settings, Printer(quiet=settings.quiet, verbose=settings.verbose)


def _resolve_source(raw: str) -> Path:
    """Validate the source directory and return its resolved path.

    Raises:
        UsageError: the path is missing or is not a directory.
        PermissionDeniedError: the directory cannot be listed.
    """
    source = expand_path(raw)
    if not source.exists():
        raise UsageError(f"{source} does not exist.")
    if not source.is_dir():
        raise UsageError(f"{source} is not a directory.")

    # os.access() consults the real uid, which under sudo is not the identity
    # that will actually read the files. Listing the directory is the honest
    # test, and it is the operation that will really happen.
    try:
        with os.scandir(source) as scan:
            next(iter(scan), None)
    except PermissionError as exc:
        raise PermissionDeniedError(
            f"No read permission on {source} ({exc.strerror}). Try: sudo {PROGRAM} {source}"
        ) from exc
    except OSError as exc:
        raise UsageError(f"Cannot read {source}: {exc}") from exc
    return source.resolve()


def _resolve_output(settings: Settings, raw: str | None) -> Path:
    """Decide on and create the output directory."""
    chosen = settings.resolve(raw, settings.config.output, str(default_output_dir()))
    out_dir = expand_path(str(chosen))
    if out_dir.exists() and not out_dir.is_dir():
        raise UsageError(f"Output path is not a directory: {out_dir}")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise raise_for_oserror(exc, out_dir, action="create directory") from exc
    return out_dir.resolve()


def _resolve_compression(settings: Settings, raw: str | None) -> CompressionFormat:
    """Pick the compression format from the command line or configuration."""
    return get_format(str(settings.resolve(raw, settings.config.compression, DEFAULT_FORMAT)))


def _build_rules(
    config: Config, no_default_excludes: bool | None, extra: Sequence[str] | None
) -> ExclusionRules:
    """Assemble exclusion rules from configuration plus command line.

    Raises:
        UsageError: an exclusion pattern is empty or otherwise invalid.
    """
    use_defaults = not bool(
        no_default_excludes if no_default_excludes is not None else config.no_default_excludes
    )
    patterns: list[str] = list(config.exclude)
    patterns.extend(extra or ())
    try:
        return ExclusionRules.build(patterns, use_defaults=use_defaults)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc


def _prune_paths(out_dir: Path, source: Path) -> list[Path]:
    """Return subtrees to keep out of the archive.

    When the output directory sits inside the source tree it is pruned
    entirely, which is what stops an archive from containing itself or a
    previous run's archive. Source logs are only skipped, never deleted.
    """
    try:
        out_dir.relative_to(source)
    except ValueError:
        return []
    return [out_dir]


@contextmanager
def _partial_archive(path: Path, printer: Printer) -> Iterator[None]:
    """Guarantee that an incomplete archive is never left on disk.

    One place handles every failure mode: handled errors, unexpected
    exceptions, and interrupts. This is the fix for the original tool, which
    left a truncated ``.tar.gz`` behind when it was killed part way through.
    """
    try:
        yield
    except BaseException:
        if remove_quietly(path):
            printer.info("Removing incomplete archive...")
            printer.ok("Cleanup completed.")
        raise


# --------------------------------------------------------------------------
# archive
# --------------------------------------------------------------------------


def command_archive(args: argparse.Namespace, printer: Printer) -> int:
    """Implement ``log-archive archive``."""
    settings, printer = _prepare(args)
    config = settings.config

    source = _resolve_source(args.log_directory)
    out_dir = _resolve_output(settings, args.output)
    rules = _build_rules(config, args.no_default_excludes, args.exclude)
    fmt = _resolve_compression(settings, args.compression)
    follow = bool(settings.resolve(args.follow_symlinks, config.follow_symlinks, False))
    want_verify = bool(settings.resolve(args.verify, config.verify, False))
    want_checksum = bool(settings.resolve(args.checksum, config.checksum, False))
    want_manifest = bool(settings.resolve(args.manifest, config.manifest, False))
    keep = settings.resolve(args.keep, config.keep, None)
    if keep is not None and int(keep) < 0:
        raise UsageError("--keep must be zero or greater.")

    prune = _prune_paths(out_dir, source)

    if args.dry_run:
        return _run_dry_run(source, out_dir, fmt, rules, follow, prune, config, printer)

    _print_header(printer, source, out_dir, fmt, rules, follow, config)

    log_file = out_dir / LOG_FILENAME
    logger = ArchiveLogger(
        log_file, verbose=printer.verbose, quiet=printer.quiet, stream=printer.stderr
    )
    logger.info(f"Starting archive of {source}")

    when = datetime.now()
    archive_path = reserve_unique_path(
        out_dir, f"{ARCHIVE_PREFIX}{when:%Y%m%d_%H%M%S}", fmt.extension
    )

    manifest = None
    if want_manifest:
        manifest = Manifest.collect(
            created=when, source=source, compression=fmt.label, excludes=rules.patterns
        )

    printer.detail("Scanning...")
    started = time.monotonic()
    try:
        with _partial_archive(archive_path, printer):
            stats = build_archive(
                source,
                archive_path,
                rules,
                fmt,
                follow_symlinks=follow,
                skip_paths=prune,
                manifest=manifest,
            )
    except OperationInterrupted:
        raise
    stats.duration = time.monotonic() - started

    stats.archive_size = archive_path.stat().st_size
    logger.info(f"Archive completed: {archive_path.name}")
    logger.info(f"Files archived: {stats.files_archived}")
    for item in stats.skipped_entries:
        logger.warning(f"Skipped {item.relative}: {item.reason}")
    for name in stats.excluded_samples:
        logger.debug(f"Excluded {name}")
    if stats.anomalies:
        logger.warning(f"{stats.anomalies} file(s) changed size while being archived")

    digest: str | None = None
    if want_checksum:
        try:
            digest, checksum_path = write_checksum_file(archive_path)
        except OSError as exc:
            raise ChecksumError(f"Cannot write checksum for {archive_path}: {exc}") from exc
        logger.info(f"Checksum written: {checksum_path.name}")

    if want_verify:
        result = verify_archive(archive_path)
        if not result.ok:
            remove_quietly(archive_path)
            if want_checksum:
                remove_quietly(archive_path.with_name(archive_path.name + ".sha256"))
            logger.error(f"Verification failed: {'; '.join(result.problems)}")
            raise VerificationError(
                f"Archive failed verification and was removed: {'; '.join(result.problems)}"
            )
        logger.info("Verification successful")
        if result.unsafe_members:
            # Advisory only. Absolute symlink targets are routine in /var/log;
            # this matters if the archive is ever extracted.
            printer.warn(
                f"{len(result.unsafe_members)} member(s) would be unsafe to extract; "
                "extract with care"
            )

    cleanup_failed = False
    removed: list[Path] = []
    if keep is not None:
        removed, cleanup_error = apply_cleanup(out_dir, int(keep))
        if cleanup_error is not None:
            cleanup_failed = True
            logger.warning(f"Cleanup incomplete: {cleanup_error.message}")
            printer.warn(f"Cleanup incomplete: {cleanup_error.message}")

    _print_success(
        printer,
        stats=stats,
        archive_path=archive_path,
        log_file=log_file,
        digest=digest,
        verified=want_verify,
        removed=removed,
    )
    return int(CleanupError.exit_code) if cleanup_failed else int(ExitCode.SUCCESS)


def _print_header(
    printer: Printer,
    source: Path,
    out_dir: Path,
    fmt: CompressionFormat,
    rules: ExclusionRules,
    follow: bool,
    config: Config,
) -> None:
    """Print the run header."""
    printer.info(f"{PROGRAM} v{__version__}")
    printer.info()
    printer.info(f"Source      {source}")
    printer.info(f"Destination {out_dir}")
    printer.info(f"Compression {fmt.label}")
    printer.info("Mode        archive")
    if follow:
        printer.info("Symlinks    follow (inside source only)")
    printer.info()
    printer.detail(f"Exclusions: {rules.describe()}")
    for path in config.sources:
        printer.detail(f"Config: {path}")


def _run_dry_run(
    source: Path,
    out_dir: Path,
    fmt: CompressionFormat,
    rules: ExclusionRules,
    follow: bool,
    prune: list[Path],
    config: Config,
    printer: Printer,
) -> int:
    """Print the dry run plan. Creates and modifies nothing on disk.

    The plan is computed first and printed afterwards, so a single directory
    scan is enough: the numbers reported are exactly the numbers a real run
    would produce.
    """
    stats = plan_archive(
        source, rules, follow_symlinks=follow, skip_paths=prune, stats=ArchiveStats()
    )

    printer.info("DRY RUN")
    printer.info()
    printer.info("Source:")
    printer.info(f"  {source}")
    printer.info()
    printer.info("Destination:")
    printer.info(f"  {out_dir}")
    printer.info()
    printer.info("Compression:")
    printer.info(f"  {fmt.label}")
    printer.info()
    printer.info("Would archive:")
    printer.info(f"  {plural(stats.files_archived, 'file')}")
    printer.info(f"  {plural(stats.directories_archived, 'directory', 'directories')}")
    if stats.symlinks_archived:
        printer.info(f"  {plural(stats.symlinks_archived, 'symlink')}")
    printer.info()
    printer.info("Would exclude:")
    printer.info(f"  {plural(stats.files_excluded, 'file')}")
    for name in stats.excluded_samples:
        printer.detail(f"    {name}")
    if stats.skipped_entries:
        printer.info()
        printer.info("Would skip:")
        printer.info(f"  {plural(stats.files_skipped, 'file')}")
        for item in stats.skipped_entries:
            printer.info(f"    {item.relative}: {item.reason}")
    printer.info()
    printer.info("Estimated size:")
    printer.info(f"  {human_size(stats.original_size)}")
    printer.info()
    printer.detail(f"Exclusions: {rules.describe()}")
    for path in config.sources:
        printer.detail(f"Config: {path}")
    printer.info()
    # The plan is built without opening any file, because reading every log is
    # the expensive part. Say so, or the skip count looks like a bug when it
    # differs from a real run.
    printer.detail(
        "Note: a dry run does not open files, so unreadable ones cannot be "
        "detected here and may also be skipped by a real run."
    )
    printer.info()
    printer.info("No files were created or modified.")
    return int(ExitCode.SUCCESS)


def _print_success(
    printer: Printer,
    *,
    stats: ArchiveStats,
    archive_path: Path,
    log_file: Path,
    digest: str | None,
    verified: bool,
    removed: list[Path],
) -> None:
    """Print the end-of-run report."""
    printer.info()
    printer.ok("Archive completed")
    printer.info()
    printer.info(format_report(stats, archive_name=archive_path.name))
    if digest is not None:
        printer.info()
        printer.info(f"SHA-256     {digest}")
        printer.info(f"Checksum    {archive_path.name}.sha256")
    if verified:
        printer.info("Verified    yes")
    if removed:
        printer.info()
        printer.info(f"Removed     {len(removed)} old archive file(s)")
        for path in removed:
            printer.detail(f"    removed {path.name}")
    printer.info()
    printer.info(f"Log         {log_file}")


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def command_verify(args: argparse.Namespace, printer: Printer) -> int:
    """Implement ``log-archive verify``."""
    _settings, printer = _prepare(args)

    archive_path = expand_path(args.archive_path)
    if not archive_path.is_file():
        raise UsageError(f"{archive_path} is not a file.")

    printer.info("Verifying archive...")
    result = verify_archive(archive_path)
    if not result.ok:
        for problem in result.problems:
            printer.error(problem)
        return int(ExitCode.VERIFICATION)

    size = archive_path.stat().st_size
    ratio = 0.0
    if result.uncompressed_size > 0:
        ratio = max(0.0, min(100.0, (1 - size / result.uncompressed_size) * 100))
    printer.ok(f"{result.stream} stream valid")
    printer.ok("tar structure valid")
    printer.ok(f"{result.members} members checked")
    printer.info(f"Size        {human_size(size)}")
    printer.info(f"Ratio       {ratio:.1f}%")
    if result.unsafe_members:
        printer.warn(
            f"{len(result.unsafe_members)} member(s) would be unsafe to extract "
            "(absolute symlink targets are normal in /var/log)"
        )
    printer.ok("Archive verification successful")
    return int(ExitCode.SUCCESS)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


def command_list(args: argparse.Namespace, printer: Printer) -> int:
    """Implement ``log-archive list``."""
    settings, printer = _prepare(args)

    out_dir = _resolve_output(settings, args.output)
    archives = discover(out_dir)
    limit = args.limit
    if limit is not None:
        if limit < 0:
            raise UsageError("--limit must be zero or greater.")
        archives = archives[: int(limit)]

    if not archives:
        printer.info(f"No archives found in {out_dir}")
        return int(ExitCode.SUCCESS)

    width = max(len(item.path.name) for item in archives)
    if not printer.quiet:
        printer.info(f"{'ARCHIVE'.ljust(width)}  {'CREATED':19}  {'SIZE':>10}  SHA256")
    for item in archives:
        try:
            size = human_size(item.path.stat().st_size)
        except OSError:
            size = "?"
        digest = "yes" if item.has_checksum else "-"
        if printer.quiet:
            printer.info(item.path.name)
        else:
            stamp = item.created.strftime("%Y-%m-%d %H:%M:%S")
            printer.info(f"{item.path.name.ljust(width)}  {stamp:19}  {size:>10}  {digest}")
    printer.info()
    printer.info(f"{len(archives)} archive(s) in {out_dir}")
    return int(ExitCode.SUCCESS)


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------


def command_cleanup(args: argparse.Namespace, printer: Printer) -> int:
    """Implement ``log-archive cleanup``."""
    settings, printer = _prepare(args)

    out_dir = _resolve_output(settings, args.output)
    keep = int(settings.resolve(args.keep, settings.config.keep, 10))
    if keep < 0:
        raise UsageError("--keep must be zero or greater.")

    if args.dry_run:
        plan = plan_cleanup(out_dir, keep)
        printer.info("Cleanup preview")
        printer.info()
        printer.info(f"Archives found: {len(plan.found)}")
        printer.info(f"Keeping:        {len(plan.keep)}")
        printer.info(f"Would remove:   {len(plan.remove)}")
        for archive in plan.remove:
            printer.info(f"  {archive.path.name}")
        printer.info()
        printer.info("No files were deleted.")
        return int(ExitCode.SUCCESS)

    deleted, error = apply_cleanup(out_dir, keep)
    for path in deleted:
        printer.detail(f"removed {path.name}")
    if error is not None:
        printer.error(error.message)
        return int(ExitCode.CLEANUP)
    printer.ok(f"Removed {len(deleted)} old archive file(s); kept {keep}")
    return int(ExitCode.SUCCESS)


# --------------------------------------------------------------------------
# info
# --------------------------------------------------------------------------


def command_info(args: argparse.Namespace, printer: Printer) -> int:
    """Implement ``log-archive info``."""
    _settings, printer = _prepare(args)

    archive_path = expand_path(args.archive_path)
    if not archive_path.is_file():
        raise UsageError(f"{archive_path} is not a file.")

    printer.info("Archive Information")
    printer.info()
    printer.info("File:")
    printer.info(f"  {archive_path.name}")
    printer.info()
    printer.info("Size:")
    printer.info(f"  {human_size(archive_path.stat().st_size)}")
    printer.info()
    printer.info("Created:")
    stamp = datetime.fromtimestamp(archive_path.stat().st_mtime)
    printer.info(f"  {stamp.strftime('%Y-%m-%d %H:%M:%S')}")
    printer.info()
    printer.info("Format:")
    printer.info(f"  tar.{describe_format(archive_path)}")

    try:
        members, files, size = count_members(archive_path)
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        printer.error(f"Cannot read archive: {exc}")
        return int(ExitCode.ARCHIVE)
    printer.info()
    printer.info("Members:")
    printer.info(f"  {members} ({files} files, {human_size(size)} uncompressed)")

    printer.info()
    printer.info("Checksum:")
    checksum_path = archive_path.with_name(archive_path.name + ".sha256")
    if checksum_path.is_file():
        try:
            printer.info(f"  {read_checksum_file(checksum_path)}")
        except (OSError, IndexError) as exc:
            printer.info(f"  unreadable ({exc})")
    else:
        printer.info("  not present")

    state, body = _read_manifest(archive_path)
    printer.info()
    printer.info("Manifest:")
    printer.info(f"  {state}")
    if body and printer.verbose:
        printer.info()
        for line in body.rstrip().splitlines():
            printer.info(f"  {line}")

    printer.info()
    printer.info("Verification:")
    if args.no_verify:
        printer.info("  not checked (--no-verify)")
        return int(ExitCode.SUCCESS)
    result = verify_archive(archive_path)
    if result.ok:
        printer.info("  passed")
        return int(ExitCode.SUCCESS)
    printer.info("  FAILED")
    for problem in result.problems:
        printer.info(f"    {problem}")
    return int(ExitCode.VERIFICATION)


def _read_manifest(archive_path: Path) -> tuple[str, str | None]:
    """Return a short manifest status and, when present, its text."""
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            for member in tar:
                if member.name.rsplit("/", 1)[-1] == "MANIFEST.txt":
                    stream = tar.extractfile(member)
                    if stream is None:
                        return "unreadable", None
                    with stream:
                        return "present", stream.read().decode("utf-8", "replace")
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return "unreadable", None
    return "not present", None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code.

    Never raises: every expected failure becomes a message plus an exit code,
    because this is the top of a command line tool and a traceback is not a
    useful thing to hand to a cron job.
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    printer = Printer()

    try:
        args = parser.parse_args(normalise_argv(raw))
    except SystemExit as exc:
        # argparse has already written its own message (--help, --version, or
        # a usage error).
        return int(exc.code) if exc.code is not None else int(ExitCode.USAGE)

    handler: Callable[[argparse.Namespace, Printer], int] | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return int(ExitCode.USAGE)

    try:
        with interruptible():
            return handler(args, printer)
    except OperationInterrupted as exc:
        printer.info()
        printer.error(f"Archive interrupted by {exc.signal_name}.")
        return exc.exit_code
    except KeyboardInterrupt:
        printer.info()
        printer.error("Archive interrupted.")
        return int(ExitCode.INTERRUPTED)
    except LogArchiveError as exc:
        printer.error(exc.message)
        return int(exc.exit_code)
    except BrokenPipeError:
        # ``log-archive ... | head`` closes the pipe early. Exit quietly.
        return int(ExitCode.SUCCESS)
    except OSError as exc:
        printer.error(str(exc))
        return int(ExitCode.ERROR)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
