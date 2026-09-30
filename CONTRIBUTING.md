# Contributing

Thanks for considering a contribution. This is a small, deliberately
uncomplicated tool, and the most useful contributions are bug reports, tests
for awkward edge cases, and small focused features.

## Ground rules

* **Standard library only at runtime.** A new dependency needs a very good
  reason. The tool is meant to drop onto a locked-down server without a
  dependency resolver run.
* **No over-engineering.** A small Linux utility should stay a small Linux
  utility. If a proposed abstraction only serves one caller, write the direct
  code instead.
* **Correctness over convenience.** This tool's job is to produce archives that
  can be trusted. Anything that could silently lose data needs to be right
  first.
* **No silent failures.** Every error either produces a message and an exit
  code, or is explicitly documented as intentional.

## Development setup

```bash
git clone https://github.com/toms1010/log-archive
cd log-archive

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## The gate

Everything must pass before a change is considered done:

```bash
ruff check .    # lint
mypy            # types, strict
pytest          # tests
```

Or all three at once, as CI does:

```bash
ruff check . && mypy && pytest
```

`mypy` runs in `strict` mode over `src/`. Please do not silence it with
`# type: ignore` unless you can explain why in a comment; `cast()` with a
reasoning comment is usually the better tool.

## Tests

Every change needs a test. Tests must use `tmp_path` and must **never** touch
`/var/log` or any real system path.

```bash
pytest                          # everything
pytest -k security              # security properties
pytest -k "not slow" -x         # stop at the first failure
pytest --lf                     # only what failed last time
```

### Testing things that are hard to test

Some behaviours cannot be triggered directly. The existing suite shows the
patterns:

* **Permission errors:** `chmod 0o000` the file or directory, and
  `@pytest.mark.skipif(os.geteuid() == 0, ...)` because root ignores the mode.
  Restore permissions in a `finally` block or `tmp_path` cleanup will fail and
  mask the real assertion.
* **Disk full:** monkeypatch `tarfile.TarFile.addfile` to raise
  `OSError(errno.ENOSPC, ...)`.
* **A file shrinking mid-read:** monkeypatch `os.fstat` in
  `log_archive.archive` to report a size larger than the file holds.
* **Interrupts:** spawn `python -m log_archive` as a subprocess over a
  directory big enough to be slow, then `send_signal(SIGINT)`.
* **Corruption:** truncate the archive, or overwrite bytes in the middle.
  Note that a bit-flip in the middle of a gzip member is *not* caught by
  walking members; that is what the stream pass is for.

## Style

* PEP 8, enforced by `ruff`.
* Type hints on public functions. `from __future__ import annotations` is
  already in every module.
* Docstrings on public functions and classes. Say *why*, not just *what*,
  especially where the reason is non-obvious.
* Small functions with clear names. `verb_noun()` is fine.
* Dataclasses for structured data rather than dictionaries.
* No global mutable state.

## Commit messages

Short, imperative, and explaining the reason rather than restating the diff:

```
Good:
  Discard archive when a log shrinks mid-read

  tarfile writes the header before the data, so a truncated read
  misaligns every following member. Better to lose the run than to
  leave a plausible-looking corrupt archive.

Bad:
  fix bug
  update files
  wip
```

## Pull requests

1. Branch from `main`.
2. Make the change, with tests.
3. Confirm the gate passes locally.
4. Update `CHANGELOG.md` under `## [Unreleased]`.
5. Update the README if you changed or added user-visible behaviour.
6. Open the PR describing what changed and why.

If you change the exit codes, the exclusion defaults, or the archive naming
scheme, call that out explicitly: those are compatibility contracts.

## Reporting bugs

Open an issue with:

* `log-archive --version`, Python version, and distribution
* The exact command you ran
* What you expected and what happened
* A synthetic directory that reproduces it, if you can

Never paste real log contents or real archives into an issue. See
[SECURITY.md](SECURITY.md) for private reporting of security issues.
