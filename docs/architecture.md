# Architecture

A tour of the package for anyone about to change it. The goal is to explain
*why* the seams are where they are, so you do not have to reverse-engineer it
from the code.

## Module map

```
cli.py ──────┬── archive.py ──┬── statistics.py
             │                ├── compression.py
             │                ├── exclusions.py
             │                └── manifest.py
             ├── verification.py
             ├── retention.py
             ├── checksum.py
             ├── config.py
             ├── logging_utils.py
             ├── signals.py
             └── utils.py
```

| Module | Responsibility |
| --- | --- |
| `cli.py` | Argument parsing, subcommand dispatch, console output, exit codes. |
| `archive.py` | The engine: walk the tree, stream files into a tar, collect statistics. |
| `exclusions.py` | Which paths are left out, and how patterns are matched. |
| `compression.py` | The compression format table. |
| `statistics.py` | Counters and report rendering. No filesystem access. |
| `verification.py` | Integrity checking and extraction-safety predicates. |
| `retention.py` | Finding this tool's archives, planning and applying cleanup. |
| `manifest.py` | Building and parsing `MANIFEST.txt`. |
| `checksum.py` | Streamed SHA-256. |
| `config.py` | TOML loading, validation, and merging. |
| `logging_utils.py` | `archive.log`. |
| `signals.py` | SIGINT and SIGTERM to one exception. |
| `utils.py` | Exit codes, error types, formatting, atomic path reservation. |

## Data flow for an archive run

```
argv
  └─ normalise_argv()      legacy form becomes `archive ...`
      └─ argparse
          └─ _prepare()    load config, merge, build Printer
              └─ command_archive()
                  ├─ _resolve_source()      must exist, be a dir, be listable
                  ├─ _resolve_output()      create the destination
                  ├─ _build_rules()         config excludes + CLI excludes
                  ├─ _resolve_compression() config + CLI
                  ├─ _prune_paths()         if output is inside source
                  ├─ reserve_unique_path()  O_EXCL, so no overwrite
                  └─ _partial_archive()     context manager: always clean up
                      └─ build_archive()
                          ├─ TreeScanner  one scandir per directory
                          │   └─ Entry    yielded lazily, in sorted order
                          └─ tarfile     streamed, 1 MiB slices
```

The lazy generator is the key structural decision. `TreeScanner` yields
`Entry` objects, and the same generator is consumed by both `build_archive()`
and `plan_archive()`. A dry run and a real run therefore cannot disagree about
what they would do, because they are the same code.

## Design decisions

### One directory scan, ever

`os.scandir` is called exactly once per directory, and its results are sorted
and consumed lazily. The stat data `scandir` already cached is reused
(`entry.stat(follow_symlinks=False)` on Linux does not re-issue a syscall).

This matters on `/var/log`, where a second scan of tens of thousands of entries
is measurable. It is also why there is no "collect then process" list: a list
of every path in a large tree is itself a memory problem.

### Streaming, always

File bodies go through `tar.addfile(tarinfo, handle)` in 1 MiB slices.
Peak memory is bounded by the largest single `tarfile` buffer, not by the size
of the archive or the size of any input file. `sha256_file` and
`verify_archive` do the same.

### Failures are typed and carry their exit code

`LogArchiveError` subclasses carry an `exit_code` attribute:

```
UsageError            -> 2
PermissionDeniedError -> 3
ArchiveError          -> 4
VerificationError     -> 5
ConfigError           -> 6
ChecksumError         -> 7
CleanupError          -> 8
```

`main()` catches `LogArchiveError` once and returns `exc.exit_code`. Nothing in
the codebase decides an exit code by inspecting an exception type, and no
command returns a bare integer that has to be remembered.

### Cleanup happens in exactly one place

`_partial_archive(archive_path, printer)` is a context manager around the
build. It catches `BaseException` and removes the file, so handled errors,
unexpected exceptions, and interrupts all leave nothing behind. There is no
second cleanup path to forget.

The same rule appears in `signals.py`: both `SIGINT` and `SIGTERM` become one
`OperationInterrupted`, so there is a single exit code (`130`) and a single
cleanup route.

### Unset is not the same as false

Every argparse option defaults to `None`, not to `False` or a default string.
`Settings.resolve(cli, config, default)` then applies precedence in one place.
Without this, "the user did not pass `--verify`" would be indistinguishable from
"the user passed a config that said `verify = false`", and a config file could
never be overridden.

### Retention is name-based on purpose

`retention.py` cannot be handed a source directory; it only accepts an output
directory, and only considers names matching
`^logs_archive_(\d{8})_(\d{6})(_\d{2,})?\.(tar|tar\.gz|...)$` that also parse as
a real date. This is the reason a hand-made `backup.tar.gz` in the same folder
can never be deleted. The alternative, tracking created paths in a database,
would be state that can be lost or out of sync with the filesystem.

### Compression is a table, not an `if`

`tarfile.open` takes a different keyword for the level depending on the codec:
`compresslevel` for gzip and bzip2, `preset` for xz, `level` for zstd. So
`CompressionFormat` carries `level_kwarg` alongside `level`. Adding a format
means adding one dict entry.

### Verification is two passes, not one

Walking tar members does not validate a gzip stream. `tarfile` stops at the
end-of-archive marker and never reads the CRC32 trailer, so an archive
corrupted in the middle of a member passes a member walk cleanly. Decoding the
whole stream separately forces the codec's own check.

Both passes stream, so this costs a second sequential read and no memory.

### The manifest is written last

`MANIFEST.txt` includes the exact file count, which is only known once the walk
has finished. Writing it last avoids a second directory scan. Tar members are
order independent, so extraction is unaffected, and `info` finds it by name.

### Security checks are advisory where they should be

`is_safe_member()` answers "would extracting this member escape the
destination?". It is deliberately **not** part of integrity verification.
An early version used it to fail verification, which meant any archive of a
normal `/var/log` tree - full of absolute symlinks - was judged corrupt and
deleted. Extraction hazards are now reported as warnings; only integrity
failures fail.

## Invariants worth preserving

If you change this code, keep these true. Several are covered by tests.

1. **The source is never written to.** No `unlink`, `write`, `chmod`, or
   `rename` on a source path, ever.
2. **No source symlink is dereferenced** unless `--follow-symlinks` is given,
   and even then only inside the source tree.
3. **An existing archive is never overwritten.** `reserve_unique_path` uses
   `O_EXCL`.
4. **The output directory is pruned from the walk** when it is inside the
   source.
5. **No incomplete archive survives** any failure path.
6. **Retention only ever deletes** files matching this tool's own naming
   scheme.
7. **No `subprocess`, `os.system`, or `shell=True`** anywhere in the package.
8. **A single unreadable file never fails the run.** It is skipped, counted,
   and reported.
9. **Memory does not scale with archive size.**

## Where the tests live

| File | Covers |
| --- | --- |
| `test_archive.py` | The engine: nesting, empty dirs, special files, streaming, disk full, shrinking files. |
| `test_exclusions.py` | Pattern semantics, defaults, invalid patterns. |
| `test_cli.py` | Argument normalisation, every subcommand, exit codes, output under redirect. |
| `test_checksum.py` | SHA-256 correctness, `sha256sum` compatibility, streaming. |
| `test_config.py` | TOML parsing, validation, precedence. |
| `test_manifest.py` | Manifest contents and that it never leaks log data. |
| `test_retention.py` | Name matching, ordering, and that foreign files survive. |
| `test_verification.py` | Truncation, bit-flips, garbage, extraction hazards. |
| `test_security.py` | Symlink and traversal properties, immutability of the source. |
| `test_signals.py` | Handler installation, interrupts, no partial archives. |

Every test uses `tmp_path`. None touches `/var/log`.
