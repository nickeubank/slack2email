"""Watch Slack and forward messages to email.

Two backends, because Slack's support for *user*-scoped event subscriptions over
Socket Mode is not documented and is reported to be inconsistent:

  socket  push over a WebSocket. Instant, no rate-limit pressure, and it sees
          thread replies. Depends on `user_events` actually being delivered.
  poll    conversations.history on a timer. Always works, survives downtime
          (it resumes from a saved cursor), but it is slower, is throttled by
          Slack's rate limits, and does not see thread replies.

Run `slack2email probe` to find out which one this workspace supports.
"""

from __future__ import annotations

import html as html_mod
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from . import render
from .config import NOISE_SUBTYPES, Config, WorkspaceConfig
from .mailer import Mailer
from .state import Cursors, SeenStore

log = logging.getLogger(__name__)

CONTENT_SUBTYPES = frozenset({"", "bot_message", "file_share", "me_message", "thread_broadcast"})
CONVERSATION_TYPES = "public_channel,private_channel,im,mpim"


@dataclass
class ConvInfo:
    key: str
    kind: str  # channel | private | im | mpim
    label: str
    channel_id: str


@dataclass
class Pending:
    conv: ConvInfo
    author: str
    ts: str
    thread_ts: str
    text: str
    event: dict
    team_domain: str
    workspace: str


@dataclass
class Stats:
    received: int = 0
    forwarded: int = 0
    skipped: int = 0
    emails: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def bump(self, name: str) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + 1)


def _prettify_mpim(name: str) -> str:
    """mpdm-alice--bob--carol-1 -> 'alice, bob, carol'."""
    stripped = re.sub(r"^mpdm-", "", name)
    stripped = re.sub(r"-\d+$", "", stripped)
    parts = [p for p in stripped.split("--") if p]
    return ", ".join(parts) if parts else name


def should_forward(event: dict, fwd, user_id: str) -> bool:
    """Decide whether a raw message event is worth emailing."""
    subtype = event.get("subtype") or ""

    if event.get("hidden"):
        return False
    if subtype in NOISE_SUBTYPES and not fwd.noise:
        return False
    if subtype and subtype not in CONTENT_SUBTYPES and not fwd.noise:
        return False
    if subtype == "bot_message" and not fwd.bot_messages:
        return False
    if event.get("user") == user_id and not fwd.own_messages:
        return False
    return True


def channel_allowed(conv: ConvInfo, fwd) -> bool:
    """Apply the allow/block lists, matching on channel ID or name."""
    candidates = {conv.channel_id.lstrip("#"), conv.label.lstrip("#")}
    if fwd.channel_allowlist:
        allow = {c.lstrip("#") for c in fwd.channel_allowlist}
        if not candidates & allow:
            return False
    if fwd.channel_blocklist:
        block = {c.lstrip("#") for c in fwd.channel_blocklist}
        if candidates & block:
            return False
    return True


class WorkspaceBase:
    """Shared identity, name resolution, filtering and enqueueing."""

    def __init__(self, ws: WorkspaceConfig, cfg: Config, out: queue.Queue, seen: SeenStore, stats: Stats):
        self.ws = ws
        self.cfg = cfg
        self.out = out
        self.seen = seen
        self.stats = stats

        self.web = WebClient(token=ws.user_token)
        self.names = render.Names(self.web)
        self._conv_cache: dict[str, ConvInfo] = {}

        auth = self.web.auth_test()
        self.user_id: str = auth["user_id"]
        self.team_id: str = auth["team_id"]
        self.team_name: str = auth.get("team", ws.name)
        host = urlparse(auth.get("url", "")).hostname or ""
        self.team_domain: str = host.split(".")[0] if host else ""
        self.names.prime_user(self.user_id, auth.get("user", "me"))

    def conversation(self, channel_id: str) -> ConvInfo:
        if channel_id in self._conv_cache:
            return self._conv_cache[channel_id]

        kind, label = "channel", channel_id
        try:
            ch = self.web.conversations_info(channel=channel_id)["channel"]
            if ch.get("is_im"):
                kind, label = "im", self.names.user(ch.get("user", ""))
            elif ch.get("is_mpim"):
                kind, label = "mpim", _prettify_mpim(ch.get("name", channel_id))
            elif ch.get("is_private"):
                kind, label = "private", "#" + ch.get("name", channel_id)
            else:
                kind, label = "channel", "#" + ch.get("name", channel_id)
        except SlackApiError as exc:
            log.warning("conversations.info failed for %s: %s", channel_id, exc)

        info = ConvInfo(key=f"{self.team_id}:{channel_id}", kind=kind, label=label, channel_id=channel_id)
        self._conv_cache[channel_id] = info
        return info

    def _author(self, event: dict) -> str:
        if event.get("subtype") == "bot_message" or not event.get("user"):
            return (event.get("bot_profile") or {}).get("name") or event.get("username") or "app"
        return self.names.user(event["user"])

    def handle_message(self, event: dict) -> None:
        self.stats.bump("received")
        channel_id = event.get("channel") or ""
        ts = event.get("ts") or ""
        if not channel_id or not ts:
            self.stats.bump("skipped")
            return
        if not should_forward(event, self.cfg.forward, self.user_id):
            self.stats.bump("skipped")
            return
        if not self.seen.add_if_new(f"{self.team_id}:{channel_id}:{ts}"):
            self.stats.bump("skipped")
            return

        conv = self.conversation(channel_id)
        if not channel_allowed(conv, self.cfg.forward):
            self.stats.bump("skipped")
            return

        self.out.put(
            Pending(
                conv=conv,
                author=self._author(event),
                ts=ts,
                thread_ts=event.get("thread_ts") or "",
                text=event.get("text") or "",
                event=event,
                team_domain=self.team_domain,
                workspace=self.team_name,
            )
        )
        self.stats.bump("forwarded")

    def list_conversations(self) -> list[str]:
        """Every conversation the authenticated user belongs to."""
        ids, cursor = [], None
        while True:
            resp = self.web.users_conversations(
                types=CONVERSATION_TYPES, limit=200, exclude_archived=True, cursor=cursor
            )
            for ch in resp.get("channels", []):
                ids.append(ch["id"])
                if ch.get("id") not in self._conv_cache:
                    if ch.get("is_im"):
                        kind, label = "im", self.names.user(ch.get("user", ""))
                    elif ch.get("is_mpim"):
                        kind, label = "mpim", _prettify_mpim(ch.get("name", ch["id"]))
                    else:
                        kind = "private" if ch.get("is_private") else "channel"
                        label = "#" + ch.get("name", ch["id"])
                    self._conv_cache[ch["id"]] = ConvInfo(
                        key=f"{self.team_id}:{ch['id']}", kind=kind, label=label, channel_id=ch["id"]
                    )
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return ids

    def close(self) -> None:
        pass


class SocketListener(WorkspaceBase):
    """Push delivery over Socket Mode."""

    def __init__(self, *args, on_event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_event = on_event
        self.client = SocketModeClient(app_token=self.ws.app_token, web_client=self.web)
        self.client.socket_mode_request_listeners.append(self._on_request)

    def start(self) -> None:
        self.client.connect()
        log.info("socket mode connected to %s as %s", self.team_name, self.names.user(self.user_id))

    def healthy(self) -> bool:
        try:
            return bool(self.client.is_connected())
        except Exception:
            return False

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def _on_request(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        try:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception as exc:
            log.warning("ack failed: %s", exc)

        if self.on_event is not None:
            self.on_event(req)
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        if event.get("type") != "message":
            return
        try:
            self.handle_message(event)
        except Exception:
            log.exception("failed to handle message event")


class PollingListener(WorkspaceBase):
    """Fallback delivery via conversations.history on a timer."""

    def __init__(self, *args, cursors: Cursors, stop: threading.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self.cursors = cursors
        self._stop = stop
        self._channels: list[str] = []
        self._channels_refreshed = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"poll-{self.team_id}", daemon=True)
        self._thread.start()
        log.info("polling %s as %s", self.team_name, self.names.user(self.user_id))

    def healthy(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        fwd = self.cfg.forward
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if not self._channels or now - self._channels_refreshed > fwd.conv_refresh_seconds:
                    self._channels = self.list_conversations()
                    self._channels_refreshed = now
                    log.info("polling %d conversations in %s", len(self._channels), self.team_name)

                for channel_id in list(self._channels):
                    if self._stop.is_set():
                        break
                    self._poll_channel(channel_id)
                    self._stop.wait(fwd.poll_spacing_seconds)
                self.cursors.flush()
            except Exception:
                log.exception("polling cycle failed for %s", self.team_name)
            self._stop.wait(self.cfg.forward.poll_interval_seconds)

    def _poll_channel(self, channel_id: str) -> None:
        key = f"{self.team_id}:{channel_id}"
        oldest = self.cursors.get(key)
        if oldest is None:
            # First sight of this conversation: start from now, don't replay history.
            backfill = max(0, self.cfg.forward.backfill_seconds)
            self.cursors.set(key, f"{time.time() - backfill:.6f}")
            return

        messages, cursor = [], None
        for _ in range(5):  # bounded pagination
            try:
                resp = self._call_history(channel_id, oldest, cursor)
            except SlackApiError as exc:
                if exc.response.status_code == 429:
                    delay = int(exc.response.headers.get("Retry-After", 30))
                    log.warning("rate limited on %s; sleeping %ss", channel_id, delay)
                    self._stop.wait(delay)
                    return
                if exc.response.get("error") not in {"channel_not_found", "not_in_channel"}:
                    log.warning("history failed for %s: %s", channel_id, exc.response.get("error"))
                return
            messages.extend(resp.get("messages", []))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not resp.get("has_more") or not cursor:
                break
            self._stop.wait(self.cfg.forward.poll_spacing_seconds)

        for msg in sorted(messages, key=lambda m: float(m.get("ts", 0))):
            msg["channel"] = channel_id
            self.handle_message(msg)
            self.cursors.set(key, msg["ts"])

    def _call_history(self, channel_id: str, oldest: str, cursor: str | None):
        return self.web.conversations_history(
            channel=channel_id, oldest=oldest, inclusive=False, limit=200, cursor=cursor
        )


class Forwarder:
    """Owns the listeners, the batching queue, and mail delivery."""

    def __init__(self, cfg: Config, mailer: Mailer, seen: SeenStore, cursors: Cursors | None = None):
        self.cfg = cfg
        self.mailer = mailer
        self.seen = seen
        self.cursors = cursors
        self.queue: queue.Queue[Pending] = queue.Queue()
        self.stats = Stats()
        self.listeners: list[WorkspaceBase] = []
        self._stop = threading.Event()

    def start(self) -> None:
        mode = self.cfg.forward.mode
        for ws in self.cfg.workspaces:
            if mode == "poll":
                listener = PollingListener(
                    ws, self.cfg, self.queue, self.seen, self.stats,
                    cursors=self.cursors or Cursors(self.seen.path.with_name("cursors.json")),
                    stop=self._stop,
                )
            else:
                listener = SocketListener(ws, self.cfg, self.queue, self.seen, self.stats)
            listener.start()
            self.listeners.append(listener)

        threading.Thread(target=self._flush_loop, name="flusher", daemon=True).start()
        threading.Thread(target=self._watchdog_loop, name="watchdog", daemon=True).start()

    def wait(self) -> None:
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        for listener in self.listeners:
            listener.close()
        self.seen.flush()
        if self.cursors:
            self.cursors.flush()

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(30.0):
            for listener in self.listeners:
                if not listener.healthy():
                    log.warning("%s unhealthy; restarting", listener.team_name)
                    try:
                        listener.start()
                    except Exception as exc:
                        log.error("restart of %s failed: %s", listener.team_name, exc)

    # ---- batching --------------------------------------------------------

    def _flush_loop(self) -> None:
        pending: dict[str, list[Pending]] = {}
        opened: dict[str, float] = {}
        window = max(0, self.cfg.forward.batch_seconds)

        while not self._stop.is_set() or pending:
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                item = None

            if item is not None:
                pending.setdefault(item.conv.key, []).append(item)
                opened.setdefault(item.conv.key, time.monotonic())

            now = time.monotonic()
            ready = [k for k, started in opened.items() if now - started >= window]
            if self._stop.is_set():
                ready = list(pending)

            for key in ready:
                batch = pending.pop(key, [])
                opened.pop(key, None)
                if not batch:
                    continue
                size = max(1, self.cfg.email.max_messages_per_email)
                for i in range(0, len(batch), size):
                    try:
                        self._send(batch[i : i + size])
                    except Exception:
                        log.exception("failed to send batch for %s", key)

            if self._stop.is_set() and not pending:
                break

    # ---- rendering + delivery -------------------------------------------

    def _send(self, batch: list[Pending]) -> None:
        first = batch[0]
        conv = first.conv
        tz = self.cfg.forward.timezone
        team_id = conv.key.split(":")[0]
        listener = next((w for w in self.listeners if w.team_id == team_id), None)
        names = listener.names if listener else render.Names(None)

        where = conv.label if conv.kind in {"channel", "private"} else f"DM · {conv.label}"

        if len(batch) == 1:
            snippet, _ = render.render_text(first.text, names)
            snippet = " ".join(snippet.split())[:60] or "(no text)"
            subject = f"{self.cfg.email.subject_prefix} {where} — {first.author}: {snippet}"
        else:
            subject = f"{self.cfg.email.subject_prefix} {where} — {len(batch)} messages"

        plain_parts = [f"{where}  ({first.workspace})", "=" * 56, ""]
        html_parts = [
            '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'font-size:14px;line-height:1.45;color:#1d1c1d">',
            f'<div style="font-weight:700;font-size:15px">{html_mod.escape(where)}'
            f'<span style="font-weight:400;color:#616061"> · '
            f"{html_mod.escape(first.workspace)}</span></div>",
            '<hr style="border:none;border-top:1px solid #ddd;margin:8px 0">',
        ]

        for item in batch:
            when = render.ts_to_datetime(item.ts, tz).strftime("%a %-I:%M %p")
            in_thread = " (in thread)" if item.thread_ts and item.thread_ts != item.ts else ""
            link = render.permalink(item.team_domain, conv.channel_id, item.ts, item.thread_ts)
            body_plain, body_html = render.render_text(item.text, names)
            extra_plain, extra_html = render.extras(item.event, names)

            plain_parts.append(f"{item.author} · {when}{in_thread}")
            plain_parts.extend(f"  {line}" for line in (body_plain or "(no text)").split("\n"))
            plain_parts.extend(f"  {line}" for line in extra_plain)
            if link:
                plain_parts.append(f"  → {link}")
            plain_parts.append("")

            html_parts.append('<div style="margin:0 0 14px 0">')
            html_parts.append(
                f'<div><span style="font-weight:700">{html_mod.escape(item.author)}</span>'
                f'<span style="color:#616061;font-size:12px"> {html_mod.escape(when)}'
                f"{html_mod.escape(in_thread)}</span></div>"
            )
            html_parts.append(f'<div style="margin:2px 0">{body_html or "<i>(no text)</i>"}</div>')
            html_parts.extend(f'<div style="margin:2px 0">{line}</div>' for line in extra_html)
            if link:
                html_parts.append(
                    f'<div style="font-size:12px"><a href="{html_mod.escape(link, quote=True)}"'
                    ' style="color:#1264a3;text-decoration:none">Open in Slack ↗</a></div>'
                )
            html_parts.append("</div>")

        html_parts.append("</div>")

        msg = self.mailer.build(
            subject=subject,
            plain="\n".join(plain_parts),
            html="\n".join(html_parts),
            conversation_key=conv.key.replace(":", "."),
            message_key=f"{conv.key.replace(':', '.')}.{first.ts}",
        )
        if self.mailer.send(msg):
            self.stats.bump("emails")
            log.info("emailed %d message(s) from %s", len(batch), where)
        self.seen.flush()
