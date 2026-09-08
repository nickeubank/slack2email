"""The launchd plist must invoke a command line the CLI can actually parse."""

import plistlib
import sys
from types import SimpleNamespace

from slack2email import cli


def install(tmp_path, monkeypatch, config="/tmp/c.toml"):
    plist_path = tmp_path / "agent.plist"
    monkeypatch.setattr(cli, "PLIST_PATH", plist_path)
    cli.cmd_install_agent(SimpleNamespace(config=config, load=False))
    return plistlib.loads(plist_path.read_bytes())


def test_plist_arguments_are_parseable(tmp_path, monkeypatch):
    """Regression: --config is a top-level flag and must precede the subcommand."""
    data = install(tmp_path, monkeypatch, config="/tmp/mine.toml")
    argv = data["ProgramArguments"]
    assert argv[:3] == [sys.executable, "-m", "slack2email"]

    parsed = cli.build_parser().parse_args(argv[3:])
    assert parsed.command == "run"
    assert parsed.config == "/tmp/mine.toml"


def test_plist_core_keys(tmp_path, monkeypatch):
    data = install(tmp_path, monkeypatch)
    assert data["Label"] == cli.LABEL
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["StandardOutPath"].endswith("slack2email.log")


def test_parser_rejects_flag_after_subcommand():
    """Proves the ordering actually matters, so this test isn't vacuous."""
    import pytest

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--config", "/tmp/x.toml"])
