# Usage

Complete reference for `log-archive`. The [README](../README.md) has a quick
start; this document covers the details.

## Contents

- [Invocation forms](#invocation-forms)
- [Commands](#commands)
- [archive](#archive)
- [verify](#verify)
- [list](#list)
- [cleanup](#cleanup)
- [info](#info)
- [Exclusion patterns](#exclusion-patterns)
- [Compression formats](#compression-formats)
- [Configuration](#configuration)
- [Output and scripting](#output-and-scripting)
- [Exit codes](#exit-codes)
- [Recovery](#recovery)

## Invocation forms

```bash
log-archive /var/log                        # implicit "archive"
log-archive archive /var/log                # explicit
log-archive /var/log -o ~/archives          # legacy flags still work
log-archive archive /var/log -o ~/archives
python -m log_archive /var/log              # without the console script
```

The legacy form is rewritten into the `archive` subcommand by inspecting
`argv` before parsing. A first positional argument that is not one of
`archive`, `verify`, `list`, `cleanup`, `info` is treated as a source path, so
`log-archive /var/log` and `log-archive archive /var/log` are identical.
`--version` and `--help` are never preceded by an injected subcommand.

## Commands

| Command | Purpose |
| --- | --- |
| `archive` | Create a timestamped compressed archive of a log directory |
| `verify` | Check an existing archive's integrity |
| `list` | List archives in the output directory |
| `cleanup` | Apply retention, deleting all but the newest N |
| `info` | Show size, format, members, checksum, manifest, verification |

## archive

```bash
log-archive archive SOURCE [OPTIONS]
```

`SOURCE` must be an existing, readable directory. It is opened read only and
is never modified.

### Options

| Option | Description |
| --- | --- |
| `-o`, `--output PATH` | Destination directory. Created if missing. Default `~/log-archives`. |
| `-x`, `--exclude PATTERN` | Extra exclusion glob. Repeatable. |
| `-X`, `--no-default-excludes` | Ignore the built-in list. |
| `--dry-run` | Report the plan; create nothing. |
| `--verify` | Verify the new archive; remove it if verification fails. |
| `--checksum` | Write a `.sha256` sidecar. |
| `--manifest` | Add `MANIFEST.txt` inside the archive. |
| `--keep N` | After a successful run, keep only the newest N archives. |
| `--compression FORMAT` | See [Compression formats](#compression-formats). |
| `--follow-symlinks` | Follow links resolving inside the source tree. |
| `--no-follow-symlinks` | Store links as links. Default. |
| `-q`, `--quiet` | Only errors. |
| `-v`, `--verbose` | Show skips, exclusions, and per-step detail. |
| `-c`, `--config PATH` | Read configuration from `PATH`. |
| `--no-config` | Ignore all configuration. |

### Output filename

```
logs_archive_YYYYMMDD_HHMMSS.tar.gz
```

The name is reserved atomically with `O_EXCL` before any writing begins, so an
existing archive is never truncated. A same-second collision produces
`_01`, `_02`, and so on.

### What ends up in the archive

| Source object | Result |
| --- | --- |
| Regular file | Streamed into the archive |
| Directory | Stored, then descended into |
| Symlink | Stored as a symlink; target never read |
| Broken symlink | Stored as a symlink, or skipped in follow mode |
| Socket | Skipped, counted, reported |
| FIFO | Skipped, counted, reported |
| Device node | Skipped, counted, reported |
| Unreadable file | Skipped, counted, reported; the run continues |
| Excluded path | Skipped, counted |

If a log is being appended to while it is read, the archived copy is a
consistent prefix as of the moment it was read. That is intentional: the copy
is internally valid rather than a torn snapshot.

If a log *shrinks* mid-read, which `logrotate` causes routinely, the tar
framing cannot be repaired after the header is written. The archive is
discarded and the tool exits `4` with an explanation. Re-running normally
succeeds.

### dry-run

```bash
log-archive archive /var/log --dry-run
```

Computes the plan with the same walker the real run uses, then prints it. Writes
nothing, creates no output directory, and does not open any file for reading, so
it is cheap. The consequence is that it cannot report files that would turn out
to be unreadable, since detecting those requires opening them.

## verify

```bash
log-archive verify ARCHIVE [OPTIONS]
```

Two passes:

1. Decode the entire compression stream, forcing the codec's own integrity
   check (gzip CRC32 and length) and detecting truncation.
2. Walk every tar member and read every file body, proving consistent framing.

The first pass is what catches silent corruption. `tarfile` stops at the
end-of-archive marker and never reads the gzip trailer, so a member walk alone
accepts an archive altered halfway through.

Exit `0` if intact, `5` otherwise.

## list

```bash
log-archive list [-o PATH] [--limit N] [-q] [-v]
```

Newest first, with creation time, size, and whether a checksum sidecar exists.
Only files matching this tool's naming scheme are listed; anything else in the
directory is invisible.

## cleanup

```bash
log-archive cleanup [-o PATH] [--keep N] [--dry-run] [-q] [-v]
```

Deletes all but the newest N archives, together with their `.sha256` sidecars.
Default `--keep` is `10`.

Only these filenames are ever considered:

```
logs_archive_YYYYMMDD_HHMMSS.<ext>
logs_archive_YYYYMMDD_HHMMSS_NN.<ext>
```

A `backup.tar.gz` from another tool, a `notes.txt`, or even
`logs_archive_20260930_065300.tar.gz.bak` are all left alone. The timestamp is
parsed as a real date, so a name with month 13 is not treated as ours.

`--dry-run` prints the plan and deletes nothing.

## info

```bash
log-archive info ARCHIVE [--no-verify] [-q] [-v]
```

Shows size, creation time, format, member count with uncompressed bytes,
checksum if present, manifest status, and verification result. `--no-verify`
skips the full read, which is much faster on a large archive.

## Exclusion patterns

| Pattern | Slash | Matched against | Example |
| --- | --- | --- | --- |
| `*.log.1` | no | base name, any depth | `app.log.1`, `deep/a.log.1` |
| `nginx/*.old` | yes | path relative to source | `nginx/access.old` only |
| `journal/**` | yes | path relative to source | everything under `journal/` |
| `*/cache` | yes | path relative to source | `a/cache`, `a/b/cache` |

* `*` and `**` both cross `/`.
* Path patterns are anchored at the source root, never at the filesystem root.
* Excluding a directory prunes its whole subtree.
* Rules are evaluated in order; the first match wins.
* Name patterns are anchored to the full base name, so `*.log` does not match
  `app.log.1`.

### Built-in exclusions

`journal`, `*.sock`, `*.pid`, `wtmp`, `btmp`, `*.gz`, `*.xz`, `*.zst`.

Keep in mind that `*.gz` excludes rotated logs, which are often the ones you
most want. For long-retention archiving, try:

```bash
log-archive /var/log -X -x 'journal' -x '*.sock' -x '*.pid' -x 'wtmp' -x 'btmp'
```

## Compression formats

| Name | Extension | Level | Notes |
| --- | --- | --- | --- |
| `gzip` | `.tar.gz` | 6 | Default |
| `gzip-fast` | `.tar.gz` | 1 | Fastest, largest |
| `gzip-best` | `.tar.gz` | 9 | Slowest, smallest |
| `none` | `.tar` | – | Uncompressed |
| `xz` | `.tar.xz` | 6 | Smallest, slowest |
| `bzip2` | `.tar.bz2` | 9 | |
| `zstd` | `.tar.zst` | 3 | Requires Python 3.14+ |

`gzip-best` on a multi-gigabyte `/var/log` is often the wrong trade; `xz` will
beat it on size if you can wait.

## Configuration

Searched in order, later entries winning:

1. `/etc/log-archive/config.toml`
2. `~/.config/log-archive/config.toml`
3. `--config PATH`
4. command line flags

```toml
output = "~/log-archives"
compression = "gzip"
verify = true
checksum = true
manifest = true
follow_symlinks = false
keep = 10

exclude = ["journal", "*.sock", "*.pid", "*.old"]
```

Recognised keys, and nothing else:

| Key | Type |
| --- | --- |
| `output` | string |
| `compression` | string, must be a known format |
| `verify`, `checksum`, `manifest`, `follow_symlinks`, `no_default_excludes` | boolean |
| `keep` | non-negative integer |
| `exclude` | list of strings |

Anything else is a hard error (exit `6`) rather than a silent no-op. A
malformed file is reported with the parse error. `--no-config` skips everything
and takes precedence over `--config`.

Every option defaults to "unset" internally, which is what lets a config value
apply while an explicit command line flag still wins.

## Output and scripting

* No ANSI escapes, ever. Output is safe to redirect into a file.
* No progress bars, spinners, or terminal control sequences.
* Status markers degrade from `✓` to `[ok]` when the output encoding cannot
  represent them, so `LC_ALL=C` does not raise `UnicodeEncodeError`.
* Informational output goes to stdout, errors to stderr, and both are flushed
  per line so `> out.txt 2> err.txt` stays in order.
* `--quiet` suppresses normal output and warnings; errors always appear. The
  full detail of what was skipped is always recorded in `archive.log`.
* No prompts, ever. Nothing waits for input, so cron and CI are safe.

## Exit codes

| Code | Meaning | Typical cause |
| --- | --- | --- |
| `0` | Success | |
| `1` | General error | Unclassified OS error |
| `2` | Invalid CLI usage | Missing source, bad option, invalid pattern |
| `3` | Permission error | Unreadable source or unwritable output |
| `4` | Archive error | Out of disk space, read-only filesystem, file shrank mid-read |
| `5` | Verification failure | Corrupt or truncated archive |
| `6` | Configuration error | Missing, malformed, or invalid config file |
| `7` | Checksum error | Cannot write or read the `.sha256` file |
| `8` | Cleanup error | Retention could not delete something |
| `130` | Interrupted | SIGINT or SIGTERM |

## Recovery

### An archive failed verification

It has already been removed, because a corrupt archive is worse than no
archive: it looks like a backup. Re-run.

```bash
log-archive /var/log --verify --checksum
```

### Suspect a stored archive

```bash
log-archive verify /var/backups/log-archives/logs_archive_20260930_065301.tar.gz
sha256sum -c /var/backups/log-archives/logs_archive_20260930_065301.tar.gz.sha256
```

### Inspect without extracting

```bash
tar -tzf /var/backups/log-archives/logs_archive_20260930_065301.tar.gz | less
```

### Extract an archive

This tool does not extract, by design. When you do extract, be aware that a
`/var/log` archive legitimately contains absolute symlinks.

```bash
# List first and look for anything unexpected
tar -tzvf ARCHIVE | less

# Prefer extracting into an empty directory, without preserving ownership
mkdir -p /safe/restore
tar -xzf ARCHIVE -C /safe/restore --no-same-owner --no-same-permissions
```

Do not extract an archive from a source you do not trust. `log-archive info`
and `log-archive verify` report members that would be unsafe to extract.

### Dispatched to a log shipper

`archive.log` records metadata only. To get failures noticed, pair the exit
code with your scheduler:

```bash
log-archive /var/log --verify --checksum --keep 30 \
  || logger -t log-archive "archive run failed with status $?"
```
