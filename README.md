# log-archive

A professional Linux command-line utility for safely compressing, verifying,
and managing system log directories into timestamped archives.

`log-archive` is for Linux administrators, developers, and students who need a
simple, reliable way to back up log directories such as `/var/log`.

---

## Table of Contents

- [Purpose](#purpose)
- [Features](#features)
- [Screenshots](#screenshots)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Basic Usage](#basic-usage)
- [Commands](#commands)
- [Dry Run](#dry-run)
- [Exclusions](#exclusions)
- [Verification](#verification)
- [SHA-256 Checksum](#sha-256-checksum)
- [Manifest](#manifest)
- [Retention](#retention)
- [Archive Information](#archive-information)
- [Quiet and Verbose Modes](#quiet-and-verbose-modes)
- [Archive Location and Naming](#archive-location-and-naming)
- [Configuration](#configuration)
- [Compression Formats](#compression-formats)
- [Exit Codes](#exit-codes)
- [Security and Safety](#security-and-safety)
- [Systemd (Optional)](#systemd-optional)
- [Architecture](#architecture)
- [Development](#development)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [License](#license)

---

## Purpose

Instead of manually running commands such as:

```bash
tar -czf logs.tar.gz /var/log
```

you can use:

```bash
log-archive /var/log
```

The application automatically:

* Creates a timestamped `.tar.gz` archive
* Stores archives in a dedicated archive directory
* Excludes unnecessary or already-compressed files
* Handles common Linux log files safely
* Records archive operations in a log
* Provides readable archive statistics
* Prevents accidental overwriting of existing archives
* Supports verification and SHA-256 checksums
* Supports dry-run operation
* Supports archive retention and cleanup

---

## Features

| Feature              | Description |
| -------------------- | ----------- |
| Log Archiving        | Compress log directories into `.tar.gz` archives |
| Timestamped Archives | Unique archive names; an existing archive is never overwritten |
| Default Exclusions   | Skips sockets, pid files, the journal, and already-compressed logs |
| Custom Exclusions    | Add your own glob patterns |
| Dry Run              | Preview what will be archived without creating anything |
| Verification         | Confirms an archive is intact, not merely present |
| SHA-256              | Generate checksums for archive integrity |
| Manifest             | Store archive metadata inside the archive |
| Retention            | Keep only a selected number of recent archives |
| Safe Symlinks        | Never dereferences links outside the source directory |
| Error Handling       | Handles permissions, interrupts, disk-full, and archive failures |
| Logging              | Records archive operations, metadata only |
| Configuration        | Optional TOML file; the command line always wins |
| Compression Formats  | `gzip`, `gzip-fast`, `gzip-best`, `none`, `xz`, `bzip2`, `zstd` |
| CLI                  | Designed for terminals and shell automation |
| Linux Friendly       | Designed primarily for Linux environments |

No runtime dependencies beyond the Python standard library.

---

## Screenshots

### Help and command overview

![log-archive CLI help](docs/screenshots/01-help.png)

### Creating an archive

Creates a timestamped archive and verifies it in one command:

![log-archive basic archive](docs/screenshots/02-archive.png)

### Dry run

Shows what would be archived, excluded, and skipped, without writing anything:

![log-archive dry run](docs/screenshots/03-dry-run.png)

### Verification

Checks the compression stream and every member, so silent corruption is caught:

![log-archive verification](docs/screenshots/04-verify.png)

### SHA-256 checksum

![log-archive checksum](docs/screenshots/05-checksum.png)

### Archive information

![log-archive information](docs/screenshots/06-info.png)

### Retention and cleanup

A dry run, then the real thing. Only archives created by `log-archive` are ever
considered:

![log-archive cleanup](docs/screenshots/07-cleanup.png)

> These screenshots show real captured output. The demo log tree is entirely
> synthetic and sandbox paths are rewritten for presentation, so no real
> hostname, username, IP address, or log content appears in them. Regenerate
> them with `python scripts/generate_screenshots.py`.

---

## Requirements

* Linux
* Python 3.11 or newer
* Standard Python libraries only

For development, the project additionally uses `pytest`, `ruff`, `mypy`, and
`build`.

---

## Installation

Clone the repository:

```bash
git clone https://github.com/toms1010/log-archive
cd log-archive
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the project:

```bash
pip install -e .
```

Check the installation:

```bash
log-archive --version     # log-archive v1.0.0
log-archive --help
```

For development, install the tooling too:

```bash
pip install -e ".[dev]"
```

### System-wide installation

Prefer `pipx`, which keeps the tool out of the system Python:

```bash
python3 -m build
pipx install dist/log_archive-1.0.0-py3-none-any.whl
```

---

## Quick Start

Archive `/var/log`:

```bash
log-archive /var/log
```

Because `/var/log` contains files that require elevated permissions, you may
need:

```bash
sudo log-archive /var/log
```

Archives are stored in `~/log-archives/` by default:

```text
~/log-archives/
├── logs_archive_20260930_075401.tar.gz
└── archive.log
```

---

## Basic Usage

Archive a log directory:

```bash
log-archive /var/log
log-archive ~/my-logs
```

Choose an output directory:

```bash
log-archive /var/log --output ~/backups
log-archive /var/log -o ~/backups
```

Run with sudo, writing somewhere root owns:

```bash
sudo log-archive /var/log -o /var/backups/log-archives
```

> The tool never needs write access to `/var/log` and never modifies it. Only
> read access is required, which in practice means running as root.

---

## Commands

```text
log-archive archive SOURCE     create an archive
log-archive verify ARCHIVE     check an archive's integrity
log-archive list               list archives in the output directory
log-archive cleanup            apply retention
log-archive info ARCHIVE       show details about an archive
```

A bare source path is shorthand for the `archive` command, so these are
identical:

```bash
log-archive /var/log
log-archive archive /var/log
```

The tool can also be run as a module:

```bash
python -m log_archive /var/log
```

### `archive` options

| Option | Description |
| --- | --- |
| `SOURCE` | Log directory to archive. Read only, never modified. |
| `-o`, `--output PATH` | Destination directory. Default `~/log-archives`. |
| `-x`, `--exclude PATTERN` | Extra exclusion glob. Repeatable. |
| `-X`, `--no-default-excludes` | Ignore the built-in exclusion list. |
| `--dry-run` | Report the plan; create nothing. |
| `--verify` | Verify the new archive; remove it if verification fails. |
| `--checksum` | Write a `.sha256` sidecar. |
| `--manifest` | Include `MANIFEST.txt` inside the archive. |
| `--keep N` | After archiving, keep only the newest N. |
| `--compression FORMAT` | See [Compression Formats](#compression-formats). |
| `--follow-symlinks` | Follow links resolving inside the source directory. |
| `--no-follow-symlinks` | Store links as links. This is the default. |
| `-q`, `--quiet` | Show only errors. |
| `-v`, `--verbose` | Show skips, active exclusions, and extra detail. |
| `-c`, `--config PATH` | Read configuration from `PATH`. |
| `--no-config` | Ignore all configuration files. |

---

## Dry Run

Preview an operation without creating anything:

```bash
log-archive /var/log --dry-run
```

```text
DRY RUN

Source:
  /var/log

Destination:
  /home/user/log-archives

Compression:
  gzip

Would archive:
  184 files
  22 directories
  4 symlinks

Would exclude:
  13 files

Would skip:
  1 file
    audit.pipe: FIFO

Estimated size:
  482.7 MB

No files were created or modified.
```

A dry run does not open any file, because reading every log is the expensive
part. Unreadable files therefore cannot be listed here, and a real run may skip
more than the plan shows. `--verbose` prints that note explicitly.

Dry run is recommended before archiving a large directory or using custom
exclusion rules. It uses the same walker as a real run, so the numbers cannot
disagree with what would really happen.

One limitation: it cannot report files that would turn out to be unreadable,
because detecting those means opening every file.

---

## Exclusions

Default exclusions:

```text
journal
*.sock
*.pid
wtmp
btmp
*.gz
*.xz
*.zst
```

These avoid unnecessary processing of systemd journal directories, sockets,
temporary PID files, binary login records, and files that are already
compressed.

> **A note on `*.gz`, `*.xz`, and `*.zst`.** Rotated logs are often the ones
> you most want to keep, and they are already compressed, so storing them is
> nearly free. These defaults are kept for compatibility. For long-retention
> archiving, consider dropping them:
>
> ```bash
> log-archive /var/log -X -x journal -x '*.sock' -x '*.pid' -x wtmp -x btmp
> ```

### Add custom exclusions

```bash
log-archive /var/log --exclude "*.old"
log-archive /var/log -x "*.old" -x "*.backup"
log-archive /var/log -x "journal/**" -x "nginx/*.old"
```

### How patterns match

| Pattern | Contains `/` | Matched against | Example |
| --- | --- | --- | --- |
| `*.log.1` | no | base name, any depth | `app.log.1`, `deep/a.log.1` |
| `nginx/*.old` | yes | path relative to the source | `nginx/access.old` only |
| `journal/**` | yes | path relative to the source | everything under `journal/` |

* `*` and `**` both cross `/`.
* Path patterns are anchored at the source root, never at the filesystem root.
* Excluding a directory prunes its whole subtree.
* Name patterns match the whole base name, so `*.log` does not match
  `app.log.1`.

### Disable default exclusions

```bash
log-archive /var/log --no-default-excludes
log-archive /var/log -X
```

Use carefully: it may increase archive size and processing time, and it will
reintroduce the journal.

---

## Verification

Create and verify in one step:

```bash
log-archive /var/log --verify
```

Or verify an existing archive:

```bash
log-archive verify ~/log-archives/logs_archive_20260930_075401.tar.gz
```

```text
Verifying archive...

✓ gzip stream valid
✓ tar structure valid
✓ 184 members checked
Size        71.4 MB
Ratio       85.2%
✓ Archive verification successful
```

Verification performs two independent passes:

1. Decodes the entire compression stream, forcing the codec's own integrity
   check (gzip CRC32 and length) and catching truncation.
2. Walks every tar member and reads every file body, proving the framing is
   consistent.

The first pass matters. `tarfile` stops at the end-of-archive marker and never
reads the gzip trailer, so simply listing members would accept an archive
corrupted halfway through.

If verification fails during `--verify`, the archive **and its checksum are
removed** and the tool exits `5`. It will not leave behind something that looks
like a good backup.

---

## SHA-256 Checksum

```bash
log-archive /var/log --checksum
```

Produces:

```text
logs_archive_20260930_075401.tar.gz
logs_archive_20260930_075401.tar.gz.sha256
```

The sidecar is in standard `sha256sum` format, so any tool that understands it
works:

```bash
cd ~/log-archives
sha256sum -c logs_archive_20260930_075401.tar.gz.sha256
# logs_archive_20260930_075401.tar.gz: OK
```

A checksum proves the file has not changed since it was written. It does not
prove the archive was correct when written, which is why `--verify` exists as a
separate step.

---

## Manifest

```bash
log-archive /var/log --manifest
```

Adds `MANIFEST.txt` inside the archive:

```text
LOG-ARCHIVE MANIFEST
===================

Version:     1.0
Tool:        log-archive 1.0.0
Created:     2026-09-30 07:54:01
Hostname:    web-01
User:        root
Source:      /var/log
Compression: gzip
Kernel:      6.8.0-45-generic
System:      Linux
Python:      3.12.3
Files:       184
Excluded:
  journal
  *.sock
  *.pid
  wtmp
  btmp
  *.gz
  *.xz
  *.zst
```

The manifest never contains log contents. The manifest is stored as the
archive's final member, which is what lets it report an exact file count
without a second directory scan. Tar members are order independent, so
extraction is unaffected.

---

## Retention

Keep only the newest 10 archives:

```bash
log-archive cleanup --keep 10
```

Preview without deleting:

```bash
log-archive cleanup --keep 10 --dry-run
```

```text
Cleanup preview

Archives found: 18
Keeping:        10
Would remove:   8
  logs_archive_20260930_070112.tar.gz
  ...

No files were deleted.
```

Retention only targets files this tool created, matched by a strict pattern:

```text
logs_archive_YYYYMMDD_HHMMSS.tar.gz
logs_archive_YYYYMMDD_HHMMSS_01.tar.gz
```

A hand-made `backup.tar.gz`, a `notes.txt`, or an archive from another tool in
the same directory is invisible to cleanup and can never be deleted. Checksum
sidecars are removed together with their archive.

You can also apply retention as part of an archive run:

```bash
log-archive /var/log --keep 30
```

### List archives

```bash
log-archive list
```

```text
ARCHIVE                              CREATED                    SIZE  SHA256
logs_archive_20260930_075401.tar.gz  2026-09-30 07:54:01      71.4 MB  yes
logs_archive_20260930_081530.tar.gz  2026-09-30 08:15:30      72.0 MB  yes

2 archive(s) in /home/user/log-archives
```

---

## Archive Information

```bash
log-archive info ~/log-archives/logs_archive_20260930_075401.tar.gz
```

```text
Archive Information

File:
  logs_archive_20260930_075401.tar.gz

Size:
  71.4 MB

Created:
  2026-09-30 07:54:01

Format:
  tar.gz

Members:
  206 (184 files, 482.7 MB uncompressed)

Checksum:
  8d7a1f3c9e2b4a6d8f0c1e3b5a7d9f2c4e6a8b0d2f4c6e8a0b2d4f6c8e0a2b4d

Manifest:
  present

Verification:
  passed
```

Pass `--no-verify` to skip the full read, which is much faster on a large
archive.

---

## Quiet and Verbose Modes

For scripts and automated jobs, `--quiet` shows only errors:

```bash
log-archive /var/log --quiet
```

```bash
if log-archive /var/log --quiet; then
    echo "Log archive successful"
else
    echo "Log archive failed"
fi
```

Skipped files are always recorded in `archive.log`, even when quiet.

`--verbose` shows active exclusions, individual skips, and per-step detail:

```bash
log-archive /var/log --verbose
```

Output contains no ANSI escape sequences, so it is safe to redirect into a file.
Under a non-UTF-8 terminal (`LC_ALL=C`) status markers degrade from `✓` to
`[ok]` rather than crashing.

---

## Archive Location and Naming

Default destination:

```text
~/log-archives/
```

```text
~/log-archives/
├── logs_archive_20260930_075401.tar.gz
├── logs_archive_20260930_081530.tar.gz
├── logs_archive_20260930_090201.tar.gz
└── archive.log
```

Change it with `--output` or `-o`.

Archive names are timestamp based:

```text
logs_archive_YYYYMMDD_HHMMSS.tar.gz
```

The name is reserved atomically before writing begins, so an existing archive is
**never** overwritten. Two runs in the same second produce
`..._01.tar.gz` and `..._02.tar.gz` rather than destroying each other.

---

## Configuration

Optional TOML, read from these locations in order of increasing priority:

1. `/etc/log-archive/config.toml` (system)
2. `~/.config/log-archive/config.toml` (user)
3. `--config PATH`
4. Command line flags, which always win

```toml
output = "~/log-archives"
compression = "gzip"
verify = true
checksum = true
manifest = true
follow_symlinks = false
keep = 10

exclude = [
    "journal",
    "*.sock",
    "*.pid",
    "*.old",
]
```

Unknown keys and wrong value types are hard errors (exit `6`) rather than
silent no-ops, so a typo is reported instead of quietly ignored. Use
`--no-config` to ignore configuration entirely.

---

## Compression Formats

| Name | Extension | Notes |
| --- | --- | --- |
| `gzip` | `.tar.gz` | Default, level 6 |
| `gzip-fast` | `.tar.gz` | Level 1, fastest and largest |
| `gzip-best` | `.tar.gz` | Level 9, slowest and smallest |
| `none` | `.tar` | Uncompressed |
| `xz` | `.tar.xz` | Smallest, slowest |
| `bzip2` | `.tar.bz2` | |
| `zstd` | `.tar.zst` | Requires Python 3.14+ |

```bash
log-archive /var/log --compression xz
log-archive /var/log --compression gzip-fast
```

---

## Exit Codes

Stable, and part of the public contract.

| Code | Meaning |
| ---: | --- |
| `0` | Success |
| `1` | General error |
| `2` | Invalid command-line usage |
| `3` | Permission error |
| `4` | Archive error, including out of disk space |
| `5` | Verification failure |
| `6` | Configuration error |
| `7` | Checksum error |
| `8` | Cleanup error |
| `130` | Interrupted by SIGINT or SIGTERM |

```bash
log-archive /var/log --verify && echo "Backup successful"
```

---

## Security and Safety

Log files can contain sensitive information, including usernames, IP addresses,
system information, application data, and authentication-related information.
Treat generated archives as sensitive files.

Archives and `archive.log` are created with mode `0600` regardless of your
umask, so no manual `chmod` is needed. Do not upload system log archives
publicly unless their contents have been reviewed.

`log-archive` is designed to:

* Never modify or delete source logs
* Never dereference symlinks by default, and never leave the source tree even
  with `--follow-symlinks`
* Read regular files with `O_NOFOLLOW`, so a file swapped for a symlink between
  the scan and the read is refused
* Skip sockets, FIFOs, and device files rather than reading them
* Prevent accidental archive recursion and self-archiving
* Never overwrite an existing archive
* Use no shell commands at all
* Remove incomplete archives after any failure, including `Ctrl+C`
* Never place log contents in its own log or in the manifest

Full detail: [SECURITY.md](SECURITY.md) and [docs/security.md](docs/security.md).

The tool does not extract archives; that is a deliberate design choice, since
extraction is where tar path traversal attacks live.

---

## Systemd (Optional)

Example units are in [`packaging/systemd/`](packaging/systemd/). They are not
installed or enabled automatically.

```bash
sudo mkdir -p /var/backups/log-archives
sudo cp packaging/systemd/log-archive.service /etc/systemd/system/
sudo cp packaging/systemd/log-archive.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now log-archive.timer

systemctl list-timers log-archive.timer
journalctl -u log-archive.service
```

For a weekly schedule:

```bash
sudo systemctl edit log-archive.timer
```

```ini
[Timer]
OnCalendar=weekly
Persistent=true
```

---

## Architecture

Sixteen small modules, no framework, no plugin system, and one rule that shapes
everything else: **the source tree is untrusted input.** `log-archive` runs as
root over directories full of output from daemons you do not control, so the
interesting design decisions are all about what it refuses to do.

### Module layout

Dependencies point one way only: the CLI knows about the engine, the engine
knows about policy, and the leaf modules know about nothing.

```mermaid
flowchart TD
    subgraph iface["Interface"]
        MAIN["__main__.py<br/>python -m log_archive"]
        CLI["cli.py<br/>argparse, subcommands, exit codes"]
    end
    subgraph engine["Archive engine"]
        ARCH["archive.py<br/>TreeScanner, streaming writer"]
    end
    subgraph decisions["Policy, resolved at runtime"]
        EXCL["exclusions.py<br/>glob rules"]
        COMP["compression.py<br/>format table"]
        CONF["config.py<br/>TOML, validated"]
    end
    subgraph support["Support"]
        STATS["statistics.py<br/>counters, report text"]
        VERIF["verification.py<br/>integrity, extraction safety"]
        RETAIN["retention.py<br/>name-matched cleanup"]
        SUM["checksum.py"]
        MAN["manifest.py"]
        LOG["logging_utils.py"]
        SIG["signals.py"]
        UTIL["utils.py<br/>errors, exit codes, formatting"]
    end
    MAIN --> CLI
    CLI --> ARCH
    CLI --> CONF
    CLI --> STATS
    CLI --> RETAIN
    CLI --> SUM
    CLI --> VERIF
    CLI --> SIG
    CLI --> LOG
    ARCH --> EXCL
    ARCH --> COMP
    ARCH --> STATS
    ARCH --> MAN
    ARCH --> UTIL
    VERIF --> COMP
    VERIF --> UTIL
    RETAIN --> UTIL
    LOG --> UTIL
    SIG --> UTIL
    CONF --> COMP
```

<details>
<summary>Same map as plain text</summary>

```text
__main__.py  ->  cli.py  ->  archive.py  ->  exclusions.py
                                  |        ->  compression.py
                                  |        ->  statistics.py
                                  |        ->  manifest.py
                                  |        ->  utils.py
                            ->  verification.py, retention.py, checksum.py
                            ->  config.py, logging_utils.py, signals.py
```

</details>

### One archive run

```mermaid
flowchart TD
    A["argv"] --> B["normalise_argv<br/>legacy form becomes archive"]
    B --> C["argparse"]
    C --> D["load_config<br/>system, then user, then explicit, CLI wins"]
    D --> E["validate source<br/>exists, is a directory, is listable"]
    E --> F["create output directory"]
    F --> G["resolve compression and exclusion rules"]
    G --> H{"output inside source?"}
    H -- yes --> I["prune that subtree<br/>no self-archiving"]
    H -- no --> J["reserve unique name<br/>O_EXCL, never overwrite"]
    I --> J
    J --> K["build_archive, wrapped in<br/>the single cleanup context"]
    K --> L["TreeScanner yields entries lazily"]
    L --> M["stream each file into tar<br/>1 MiB slices, O_NOFOLLOW"]
    M --> N["append MANIFEST.txt<br/>carries the exact file count"]
    N --> O["optional checksum, then verify"]
    O --> P{"verification ok?"}
    P -- no --> Q["remove archive and sidecar"]
    P -- yes --> R["optional retention"]
    R --> S["report statistics"]
    Q --> T["exit 5"]
    S --> U["exit 0"]
    E -.-> V
    F -.-> V
    K -.-> V
    O -.-> V
    V["one cleanup point<br/>remove the incomplete archive,<br/>then re-raise"]
```

### The decisions that matter

**A single directory scan.** `os.scandir` runs exactly once per directory and
its results are consumed lazily. A dry run and a real run share the same
walker, so the plan cannot drift from what a run actually does. Peak memory is
bounded by one read buffer, not by the size of the archive.

**One cleanup path.** `_partial_archive` wraps the build. It catches
`BaseException`, so handled errors, unexpected exceptions, `SIGINT` and
`SIGTERM` all leave nothing behind. There is no second cleanup path to forget.
`signals.py` funnels both signals into one exception so this stays a single
exit code.

**Failures carry their exit code.** `UsageError` maps to 2, `ArchiveError` to
4, and so on. `main()` catches the base class once and returns
`exc.exit_code`. No code decides a status by inspecting an exception type.

**Unset is not the same as false.** Every option defaults to `None` in argparse
so that "not passed" stays distinguishable from "passed the default value".
Without that, a config file could never be overridden from the command line.

**Retention is name-based on purpose.** `retention.py` cannot even be handed a
source directory, and only considers names matching this tool's own pattern
that also parse as a real date. That is why a hand-made `backup.tar.gz` in the
same folder can never be deleted.

**Compression is a table.** `tarfile` names the level differently per codec:
`compresslevel` for gzip and bzip2, `preset` for xz, `level` for zstd. So each
format carries its own level keyword, and adding one is a single table entry.

Per-entry decision logic, failure paths, and the full test map are in
[docs/architecture.md](docs/architecture.md).

---

## Development

```bash
git clone https://github.com/toms1010/log-archive
cd log-archive

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Running from source

```bash
python -m log_archive --help
python -m log_archive /tmp/test-logs --dry-run
```

---

## Testing

```bash
pytest              # full suite
pytest -v           # detailed
pytest -k security  # security properties only
```

Static checks and packaging:

```bash
ruff check .
ruff format --check .
mypy src/
python -m build
```

The complete gate, which is what CI runs:

```bash
ruff check . && ruff format --check . && mypy && pytest
```

Regenerate the screenshots in this README:

```bash
pip install pillow
python scripts/generate_screenshots.py
```

### Example test directory

Try it without touching real system logs:

```bash
mkdir -p ~/test-logs
echo "Application started" > ~/test-logs/app.log
echo "User logged in"     > ~/test-logs/auth.log
echo "System event"       > ~/test-logs/system.log

log-archive ~/test-logs --dry-run
log-archive ~/test-logs
log-archive verify ~/log-archives/<archive-name>.tar.gz
```

---

## Troubleshooting

**`Permission denied` on some files (exit 3)**
Expected when not running as root. Unreadable files are skipped and counted
rather than aborting the run, so you still get an archive. Re-run with `sudo` if
you need everything.

**`Ran out of disk space` (exit 4)**
Archives are written to the output filesystem. Point `-o` somewhere with room,
or use `--compression none`.

**`verification failed` (exit 5)**
The archive was corrupt and has been removed, so nothing misleading is left
behind. Re-run. If it recurs on the same input, the source is likely changing
while it is read.

**`file shrank ... while it was being archived` (exit 4)**
`logrotate` truncated a log mid-read. That cannot be repaired inside a tar
stream, so the archive is discarded rather than left structurally broken.
Re-running normally succeeds. For scheduled runs, schedule shortly after
rotation.

**Output shows `[ok]` instead of `✓`**
The terminal or redirect is not UTF-8, for example under `LC_ALL=C`. Cosmetic
only.

**A log is being written while it is archived**
The archived copy is a consistent prefix as of the moment it was read. That is
intentional: the copy is internally valid rather than a torn snapshot.

---

## Limitations

* No incremental or differential mode; every run is a full archive.
* No extraction, by design.
* The manifest is the last member, so that it can report an exact file count
  without a second directory scan.
* Zero padding appended to a valid archive is tolerated, because CPython's gzip
  decoder silently ignores it. All real corruption is detected.
* `--follow-symlinks` never leaves the source tree, despite the name. A log
  directory mounted elsewhere needs its own run.
* Dry run cannot report unreadable files, since detecting them means opening
  every file.
* Not a backup system: no scheduling, no remote copy, no restore tool.

---

## License

MIT. See [LICENSE](LICENSE).
