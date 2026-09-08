import os

import pytest

from slack2email import config as cfgmod
from slack2email.config import ConfigError


def write(tmp_path, body, mode=0o600):
    p = tmp_path / "config.toml"
    p.write_text(body)
    p.chmod(mode)
    return p


GOOD = """
[smtp]
username = "me@example.com"
password = "pw"

[email]
to = "me@example.com"

[[workspace]]
name = "w"
user_token = "xoxp-1"
app_token = "xapp-1"
"""


def test_loads_good_config(tmp_path):
    cfg = cfgmod.load(write(tmp_path, GOOD))
    assert cfg.email.to == "me@example.com"
    assert cfg.workspaces[0].user_token == "xoxp-1"
    assert cfg.forward.mode == "socket"
    assert cfg.email.sender == "me@example.com"  # defaults to smtp username


def test_rejects_world_readable(tmp_path):
    with pytest.raises(ConfigError, match="readable by other users"):
        cfgmod.load(write(tmp_path, GOOD, mode=0o644))


def test_rejects_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="No config at"):
        cfgmod.load(tmp_path / "absent.toml")


def test_rejects_no_workspaces(tmp_path):
    body = GOOD.split("[[workspace]]")[0]
    with pytest.raises(ConfigError, match="No .*workspace"):
        cfgmod.load(write(tmp_path, body))


def test_rejects_missing_recipient(tmp_path):
    with pytest.raises(ConfigError, match="to is required"):
        cfgmod.load(write(tmp_path, GOOD.replace('to = "me@example.com"', 'to = ""')))


def test_rejects_bad_mode(tmp_path):
    body = GOOD.replace("[email]", '[forward]\nmode = "smoke-signal"\n\n[email]')
    with pytest.raises(ConfigError, match="mode must be"):
        cfgmod.load(write(tmp_path, body))


def test_rejects_unknown_key(tmp_path):
    body = GOOD.replace("[email]", "[forward]\nbatch_secondz = 5\n\n[email]")
    with pytest.raises(ConfigError, match="unknown key"):
        cfgmod.load(write(tmp_path, body))


def test_rejects_incomplete_workspace(tmp_path):
    body = GOOD.replace('app_token = "xapp-1"', "")
    with pytest.raises(ConfigError, match="missing app_token"):
        cfgmod.load(write(tmp_path, body))


def test_template_is_valid_config(tmp_path):
    """The file `init` writes must itself parse once secrets resolve."""
    body = cfgmod.TEMPLATE.format(email="me@example.com")
    body = body.replace("keychain:slack2email-smtp", "pw")
    body = body.replace("keychain:slack2email-user-token", "xoxp-1")
    body = body.replace("keychain:slack2email-app-token", "xapp-1")
    cfg = cfgmod.load(write(tmp_path, body))
    assert cfg.forward.mode == "socket"
    assert cfg.forward.poll_interval_seconds == 60


def test_secret_from_env(monkeypatch):
    monkeypatch.setenv("S2E_TEST", "  secret  ")
    assert cfgmod.resolve_secret("env:S2E_TEST", what="t") == "secret"


def test_secret_from_missing_env_raises():
    os.environ.pop("S2E_ABSENT", None)
    with pytest.raises(ConfigError, match="not set"):
        cfgmod.resolve_secret("env:S2E_ABSENT", what="t")


def test_secret_from_file(tmp_path):
    p = tmp_path / "s"
    p.write_text("tok\n")
    assert cfgmod.resolve_secret(f"file:{p}", what="t") == "tok"


def test_secret_from_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        cfgmod.resolve_secret(f"file:{tmp_path / 'nope'}", what="t")


def test_literal_secret_passes_through():
    assert cfgmod.resolve_secret("xoxp-literal", what="t") == "xoxp-literal"


def test_keychain_miss_raises():
    with pytest.raises(ConfigError, match="no Keychain item"):
        cfgmod.resolve_secret("keychain:slack2email-definitely-absent-xyz", what="t")
