# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x   | ✓         |

## Threat model

`log-archive` runs with whatever privileges you give it, and its intended use
involves running as **root against `/var/log`**. That combination makes the
following the properties that matter.

### The archive must not contain anything you did not ask for

A log directory is not a trusted input. It can contain symlinks placed by
compromised services, FIFOs that block on open, device nodes that produce
endless data, and files with hostile names.

* Symlinks are **stored as symlinks and never dereferenced**. A link to
  `/etc/shadow` is recorded as a link; its bytes never enter the archive.
* Regular files are opened with **`O_NOFOLLOW`**, so a file swapped for a
  symlink between the directory scan and the read is refused rather than
  followed. Without this, a writable log directory is a read-anywhere-else
  primitive.
* `--follow-symlinks` follows only links whose target resolves **inside the
  source tree**, and visits each real inode at most once so cycles terminate.
  This is stricter than the flag name suggests, on purpose: the alternative is
  a footgun in a tool that is documented for use on `/var/log` as root.
* Sockets, FIFOs, and character or block devices are **skipped, not read**.
  Opening a FIFO for reading blocks until a writer appears; reading
  `/dev/zero` never ends.
* Broken symlinks are stored as links or skipped. They are never followed.

### The source must not be modified

The tool's only writes are the archive, its optional `.sha256` sidecar, and
`archive.log`. No code path deletes, truncates, renames, or chmods a source
file. Retention and cleanup accept only an *output* directory and only ever
consider filenames matching this tool's own naming scheme.

### A partial archive must never look like a good one

The worst outcome for a backup tool is a truncated file that a later script
treats as a successful backup. Therefore:

* `SIGINT` and `SIGTERM` both remove the incomplete archive and exit `130`.
* Any write failure, including `ENOSPC`, removes the incomplete archive.
* A file that changes size mid-read, which would misalign the tar framing, is
  detected and the archive is discarded rather than written.
* `--verify` failing removes both the archive and its checksum.

### Archives must not be overwritten

Archive names are reserved with `O_EXCL` before writing, so two concurrent
runs, or a run racing a cron job, cannot clobber an existing backup. Same-second
collisions get a `_01`, `_02`, ... suffix.

### No shell, ever

The package contains no `subprocess`, no `os.system`, and no `shell=True`.
Every filesystem operation uses standard library calls with explicit paths.
`test_no_shell_execution_anywhere_in_the_package` guards this.

### This tool never extracts

There is no extraction code path, so the classic tar attacks (absolute member
paths, `..` traversal, symlink-then-write, absolute link targets) have no
surface here. `verification.is_safe_member()` exists to *report* such members
should an extraction feature ever be added, and a test asserts no module
contains `extractall` or `extract`.

Note that archives produced from a normal `/var/log` tree **will** contain
absolute symlink targets, which is correct and expected. Those are reported as
advisory warnings, not as verification failures, because rejecting them would
mean deleting good backups.

## Sensitive log contents

Logs routinely contain:

* IP addresses and geolocation data
* Usernames and account identifiers
* Authentication events, failed logins, and session tokens
* Application request data, query strings, and error traces
* Hostnames, internal service names, and infrastructure detail
* Occasionally, credentials leaked by misconfigured applications

Treat an archive with the same care as the logs themselves:

* Archives are created with mode `0600`. Keep the archive directory no more
  permissive than the source.
* Encrypt archives at rest if the destination is not already protected.
  `log-archive` does not encrypt; use your filesystem or storage layer.
* The tool **never** puts log contents into its own log or into a manifest.
  `MANIFEST.txt` carries metadata only. Both properties are covered by tests.
* Transfer archives over an encrypted channel.
* Retention deletes old archives and their checksums, but it does not shred
  them. On most filesystems that is not a secure erase.

## Using sudo safely

```bash
sudo log-archive /var/log -o /var/backups/log-archives
```

* Prefer an explicit root-owned `-o` destination. Without it the archive lands
  in your home directory owned by root, which then blocks your own later runs.
* The tool never escalates privileges itself and never invokes `sudo`.
* Read-only access is enough. The tool never needs to modify `/var/log`.
* After running as root, consider `chown`ing the archive if a non-root process
  will consume it.

## Verification of integrity

`--verify` runs two passes, both of which matter:

1. The **whole compression stream** is decoded, forcing the codec's integrity
   check (gzip CRC32 and length) and catching truncation. Walking tar members
   alone is not sufficient, because `tarfile` stops at the end-of-archive
   marker and never reads the gzip trailer — so silent corruption in the middle
   of a member would otherwise pass.
2. **Every member body is read**, proving the framing is consistent.

Check the SHA-256 sidecar independently when it matters:

```bash
sha256sum -c ~/log-archives/logs_archive_20260930_065301.tar.gz.sha256
```

A checksum only proves the file has not changed since it was written. It does
not prove the archive was correct at the time of writing, which is why
`--verify` exists as a separate step.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

Use GitHub's **"Report a vulnerability"** button on the repository's Security
tab. This opens a private advisory that only you and the maintainers can see,
which is the right channel for anything sensitive.

Please include:

* A description of the issue and its impact
* Steps to reproduce, ideally as a `pytest` test using `tmp_path`
* Your `log-archive --version`, Python version, and distribution

Please do not include real log data or archives in a report. A synthetic
directory reproducing the shape of the problem is all that is needed.

You can expect an acknowledgement within 72 hours and an assessment within
seven days. Fixes for confirmed issues are released as soon as they are
available, and the advisory is published after users have had a reasonable
window to update.
