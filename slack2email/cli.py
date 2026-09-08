"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config as cfgmod
from .config import CONFIG_PATH, LOG_PATH, STATE_DIR, Config, ConfigError
from .mailer import Mailer
from .state import Cursors, SeenStore

LABEL = "com.nickeubank.slack2email"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

REQUIRED_USER_SCOPES = [
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "users:read",
]

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)


def _load(args) -> Config:
    return cfgmod.load(Path(args.config) if args.config else None)


def cmd_init(args) -> int:
    path = Path(args.config) if args.config else CONFIG_PATH
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    email = args.email or os.environ.get("EMAIL", "you@example.com")
    path.write_text(cfgmod.TEMPLATE.format(email=email))
    path.chmod(0o600)
    print(f"Wrote {path} (mode 600).")
    print("Next: fill in your tokens, then run `slack2email doctor`.")
    return 0


def cmd_doctor(args) -> int:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        cfg = _load(args)
    except ConfigError as exc:
        print(f"[{BAD}] config: {exc}")
        return 1
    print(f"[{OK}] config loaded from {Path(args.config) if args.config else CONFIG_PATH}")

    failures = 0
    for ws in cfg.workspaces:
        print(f"\n--- workspace: {ws.name} ---")
        client = WebClient(token=ws.user_token)
        try:
            auth = client.auth_test()
        except SlackApiError as exc:
            print(f"[{BAD}] user token: {exc.response.get('error')}")
            failures += 1
            continue
        print(f"[{OK}] user token: {auth.get('user')} @ {auth.get('team')} ({auth.get('url')})")

        if not str(ws.user_token).startswith("xoxp-"):
            print(f"[{WARN}] token does not look like a user token (xoxp-). "
                  "A bot token only sees channels the bot was invited to.")

        granted = {s.strip() for s in (auth.headers.get("x-oauth-scopes") or "").split(",")}
        missing = [s for s in REQUIRED_USER_SCOPES if s not in granted]
        if missing:
            print(f"[{BAD}] missing user scopes: {', '.join(missing)}")
            failures += 1
        else:
            print(f"[{OK}] all {len(REQUIRED_USER_SCOPES)} required user scopes granted")

        try:
            WebClient().apps_connections_open(app_token=ws.app_token)
            print(f"[{OK}] app token valid; Socket Mode connection can be opened")
        except SlackApiError as exc:
            print(f"[{BAD}] app token: {exc.response.get('error')} "
                  "(needs an app-level token with connections:write, and Socket Mode enabled)")
            failures += 1

        try:
            convs = client.users_conversations(
                types="public_channel,private_channel,im,mpim", limit=200, exclude_archived=True
            )
            items = convs.get("channels", [])
            more = "+" if convs.get("response_metadata", {}).get("next_cursor") else ""
            print(f"[{OK}] you are in {len(items)}{more} conversations that will be forwarded")
        except SlackApiError as exc:
            print(f"[{WARN}] could not list conversations: {exc.response.get('error')}")

    print("\n--- email ---")
    mailer = Mailer(cfg.smtp, cfg.email)
    try:
        mailer.verify()
        print(f"[{OK}] SMTP {cfg.smtp.host}:{cfg.smtp.port} accepted the login")
        print(f"[{OK}] mail will be sent {cfg.email.sender} -> {cfg.email.to}")
    except Exception as exc:
        print(f"[{BAD}] SMTP: {exc}")
        if "gmail" in cfg.smtp.host and "Username and Password not accepted" in str(exc):
            print("        Google needs an App Password: https://myaccount.google.com/apppasswords")
        failures += 1

    window = cfg.forward.batch_seconds
    print(f"\nMode: {cfg.forward.mode}"
          + ("  (run `slack2email probe` to confirm user events arrive)"
             if cfg.forward.mode == "socket" else "  (polling; thread replies are not seen)"))
    print(f"Batching: {'one email per message' if window == 0 else f'digest every {window}s'}")
    print(f"Own messages: {cfg.forward.own_messages} | bot messages: {cfg.forward.bot_messages}")
    return 1 if failures else 0


def cmd_test_email(args) -> int:
    cfg = _load(args)
    mailer = Mailer(cfg.smtp, cfg.email, spool_dir=STATE_DIR / "failed")
    msg = mailer.build(
        subject=f"{cfg.email.subject_prefix} test message",
        plain="slack2email is configured correctly.\n\nThis is a test.",
        html="<p><b>slack2email</b> is configured correctly.</p><p>This is a test.</p>",
    )
    ok = mailer.send(msg)
    print("sent" if ok else "FAILED (see log / spool dir)")
    return 0 if ok else 1


def cmd_run(args) -> int:
    _setup_logging(args.verbose)
    from .watcher import Forwarder

    cfg = _load(args)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    mailer = Mailer(cfg.smtp, cfg.email, spool_dir=STATE_DIR / "failed")
    seen = SeenStore(STATE_DIR / "seen.json")
    cursors = Cursors(STATE_DIR / "cursors.json")

    forwarder = Forwarder(cfg, mailer, seen, cursors)
    forwarder.start()
    logging.info(
        "mode=%s; watching %d workspace(s); batching=%ss; delivering to %s",
        cfg.forward.mode, len(cfg.workspaces), cfg.forward.batch_seconds, cfg.email.to,
    )
    forwarder.wait()
    logging.info(
        "stopped: received=%d forwarded=%d skipped=%d emails=%d",
        forwarder.stats.received, forwarder.stats.forwarded,
        forwarder.stats.skipped, forwarder.stats.emails,
    )
    return 0


def cmd_probe(args) -> int:
    """Find out whether this workspace delivers user events over Socket Mode.

    Slack documents Socket Mode for bot events; delivery of events subscribed
    "on behalf of users" is undocumented and reported to be inconsistent. Rather
    than guess, connect and watch for real traffic.
    """
    import queue as _queue
    import tempfile

    _setup_logging(args.verbose)
    from .watcher import SocketListener, Stats

    cfg = _load(args)
    if not cfg.workspaces:
        print("no workspaces configured")
        return 1

    seen_events: list[tuple[str, str]] = []

    def record(req):
        payload = req.payload or {}
        event = payload.get("event") or {}
        if req.type == "events_api" and event.get("type") == "message":
            seen_events.append((event.get("channel", "?"), event.get("subtype") or "message"))
            print(f"  <- message in {event.get('channel')} "
                  f"(subtype={event.get('subtype') or 'none'}, user={event.get('user')})")
        else:
            print(f"  <- {req.type}")

    # Throwaway state so probing never marks real messages as already-forwarded.
    tmpdir = Path(tempfile.mkdtemp(prefix="slack2email-probe-"))
    listeners = []
    for ws in cfg.workspaces:
        listener = SocketListener(
            ws, cfg, _queue.Queue(), SeenStore(tmpdir / "seen.json"), Stats(), on_event=record
        )
        listener.start()
        listeners.append(listener)
        print(f"connected to {listener.team_name} as {listener.names.user(listener.user_id)}")

    print(
        f"\nListening for {args.seconds}s. Send yourself a Slack DM, and post in a\n"
        "channel you're in, so there is something to catch.\n"
    )
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    for listener in listeners:
        listener.close()

    print()
    if seen_events:
        channels = sorted({c for c, _ in seen_events})
        print(f"[{OK}] received {len(seen_events)} message event(s) across {len(channels)} conversation(s)")
        print(f'[{OK}] user events ARE delivered over Socket Mode -> keep mode = "socket"')
        return 0

    print(f"[{WARN}] no message events arrived.")
    print("        Either nothing was posted during the probe, or this workspace does")
    print("        not deliver user events over Socket Mode.")
    print('        If you did post something, set mode = "poll" in your config.')
    return 1


def cmd_install_agent(args) -> int:
    config_path = Path(args.config).expanduser() if args.config else CONFIG_PATH
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "slack2email", "run", "--config", str(config_path)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PATH": os.environ.get("PATH", "")},
    }
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open("wb") as fh:
        plistlib.dump(plist, fh)
    print(f"Wrote {PLIST_PATH}")

    if args.load:
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)], capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"launchctl bootstrap failed: {result.stderr.strip()}")
            return 1
        print(f"Loaded. Logs: {LOG_PATH}")
    else:
        print(f"To start it now:  launchctl bootstrap gui/{os.getuid()} {PLIST_PATH}")
    return 0


def cmd_uninstall_agent(args) -> int:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print(f"Removed {PLIST_PATH}")
    else:
        print("Nothing installed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slack2email", description=__doc__)
    parser.add_argument("--version", action="version", version=f"slack2email {__version__}")
    parser.add_argument("--config", help=f"path to config.toml (default {CONFIG_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write a starter config file")
    p_init.add_argument("--email", help="your email address")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    sub.add_parser("doctor", help="check tokens, scopes and SMTP").set_defaults(func=cmd_doctor)
    sub.add_parser("test-email", help="send yourself one test email").set_defaults(func=cmd_test_email)
    sub.add_parser("run", help="run the forwarder in the foreground").set_defaults(func=cmd_run)

    p_probe = sub.add_parser(
        "probe", help="check whether Socket Mode delivers your user events"
    )
    p_probe.add_argument("--seconds", type=int, default=60, help="how long to listen")
    p_probe.set_defaults(func=cmd_probe)

    p_agent = sub.add_parser("install-agent", help="install a launchd agent so it runs at login")
    p_agent.add_argument("--load", action="store_true", help="also start it now")
    p_agent.set_defaults(func=cmd_install_agent)

    sub.add_parser("uninstall-agent", help="remove the launchd agent").set_defaults(
        func=cmd_uninstall_agent
    )

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
