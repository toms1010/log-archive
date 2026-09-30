# Security

Implementation-level security notes. [SECURITY.md](../SECURITY.md) is the
policy and threat model; this is the mechanics.

## The log directory is untrusted input

`/var/log` is not under your control. A compromised service can create a
symlink to anywhere on the filesystem. So can a low-privilege process with write
access to its own log directory. The engine therefore treats every entry it
finds as potentially hostile.

### Symlinks are never dereferenced by default

`TreeScanner` yields symlinks with `kind == "symlink"`, and `_add_symlink()`
stores them via `tar.gettarinfo()`, which uses `lstat`. The target is recorded
as a string and never opened.

```python
# src/log_archive/archive.py
if not self.follow_symlinks:
    yield Entry(child_path, relative, arcname, "symlink", 0)
    return
```

A symlink to `/etc/shadow` therefore becomes a symlink member whose `linkname`
is the string `/etc/shadow`, with `size == 0`. Nothing is read.

### O_NOFOLLOW closes the scan-to-open race

`lstat` during the scan tells you nothing about the state of the file a
millisecond later, when you open it. A writable log directory is a race: swap
`app.log` for a symlink to `/etc/shadow` between the scan and the read.

Every regular file is opened with `O_NOFOLLOW`, which makes the kernel refuse
to traverse a final symlink component:

```python
flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOCTTY
```

`ELOOP` is translated into a skip with a clear reason rather than being
silently retried. `test_symlink_replaced_by_another_symlink_is_not_followed`
covers this.

### Following is still confined

`--follow-symlinks` is stricter than its name suggests, deliberately. It only
follows a link whose `resolve(strict=True)` is inside the source tree, and each
`(st_dev, st_ino)` is visited at most once:

```python
if not self._within_source(target):
    self.stats.note_skipped(relative, "symlink target escapes source directory")
    return
...
if inode in self._seen_inodes:
    self.stats.note_skipped(relative, "symlink loop")
    return
```

The alternative, following wherever the link points, would mean a root-run tool
on `/var/log` could be redirected to archive anything on the filesystem. The
cost is that `--follow-symlinks` will not follow a link to a log directory
mounted elsewhere; in that case archive that directory directly.

## Special files

| Type | Handling | Why |
| --- | --- | --- |
| FIFO | Skipped | Opening for reading blocks until a writer appears. A backup that hangs forever is worse than one with a gap. |
| Socket | Skipped | Carries no data; its target is transient. |
| Character device | Skipped | `/dev/zero` never ends; `/dev/tty` may block. |
| Block device | Skipped | Not a log. |

This is a safety property, not an optimisation. `test_fifo_is_skipped_not_opened`
and `test_symlink_to_a_device_is_never_read` guard it.

## No self-archiving

If the output directory resolves inside the source, the whole subtree is pruned
before it is walked:

```python
if child.is_dir(follow_symlinks=False):
    if child_path.resolve() in self._skip_paths:
        self.stats.note_excluded(relative)
        return
```

Without this, `--no-default-excludes` plus a repeated run would nest each
archive inside the next one without bound. Pruning is preferable to refusing to
run: a user who puts their output inside their logs probably has a reason, and
they still get a correct archive.

## No overwriting

```python
descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
```

`O_EXCL` makes name reservation atomic even with concurrent processes, so two
`log-archive` runs in the same second cannot destroy each other's output. `0600`
means a log archive is not world-readable even before the caller thinks about
it. `test_existing_archives_are_never_overwritten` and
`test_reservation_is_atomic_under_concurrency` guard both.

## No shell

The package contains no `subprocess`, no `os.system`, no `os.popen`, no
`eval`, no `exec`. A tool that runs as root and processes paths taken from a
directory full of third-party output has no business shelling out.
`test_no_shell_execution_anywhere_in_the_package` scans the source for it.

## Extraction

This tool does not extract, so there is no extraction attack surface.
`test_tool_ships_no_extraction_code` asserts no module contains `extractall` or
`extract`.

`verification.is_safe_member()` exists for the future, and is used to *report*
rather than to reject:

```python
def is_safe_member(member: tarfile.TarInfo) -> bool:
    if name.startswith(("/", "\\")):
        return False
    if ".." in Path(name).parts:
        return False
    if member.issym() or member.islnk():
        if target.startswith(("/", "\\")):
            return False
        return ".." not in Path(target).parts
    return member.isreg() or member.isdir()
```

An early version used this to fail verification. Because a real `/var/log`
archive is full of absolute symlinks, that meant every good archive was judged
corrupt and deleted. The distinction now:

* **Integrity** (stream decodes, members readable) decides pass or fail.
* **Extraction hazards** are warnings, surfaced by `verify` and `info`.

## Manifest and log hygiene

`MANIFEST.txt` and `archive.log` travel with or beside an archive, so putting
log contents in either would copy sensitive data to a second place. Both carry
metadata only: paths, sizes, counts, host details, and reasons for skipping.
`test_activity_log_never_contains_log_contents` writes a sentinel token into a
log file and asserts it appears in neither.

## What a user still has to do

The tool cannot do these for you:

* Archives are **not encrypted**. Use filesystem or storage-layer encryption.
* Archive directories must be **no more permissive** than the logs themselves.
* Run with `sudo` only when needed, and give it a root-owned `-o` destination.
* Do not extract archives from sources you do not trust.
* Retention deletes files; on most filesystems it does not securely erase them.

## Useful reading

* [Python `tarfile` docs](https://docs.python.org/3/library/tarfile.html) and
  the extraction-filter discussion (`data` filter, PEP 706).
* [GNU tar manual, "Extraction"](https://www.gnu.org/software/tar/manual/html_node/Extraction.html)
  on `--absolute-names` and `..` handling.
* [CWE-22](https://cwe.mitre.org/data/definitions/22.html) path traversal,
  [CWE-59](https://cwe.mitre.org/data/definitions/59.html) link following.
