# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-30

First public release. This replaces the original single-file `log-archive.py`
script.

### Fixed

Bugs found in the original implementation, each reproduced before being fixed.

- **One unreadable file destroyed the whole backup.** The script aborted on the
  first `EACCES` and then *deleted the partial archive*, so a single root-owned
  file in `/var/log` meant you got no backup at all. Unreadable files are now
  recorded, counted, skipped, and reported; the rest of the tree is still
  archived.
- **A failure to write the activity log crashed the program.** `write_log` was
  unguarded, so a read-only output directory or a full disk turned a clean
  error message into a traceback that hid the real cause. Log writes are now
  best effort and never mask the original error.
- **Two runs in the same second silently destroyed the first archive.** The
  timestamped filename was opened with `w:gz`, truncating any existing file.
  Names are now reserved atomically with `O_EXCL`, producing
  `..._01.tar.gz` on collision.
- **The output directory could be archived into itself.** With
  `--no-default-excludes`, each run embedded the previous run's archive,
  growing without bound. The output directory is now pruned from the walk.
- **Interrupting the tool left a truncated archive behind.** `Ctrl+C` skipped
  the cleanup path entirely, leaving a partial `.tar.gz` that looked like a
  valid backup. `SIGINT` and `SIGTERM` now both remove the incomplete archive
  and exit `130`.
- **Errors appeared before progress messages.** `stdout` was block buffered
  while `stderr` was not, so `log-archive ... > out.txt` produced an
  out-of-order transcript. Output is now flushed line by line.
- **The tool failed to import at all on Python 3.11 to 3.13.** The zstd
  capability probe called `importlib.util.find_spec("compression.zstd")`, which
  imports the *parent* package first and raises `ModuleNotFoundError` when that
  parent does not exist, rather than returning `None`. The whole `compression`
  package is absent before 3.14, so every `log-archive` invocation on a
  supported interpreter died during import. The probe now catches it, and the
  capability check lives in one place shared with verification.
- **A log shrinking mid-read produced a structurally corrupt archive.**
  `tarfile` writes the header before the data, so a truncated read left every
  following member misaligned. This is now detected and the archive is
  discarded with an explanation.

### Added

- `archive`, `verify`, `list`, `cleanup`, and `info` subcommands, while
  `log-archive /var/log` continues to work exactly as before.
- `--dry-run`: a full plan with no writes, sharing the real engine's walker so
  the numbers cannot drift from what a real run does.
- `--verify`: two independent integrity passes, the compression stream and
  every member body. A failed verification removes the archive.
- `--checksum`: streamed SHA-256 in `sha256sum -c` compatible format.
- `--manifest`: an in-archive `MANIFEST.txt` with host, tool version, counts,
  and active exclusions. Never contains log contents.
- `--keep N` and `cleanup --keep N` retention, restricted by a strict filename
  pattern so foreign files can never be deleted.
- `--compression` with `gzip`, `gzip-fast`, `gzip-best`, `none`, `xz`,
  `bzip2`, and `zstd` where the interpreter supports it.
- `--manifest`, `--follow-symlinks` / `--no-follow-symlinks`, `--verbose`,
  `--version`, `--config`, and `--no-config`.
- Optional TOML configuration from `/etc/log-archive/config.toml` and
  `~/.config/log-archive/config.toml`, with the command line always winning.
- Documented, stable exit codes: `0`, `1`, `2`, `3`, `4`, `5`, `6`, `7`, `8`,
  `130`.
- Statistics: files, directories, symlinks, excluded, skipped, original size,
  compressed size, ratio, and duration.
- A `src/` layout package with type hints, docstrings, `py.typed`, and a
  `log-archive` console script.
- 206 pytest tests, including a dedicated security suite.
- `ruff`, `mypy --strict`, and GitHub Actions CI.
- Documentation: README, usage, architecture, security, contributing, and
  optional systemd units.

### Changed

- Exclusions are matched more predictably: a pattern containing `/` is matched
  against the source-relative path, otherwise against the base name at any
  depth. This makes `nginx/*.old` and `*.log.1` behave as documented.
- The built-in exclusion list is unchanged, for compatibility.
- Source files are streamed in 1 MiB slices with one directory scan per
  directory, so memory does not grow with archive size.
- Reading a regular file uses `O_NOFOLLOW`, closing the window in which a file
  could be swapped for a symlink between scanning and reading.

### Security

- The source tree is never modified, moved, or deleted.
- Symlinks are stored as links by default and never dereferenced.
  `--follow-symlinks` still cannot leave the source tree.
- Sockets, FIFOs, and device nodes are skipped rather than read; a FIFO in
  particular would block forever.
- Archives are created with mode `0600`.
- No `subprocess`, no `os.system`, no shell anywhere in the package.
- `is_safe_member()` flags members that would be dangerous to extract
  (absolute paths, `..` traversal, escaping links, device nodes).

### Removed

- The single-file `log-archive.py`. Replaced by the `log_archive` package; the
  command line interface is unchanged.
