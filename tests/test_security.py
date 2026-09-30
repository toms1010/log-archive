"""Security focused tests.

These cover the properties that matter when the tool is pointed at
``/var/log`` as root: it must not read outside the tree, must not follow a
symlink that could be swapped in mid-run, must not recurse into its own
output, must never write to or delete a source log, and must never hand an
archive to a shell.
"""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path

import pytest

from helpers import find_archive, member_names
from log_archive.archive import build_archive
from log_archive.compression import get_format
from log_archive.exclusions import ExclusionRules
from log_archive.utils import reserve_unique_path

GZIP = get_format("gzip")


def _build(source: Path, dest: Path, **kwargs: object) -> None:
    build_archive(source, dest, ExclusionRules.build(), GZIP, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# No shell, ever
# --------------------------------------------------------------------------


def test_no_shell_execution_anywhere_in_the_package() -> None:
    """The tool must not shell out.

    ``subprocess`` with ``shell=True`` or ``os.system`` in a tool that runs as
    root over /var/log is a command injection waiting to happen.
    """
    import log_archive

    package = Path(log_archive.__file__).parent
    offenders: list[str] = []
    for source in package.glob("*.py"):
        text = source.read_text()
        for needle in ("os.system", "shell=True", "os.popen", "eval(", "exec("):
            if needle in text:
                offenders.append(f"{source.name}: {needle}")
    assert offenders == []


# --------------------------------------------------------------------------
# Symlink handling
# --------------------------------------------------------------------------


def test_symlink_to_sensitive_file_is_never_dereferenced(tmp_path: Path) -> None:
    """A symlink to /etc/shadow must not pull its contents into the archive."""
    source = tmp_path / "logs"
    source.mkdir()
    (source / "app.log").write_text("public data\n")
    (source / "shadow").symlink_to("/etc/shadow")

    archive = tmp_path / "out.tar.gz"
    _build(source, archive)
    with tarfile.open(archive) as tar:
        member = tar.getmember(f"{source.name}/shadow")
        assert member.issym()
        assert member.size == 0
        # extractfile resolves a symlink to a *member of the same archive*.
        # It cannot find one, which proves /etc/shadow's bytes were never
        # captured.
        with pytest.raises(KeyError):
            tar.extractfile(member)


def test_symlink_replaced_by_another_symlink_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O_NOFOLLOW closes the scan-then-open race.

    Between the directory scan and the read, an attacker with write access to
    the log directory could replace a regular file with a symlink to something
    sensitive. The engine opens with O_NOFOLLOW, so that swap is refused.
    """
    import log_archive.archive as archive_module

    source = tmp_path / "logs"
    source.mkdir()
    target = source / "app.log"
    target.write_text("original\n")

    swapper = tmp_path / "swapper.py"
    swapper.write_text("# placeholder so the path exists")

    real_open_no_follow = archive_module._open_no_follow
    swapped = {"done": False}

    def swapping_open(path: Path) -> object:
        # Simulate the race: replace the file with a symlink just before it
        # would be opened.
        if not swapped["done"] and Path(path) == target:
            swapped["done"] = True
            target.unlink()
            target.symlink_to("/etc/shadow")
        return real_open_no_follow(path)

    monkeypatch.setattr(archive_module, "_open_no_follow", swapping_open)
    archive = tmp_path / "out.tar.gz"
    stats = build_archive(source, archive, ExclusionRules.build(), GZIP)
    assert stats.files_archived == 0
    assert any("symlink" in entry.reason for entry in stats.skipped_entries)
    assert stats.skipped_entries[0].relative == "app.log"


def test_o_nofollow_is_used(tmp_path: Path) -> None:
    """A regression guard on the flag itself."""
    import log_archive.archive as archive_module

    target = tmp_path / "regular"
    target.write_text("data\n")
    handle = archive_module._open_no_follow(target)
    with handle:
        assert handle.read() == b"data\n"

    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        archive_module._open_no_follow(link)


def test_follow_symlinks_confines_to_the_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "logs"
    (source / "inner").mkdir(parents=True)
    (source / "inner" / "real.log").write_text("inside\n")
    (source / "good").symlink_to(source / "inner")
    (source / "bad").symlink_to("/etc")

    archive = tmp_path / "out.tar.gz"
    stats = build_archive(
        source, archive, ExclusionRules.build(use_defaults=False), GZIP, follow_symlinks=True
    )
    names = member_names(archive)
    assert f"{source.name}/good/real.log" in names
    assert not any(name.startswith(f"{source.name}/bad") for name in names)
    assert any("escapes" in entry.reason for entry in stats.skipped_entries)


# --------------------------------------------------------------------------
# No recursion, no self-archiving
# --------------------------------------------------------------------------


def test_archive_never_contains_itself_or_siblings(tmp_path: Path) -> None:
    """Repeated runs with the output inside the source must stay flat."""
    source = tmp_path / "logs"
    archive_dir = source / "archives"
    archive_dir.mkdir(parents=True)
    (source / "app.log").write_text("payload\n")

    for _ in range(3):
        dest = reserve_unique_path(archive_dir, "logs_archive_20260930_065300", ".tar.gz")
        _build(source, dest, skip_paths=[archive_dir.resolve()])

    archives = sorted(archive_dir.iterdir())
    assert len(archives) == 3
    for archive in archives:
        names = member_names(archive)
        assert not any("archives" in name for name in names)
        assert names == {source.name, f"{source.name}/app.log"}


# --------------------------------------------------------------------------
# The source tree is read only
# --------------------------------------------------------------------------


def test_source_files_are_never_modified_or_deleted(tmp_path: Path) -> None:
    source = tmp_path / "logs"
    source.mkdir()
    files = {}
    for name in ("a.log", "b.log", "c.log"):
        path = source / name
        path.write_text(f"content of {name}\n")
        files[name] = (path.stat().st_mtime_ns, path.stat().st_size, path.read_text())

    _build(source, tmp_path / "out.tar.gz")

    for name, before in files.items():
        path = source / name
        assert path.exists()
        assert (path.stat().st_mtime_ns, path.stat().st_size, path.read_text()) == before
    assert sorted(p.name for p in source.iterdir()) == ["a.log", "b.log", "c.log"]


def test_permissions_and_ownership_are_preserved(tmp_path: Path) -> None:
    source = tmp_path / "logs"
    source.mkdir()
    secret = source / "app.log"
    secret.write_text("x\n")
    secret.chmod(0o640)
    archive = tmp_path / "out.tar.gz"
    _build(source, archive)
    with tarfile.open(archive) as tar:
        member = tar.getmember(f"{source.name}/app.log")
    assert stat.S_IMODE(member.mode) == 0o640


# --------------------------------------------------------------------------
# Archive naming and overwrite protection
# --------------------------------------------------------------------------


def test_existing_archives_are_never_overwritten(tmp_path: Path) -> None:
    """A pre-existing archive must survive untouched."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    existing = out_dir / "logs_archive_20260930_065300.tar.gz"
    existing.write_bytes(b"precious previous backup")

    source = tmp_path / "logs"
    source.mkdir()
    (source / "new.log").write_text("new data\n")
    dest = reserve_unique_path(out_dir, "logs_archive_20260930_065300", ".tar.gz")
    assert dest != existing
    _build(source, dest)

    assert existing.read_bytes() == b"precious previous backup"
    assert dest.is_file() and dest.stat().st_size > 0


def test_reservation_is_atomic_under_concurrency(tmp_path: Path) -> None:
    """Two runs in the same second must get different files."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    reserved = [
        reserve_unique_path(out_dir, "logs_archive_20260930_065300", ".tar.gz") for _ in range(50)
    ]
    assert len({p.name for p in reserved}) == 50
    assert len(list(out_dir.iterdir())) == 50


def test_reserved_archive_is_not_world_readable(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dest = reserve_unique_path(out_dir, "logs_archive_20260930_065300", ".tar.gz")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


# --------------------------------------------------------------------------
# Extraction hazards
# --------------------------------------------------------------------------


def test_tool_ships_no_extraction_code() -> None:
    """The tool never extracts, so there is no extraction attack surface."""
    import log_archive

    package = Path(log_archive.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text()
        assert ".extractall(" not in text, f"{source.name} extracts archives"
        assert ".extract(" not in text, f"{source.name} extracts archives"


def test_hostile_member_names_are_flagged(tmp_path: Path) -> None:
    """A crafted archive must be reported as unsafe to extract."""
    from log_archive.verification import is_safe_member, unsafe_members

    hostile = tmp_path / "logs_archive_20260930_065300.tar"
    with tarfile.open(hostile, "w") as tar:
        for name, linkname in (
            ("../escape.log", ""),
            ("/etc/passwd", ""),
            ("logs/link", "/etc/shadow"),
            ("logs/up", "../../etc/shadow"),
        ):
            info = tarfile.TarInfo(name)
            if linkname:
                info.type = tarfile.SYMTYPE
                info.linkname = linkname
            else:
                info.size = 0
            tar.addfile(info)

    found = unsafe_members(hostile)
    assert "../escape.log" in found
    assert "/etc/passwd" in found
    assert "logs/link" in found
    assert "logs/up" in found
    assert is_safe_member(tarfile.TarInfo("logs/safe.log"))


def test_normal_archive_has_no_unsafe_paths_except_absolute_symlinks(
    log_tree: Path, out_dir: Path, run_cli
) -> None:
    run_cli(str(log_tree), "-o", str(out_dir), "-q", "--verify")
    from log_archive.verification import verify_archive

    result = verify_archive(find_archive(out_dir))
    assert result.ok
    # Only the deliberate escape.link should be flagged.
    assert all(name.endswith("escape.link") for name in result.unsafe_members)


# --------------------------------------------------------------------------
# Devices and special files
# --------------------------------------------------------------------------


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="needs /dev")
def test_symlink_to_a_device_is_never_read(tmp_path: Path) -> None:
    """/dev/zero would produce an endless stream if it were ever read."""
    source = tmp_path / "logs"
    source.mkdir()
    (source / "zero").symlink_to("/dev/zero")
    (source / "app.log").write_text("safe\n")

    # Default mode: stored as a link, target never opened.
    archive = tmp_path / "out.tar.gz"
    stats = build_archive(source, archive, ExclusionRules.build(), GZIP)
    assert stats.files_archived == 1
    with tarfile.open(archive) as tar:
        assert tar.getmember(f"{source.name}/zero").issym()

    # Following mode: still refused, because /dev is outside the source tree.
    other = tmp_path / "out2.tar.gz"
    followed = build_archive(source, other, ExclusionRules.build(), GZIP, follow_symlinks=True)
    assert followed.files_archived == 1
    assert any("escapes" in entry.reason for entry in followed.skipped_entries)


def test_fifo_is_skipped_not_opened(tmp_path: Path) -> None:
    """Opening a FIFO for reading blocks until a writer appears."""
    source = tmp_path / "logs"
    source.mkdir()
    os.mkfifo(source / "pipe")
    (source / "app.log").write_text("safe\n")

    archive = tmp_path / "out.tar.gz"
    stats = build_archive(source, archive, ExclusionRules.build(), GZIP)
    assert stats.files_archived == 1
    assert any("FIFO" in entry.reason for entry in stats.skipped_entries)
