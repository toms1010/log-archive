"""Tests for the in-archive manifest."""

from __future__ import annotations

import tarfile
from datetime import datetime
from pathlib import Path

from helpers import find_archive


def _manifest_of(archive: Path) -> str:
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            if member.name.rsplit("/", 1)[-1] == "MANIFEST.txt":
                stream = tar.extractfile(member)
                assert stream is not None
                with stream:
                    return stream.read().decode("utf-8")
    raise AssertionError("no manifest in archive")


def test_manifest_contains_every_required_field(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--manifest")[0] == 0
    body = _manifest_of(find_archive(out_dir))
    for field in (
        "Version:",
        "Created:",
        "Hostname:",
        "User:",
        "Source:",
        "Compression:",
        "Files:",
        "Excluded:",
    ):
        assert field in body, f"missing {field}"
    assert str(readable_tree) in body
    assert "gzip" in body


def test_manifest_records_the_exact_file_count(readable_tree: Path, out_dir: Path, run_cli) -> None:
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--manifest")[0] == 0
    body = _manifestOf = _manifest_of(find_archive(out_dir))
    line = next(row for row in body.splitlines() if row.startswith("Files:"))
    count = int(line.split(":", 1)[1].strip())
    with tarfile.open(find_archive(out_dir)) as tar:
        actual = sum(
            1 for m in tar.getmembers() if m.isfile() and m.name.endswith((".log", ".txt"))
        )
    # The manifest itself is a member, so subtract it.
    assert count == actual - 1
    assert _manifestOf


def test_manifest_lists_active_exclusions(log_tree: Path, out_dir: Path, run_cli) -> None:
    assert run_cli(str(log_tree), "-o", str(out_dir), "-q", "--manifest", "-x", "*.log")[0] == 0
    body = _manifest_of(find_archive(out_dir))
    assert "*.log" in body
    assert "journal" in body


def test_manifest_is_last_so_counts_are_accurate(
    readable_tree: Path, out_dir: Path, run_cli
) -> None:
    """Documented design choice: manifest appended last for an exact count."""
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--manifest")[0] == 0
    with tarfile.open(find_archive(out_dir)) as tar:
        assert tar.getmembers()[-1].name.endswith("MANIFEST.txt")


def test_manifest_parse_round_trip(tmp_path: Path) -> None:
    from log_archive.manifest import Manifest

    manifest = Manifest.collect(
        created=datetime(2026, 9, 30, 6, 53, 0),
        source=Path("/var/log"),
        compression="gzip",
        excludes=["*.sock", "journal"],
        file_count=184,
    )
    fields = Manifest.parse(manifest.render())
    assert fields["Compression"] == "gzip"
    assert fields["Files"] == "184"
    assert fields["Source"] == "/var/log"
    assert "*.sock" in fields["Excluded"]
    assert "journal" in fields["Excluded"]


def test_manifest_never_contains_log_contents(readable_tree: Path, out_dir: Path, run_cli) -> None:
    token = "SENSITIVE_TOKEN_VALUE"
    (readable_tree / "leaky.log").write_text(f"{token}\n")
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--manifest")[0] == 0
    assert token not in _manifest_of(find_archive(out_dir))


def test_manifest_survives_a_missing_passwd_entry(
    readable_tree: Path, out_dir: Path, run_cli, monkeypatch
) -> None:
    """Containers often have no /etc/passwd entry for the current uid."""
    import getpass

    def boom() -> str:
        raise KeyError("uid not in /etc/passwd")

    monkeypatch.setattr(getpass, "getuser", boom)
    assert run_cli(str(readable_tree), "-o", str(out_dir), "-q", "--manifest")[0] == 0
    assert "User:        unknown" in _manifest_of(find_archive(out_dir))
