"""Tests for TOML configuration loading, validation, and precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from log_archive import config as config_module
from log_archive.config import Config, load_config
from log_archive.utils import ConfigError, ExitCode

VALID = """
output = "~/archives"
compression = "xz"
verify = true
checksum = true
manifest = true
follow_symlinks = false
keep = 10
exclude = ["*.log.1", "journal/**"]
"""


def test_no_config_files_gives_an_empty_config(tmp_path: Path) -> None:
    monkey = config_module.SYSTEM_CONFIG
    try:
        config = load_config()
        assert isinstance(config, Config)
    finally:
        config_module.SYSTEM_CONFIG = monkey


def test_explicit_config_is_read(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID)
    config = load_config(str(path))
    assert config.output == "~/archives"
    assert config.compression == "xz"
    assert config.verify is True
    assert config.checksum is True
    assert config.manifest is True
    assert config.follow_symlinks is False
    assert config.keep == 10
    assert config.exclude == ("*.log.1", "journal/**")
    assert config.sources == (path,)
    assert not config.is_empty


def test_user_config_overrides_system_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = tmp_path / "system.toml"
    system.write_text('compression = "bzip2"\nkeep = 3\n')
    user = tmp_path / "user.toml"
    user.write_text('compression = "xz"\n')

    monkeypatch.setattr(config_module, "SYSTEM_CONFIG", system)
    monkeypatch.setattr(config_module, "USER_CONFIG", Path("~/.config/log-archive/config.toml"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / ".config" / "log-archive").mkdir(parents=True)
    (tmp_path / ".config" / "log-archive" / "config.toml").write_text('compression = "xz"\n')

    config = load_config()
    assert config.compression == "xz"  # user wins
    assert config.keep == 3  # only system set it


def test_explicit_config_ignores_the_others(tmp_path: Path, monkeypatch) -> None:
    system = tmp_path / "system.toml"
    system.write_text('compression = "bzip2"\n')
    monkeypatch.setattr(config_module, "SYSTEM_CONFIG", system)
    explicit = tmp_path / "only.toml"
    explicit.write_text('compression = "gzip"\n')
    assert load_config(str(explicit)).compression == "gzip"


def test_no_config_flag_skips_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system = tmp_path / "system.toml"
    system.write_text('compression = "bzip2"\n')
    monkeypatch.setattr(config_module, "SYSTEM_CONFIG", system)
    assert load_config(None, use_config=False).is_empty


def test_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "absent.toml"))


def test_missing_default_config_is_not_an_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config_module, "SYSTEM_CONFIG", tmp_path / "absent.toml")
    assert load_config().is_empty


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('compression = "lzw"\n', "Unknown compression"),
        ("keep = true\n", "must be an integer"),
        ('keep = "ten"\n', "must be an integer"),
        ("keep = -1\n", "must not be negative"),
        ('verify = "yes"\n', "must be true or false"),
        ("output = 42\n", "must be a string"),
        ('exclude = "one"\n', "must be a list of strings"),
        ("exclude = [1, 2]\n", "must be a list of strings"),
        ("nonsense = 1\n", "Unknown key"),
        ("this is not toml [[[\n", "Invalid TOML"),
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_config(str(path))


def test_config_path_that_is_a_directory_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "dir.toml"
    directory.mkdir()
    with pytest.raises(ConfigError, match="not a file"):
        load_config(str(directory))


def test_unreadable_config_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "secret.toml"
    path.write_text('output = "/tmp"\n')
    path.chmod(0o000)
    try:
        import os

        if os.geteuid() == 0:
            return
        with pytest.raises(ConfigError, match="Cannot read config"):
            load_config(str(path))
    finally:
        path.chmod(0o600)


def test_invalid_config_exits_with_code_six(
    readable_tree: Path, out_dir: Path, tmp_path: Path, run_cli
) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("keep = 'lots'\n")
    code, _out, err = run_cli(str(readable_tree), "-o", str(out_dir), "-c", str(path))
    assert code == int(ExitCode.CONFIG)
    assert "must be an integer" in err


def test_config_drives_a_whole_run(readable_tree: Path, tmp_path: Path, run_cli) -> None:
    destination = tmp_path / "from-config"
    path = tmp_path / "config.toml"
    path.write_text(
        f'output = "{destination}"\n'
        'compression = "none"\n'
        "verify = true\n"
        "checksum = true\n"
        "manifest = true\n"
        'exclude = ["*.log"]\n'
    )
    code, _out, _err = run_cli(str(readable_tree), "-c", str(path))
    assert code == 0
    names = sorted(p.name for p in destination.iterdir())
    assert any(name.endswith(".tar") for name in names)
    assert any(name.endswith(".sha256") for name in names)


def test_cli_overrides_config(readable_tree: Path, tmp_path: Path, run_cli) -> None:
    from_config = tmp_path / "config-dest"
    override = tmp_path / "cli-dest"
    path = tmp_path / "config.toml"
    path.write_text(f'output = "{from_config}"\n')

    assert run_cli(str(readable_tree), "-c", str(path), "-q", "-o", str(override))[0] == 0
    assert list(override.glob("*.tar.gz"))
    assert not from_config.exists()


def test_cli_overrides_config_booleans(readable_tree: Path, tmp_path: Path, run_cli) -> None:
    destination = tmp_path / "dest"
    path = tmp_path / "config.toml"
    path.write_text(f'output = "{destination}"\nchecksum = true\n')
    assert run_cli(str(readable_tree), "-c", str(path), "-q")[0] == 0
    assert list(destination.glob("*.sha256"))


def test_no_config_flag_ignores_the_file(readable_tree: Path, tmp_path: Path, run_cli) -> None:
    destination = tmp_path / "dest"
    path = tmp_path / "config.toml"
    path.write_text(f'output = "{destination}"\nchecksum = true\n')
    assert (
        run_cli(str(readable_tree), "-c", str(path), "-q", "--no-config", "-o", str(destination))[0]
        == 0
    )
    assert not list(destination.glob("*.sha256"))
