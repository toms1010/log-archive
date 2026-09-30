"""Tests for exclusion rule matching."""

from __future__ import annotations

import pytest

from log_archive.exclusions import DEFAULT_EXCLUDES, ExclusionRules


def test_defaults_are_preserved_from_the_original_tool() -> None:
    """The built-in list is a compatibility contract; do not quietly change it."""
    assert DEFAULT_EXCLUDES == (
        "journal",
        "*.sock",
        "*.pid",
        "wtmp",
        "btmp",
        "*.gz",
        "*.xz",
        "*.zst",
    )


def test_empty_rule_set_matches_nothing() -> None:
    rules = ExclusionRules.build(use_defaults=False)
    assert len(rules) == 0
    assert not rules.matches("anything.log")
    assert rules.describe() == "none"


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        # A pattern with no slash matches the base name at any depth.
        ("*.log.1", "app.log.1", True),
        ("*.log.1", "deep/nested/app.log.1", True),
        ("*.log.1", "app.log", False),
        ("*.old", "nginx/access.old", True),
        # A pattern with a slash is anchored at the source root.
        ("nginx/*.old", "nginx/access.old", True),
        ("nginx/*.old", "deep/nginx/access.old", False),
        ("nginx/*.old", "other/access.old", False),
        # ** crosses directory boundaries, so it prunes a whole subtree.
        ("journal/**", "journal", False),
        ("journal/**", "journal/system.journal", True),
        ("journal/**", "journal/a/b/c", True),
        # Anchored, so a same-named directory elsewhere is unaffected.
        ("journal/**", "var/journal/system.journal", False),
        ("*/cache", "a/cache", True),
        ("*/cache", "a/b/cache", True),
    ],
)
def test_pattern_matching(pattern: str, path: str, expected: bool) -> None:
    rules = ExclusionRules([pattern])
    assert rules.matches(path) is expected


def test_defaults_apply_at_the_documented_paths() -> None:
    rules = ExclusionRules.build()
    assert rules.matches("journal")
    assert rules.matches("nginx/nginx.sock")
    assert rules.matches("run/app.pid")
    assert rules.matches("wtmp")
    assert rules.matches("btmp")
    assert rules.matches("old.log.gz")
    assert rules.matches("old.log.xz")
    assert rules.matches("old.log.zst")
    assert not rules.matches("syslog")
    assert not rules.matches("nginx/access.log")
    # A name rule matches one path, not a subtree. The built-in "journal" rule
    # still removes journal/ and everything under it, because the engine stops
    # descending once a directory is excluded.
    assert not rules.matches("journal/system.journal")
    assert ExclusionRules(["journal/**"]).matches("journal/system.journal")


def test_excluded_directory_prunes_its_subtree(log_tree, out_dir, run_cli) -> None:
    """Excluding a directory must remove its contents, not just the entry."""
    from helpers import find_archive, member_names

    assert run_cli(str(log_tree), "-o", str(out_dir), "-q")[0] == 0
    names = member_names(find_archive(out_dir))
    assert not any("journal" in name for name in names)
    assert f"{log_tree.name}/syslog" in names


def test_extra_patterns_extend_the_defaults() -> None:
    rules = ExclusionRules.build(["*.log.1"])
    assert rules.matches("app.log.1")
    assert rules.matches("app.log.gz")
    assert not rules.matches("app.log")


def test_no_default_excludes_disables_them() -> None:
    rules = ExclusionRules.build(use_defaults=False)
    assert not rules.matches("app.log.gz")
    assert not rules.matches("wtmp")
    assert not rules.matches("journal")


def test_describe_lists_active_patterns() -> None:
    assert ExclusionRules.build(["*.log.1"]).describe() == ", ".join((*DEFAULT_EXCLUDES, "*.log.1"))


def test_empty_pattern_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ExclusionRules(["   "])


def test_invalid_exclusion_pattern_exits_with_usage_code(readable_tree, out_dir, run_cli) -> None:
    code, _out, err = run_cli(str(readable_tree), "-o", str(out_dir), "-x", "  ")
    assert code == 2
    assert "must not be empty" in err


def test_custom_exclusions_are_applied_end_to_end(readable_tree, out_dir, run_cli) -> None:
    from helpers import find_archive, member_names

    code, _out, _err = run_cli(str(readable_tree), "-o", str(out_dir), "-q", "-x", "*.log", "-X")
    assert code == 0
    names = member_names(find_archive(out_dir))
    assert not any(name.endswith(".log") for name in names)


def test_verbose_lists_active_exclusions(readable_tree, out_dir, run_cli) -> None:
    code, out, _err = run_cli(str(readable_tree), "-o", str(out_dir), "-v", "-q", "-x", "*.keepme")
    # -q wins over -v for output, but the run itself must still succeed.
    assert code == 0
    assert out == ""

    code, out, _err = run_cli(str(readable_tree), "-o", str(out_dir), "-v", "-x", "*.keepme")
    assert code == 0
    assert "Exclusions:" in out
    assert "*.keepme" in out
