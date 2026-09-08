"""Configuration loading and secret resolution."""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("SLACK2EMAIL_CONFIG_DIR", Path.home() / ".config" / "slack2email")
)
CONFIG_PATH = CONFIG_DIR / "config.toml"
STATE_DIR = Path(
    os.environ.get("SLACK2EMAIL_STATE_DIR", Path.home() / ".local" / "state" / "slack2email")
)
LOG_PATH = Path.home() / "Library" / "Logs" / "slack2email.log"

# Message subtypes that are membership/administrivia noise rather than content.
NOISE_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "group_topic",
        "group_purpose",
        "group_name",
        "group_archive",
        "group_unarchive",
        "message_changed",
        "message_deleted",
        "message_replied",
        "tombstone",
        "reminder_add",
        "bot_add",
        "bot_remove",
        "app_conversation_join",
    }
)


class ConfigError(Exception):
    """Raised when the config file is missing or malformed."""


def resolve_secret(value: str, *, what: str) -> str:
    """Resolve a secret that may point somewhere else instead of being inline.

    Supported forms:
      "xoxp-..."            literal value
      "env:VAR_NAME"        read from the environment
      "file:/path/to/file"  read (stripped) from a file
      "keychain:service"    macOS Keychain generic password
      "keychain:service/account"
    """
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{what} is empty")

    if value.startswith("env:"):
        name = value[4:]
        got = os.environ.get(name)
        if not got:
            raise ConfigError(f"{what}: environment variable {name} is not set")
        return got.strip()

    if value.startswith("file:"):
        path = Path(value[5:]).expanduser()
        try:
            return path.read_text().strip()
        except OSError as exc:
            raise ConfigError(f"{what}: cannot read {path}: {exc}") from exc

    if value.startswith("keychain:"):
        spec = value[9:]
        service, _, account = spec.partition("/")
        cmd = ["security", "find-generic-password", "-s", service, "-w"]
        if account:
            cmd[3:3] = ["-a", account]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except FileNotFoundError as exc:
            raise ConfigError(f"{what}: `security` not found (macOS only)") from exc
        except subprocess.CalledProcessError as exc:
            raise ConfigError(
                f"{what}: no Keychain item for service {service!r}"
                f"{f' account {account!r}' if account else ''}"
            ) from exc
        return out.stdout.strip()

    return value


@dataclass
class SmtpConfig:
    host: str = "smtp.gmail.com"
    port: int = 587
    username: str = ""
    password: str = ""
    security: str = "starttls"  # starttls | ssl | none
    timeout: int = 30


@dataclass
class EmailConfig:
    to: str = ""
    sender: str = ""
    subject_prefix: str = "[Slack]"
    thread_by_conversation: bool = True
    max_messages_per_email: int = 50


@dataclass
class ForwardConfig:
    mode: str = "socket"  # socket | poll
    batch_seconds: int = 60
    own_messages: bool = False
    bot_messages: bool = True
    noise: bool = False
    timezone: str = ""  # empty -> system local time
    channel_allowlist: list[str] = field(default_factory=list)
    channel_blocklist: list[str] = field(default_factory=list)
    # polling mode only
    poll_interval_seconds: int = 60
    poll_spacing_seconds: float = 1.5
    conv_refresh_seconds: int = 900
    backfill_seconds: int = 0


@dataclass
class WorkspaceConfig:
    name: str
    user_token: str
    app_token: str


@dataclass
class Config:
    smtp: SmtpConfig
    email: EmailConfig
    forward: ForwardConfig
    workspaces: list[WorkspaceConfig]


def _typed(section: dict, cls, *, what: str):
    """Build a dataclass from a config section, rejecting unknown keys."""
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(section) - known
    if unknown:
        raise ConfigError(f"[{what}]: unknown key(s) {', '.join(sorted(unknown))}")
    return cls(**section)


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f"No config at {path}.\nRun `slack2email init` to write a starter file."
        )

    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConfigError(
            f"{path} is readable by other users (mode {mode:o}); it holds tokens.\n"
            f"Fix with: chmod 600 {path}"
        )

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    smtp = _typed(raw.get("smtp", {}), SmtpConfig, what="smtp")
    email = _typed(raw.get("email", {}), EmailConfig, what="email")
    forward = _typed(raw.get("forward", {}), ForwardConfig, what="forward")

    workspaces = []
    for i, ws in enumerate(raw.get("workspace", [])):
        missing = {"user_token", "app_token"} - set(ws)
        if missing:
            raise ConfigError(
                f"[[workspace]] #{i + 1}: missing {', '.join(sorted(missing))}"
            )
        unknown = set(ws) - {"name", "user_token", "app_token"}
        if unknown:
            raise ConfigError(
                f"[[workspace]] #{i + 1}: unknown key(s) {', '.join(sorted(unknown))}"
            )
        name = ws.get("name") or f"workspace-{i + 1}"
        workspaces.append(
            WorkspaceConfig(
                name=name,
                user_token=resolve_secret(ws["user_token"], what=f"{name} user_token"),
                app_token=resolve_secret(ws["app_token"], what=f"{name} app_token"),
            )
        )

    if not workspaces:
        raise ConfigError("No [[workspace]] blocks in config; nothing to watch.")
    if not email.to:
        raise ConfigError("[email] to is required")
    if smtp.security not in {"starttls", "ssl", "none"}:
        raise ConfigError("[smtp] security must be one of: starttls, ssl, none")
    if forward.mode not in {"socket", "poll"}:
        raise ConfigError('[forward] mode must be "socket" or "poll"')

    email.sender = email.sender or smtp.username or email.to
    smtp.password = resolve_secret(smtp.password, what="smtp password") if smtp.password else ""

    return Config(smtp=smtp, email=email, forward=forward, workspaces=workspaces)


TEMPLATE = """\
# slack2email configuration.  Keep this file mode 0600 -- it holds tokens.
#
# Secrets may be given inline, or indirectly as:
#   "env:VAR_NAME"        "file:/path/to/secret"
#   "keychain:service"    "keychain:service/account"   (macOS Keychain)

[smtp]
host = "smtp.gmail.com"
port = 587
security = "starttls"          # starttls | ssl | none
username = "{email}"
# Google requires an App Password here, not your normal password:
#   https://myaccount.google.com/apppasswords
password = "keychain:slack2email-smtp"

[email]
to = "{email}"
sender = "{email}"
subject_prefix = "[Slack]"
thread_by_conversation = true  # one mail thread per Slack conversation
max_messages_per_email = 50

[forward]
# "socket" = instant push over a WebSocket (preferred; also catches thread replies).
# "poll"   = conversations.history on a timer. Slower and misses thread replies,
#            but works even if your workspace doesn't deliver user events over
#            Socket Mode, and it catches up on anything missed while offline.
# Run `slack2email probe` to see which one this workspace supports.
mode = "socket"

batch_seconds = 60             # group messages into a digest; 0 = one mail per message
own_messages = false           # also forward messages you sent
bot_messages = true            # forward messages from apps/bots
noise = false                  # forward joins/leaves/topic changes
timezone = ""                  # e.g. "America/New_York"; empty = system local
channel_allowlist = []         # if non-empty, ONLY these (#name or ID) are forwarded
channel_blocklist = []         # #name or ID to skip

# polling mode only:
poll_interval_seconds = 60     # pause between full sweeps
poll_spacing_seconds = 1.5     # pause between per-channel API calls
conv_refresh_seconds = 900     # how often to re-list your conversations
backfill_seconds = 0           # on first sight of a channel, look this far back

[[workspace]]
name = "my-workspace"
user_token = "keychain:slack2email-user-token"   # xoxp-...
app_token = "keychain:slack2email-app-token"     # xapp-...
"""
