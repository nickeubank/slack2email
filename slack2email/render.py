"""Turn Slack message payloads into readable plain-text and HTML."""

from __future__ import annotations

import html as html_mod
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Slack wraps every entity in angle brackets: <@U123>, <#C123|general>, <http://x|y>.
_TOKEN_RE = re.compile(r"<([^<>]*)>")
_SENTINEL = "\x00{}\x00"
_SENTINEL_RE = re.compile(r"\x00(\d+)\x00")

_CODE_BLOCK_RE = re.compile(r"```(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC_RE = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE_RE = re.compile(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])")


def slack_unescape(text: str) -> str:
    """Undo Slack's three escaped characters. Order matters: & must come last."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def ts_to_datetime(ts: str, tz_name: str = "") -> datetime:
    """Convert a Slack ``ts`` ("1712345678.000200") to an aware local datetime."""
    seconds = float(str(ts).split(".")[0] or 0)
    aware = datetime.fromtimestamp(seconds, tz=timezone.utc)
    if tz_name:
        try:
            return aware.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    return aware.astimezone()


def permalink(team_domain: str, channel_id: str, ts: str, thread_ts: str = "") -> str:
    """Build an archive URL without spending an API call on chat.getPermalink."""
    if not team_domain or not channel_id or not ts:
        return ""
    url = f"https://{team_domain}.slack.com/archives/{channel_id}/p{str(ts).replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel_id}"
    return url


class Names:
    """Lazily resolves and caches user/channel display names."""

    def __init__(self, client=None):
        self._client = client
        self._users: dict[str, str] = {}
        self._channels: dict[str, str] = {}

    def user(self, user_id: str) -> str:
        if not user_id:
            return "someone"
        if user_id not in self._users:
            name = user_id
            if self._client is not None:
                try:
                    profile = self._client.users_info(user=user_id)["user"]
                    name = (
                        profile.get("profile", {}).get("display_name")
                        or profile.get("profile", {}).get("real_name")
                        or profile.get("real_name")
                        or profile.get("name")
                        or user_id
                    )
                except Exception:
                    pass
            self._users[user_id] = name
        return self._users[user_id]

    def channel(self, channel_id: str) -> str:
        if not channel_id:
            return "unknown"
        if channel_id not in self._channels:
            name = channel_id
            if self._client is not None:
                try:
                    name = self._client.conversations_info(channel=channel_id)["channel"].get(
                        "name", channel_id
                    )
                except Exception:
                    pass
            self._channels[channel_id] = name
        return self._channels[channel_id]

    def prime_user(self, user_id: str, name: str) -> None:
        self._users[user_id] = name


def _render_token(body: str, names: Names) -> tuple[str, str]:
    """Render one <...> entity as (plain, html)."""
    target, sep, label = body.partition("|")

    if target.startswith("@"):
        shown = label or names.user(target[1:].split("^")[0])
        text = "@" + shown.lstrip("@")
        return text, f"<b>{html_mod.escape(text)}</b>"

    if target.startswith("#"):
        shown = label or names.channel(target[1:].split("|")[0])
        text = "#" + shown.lstrip("#")
        return text, f"<b>{html_mod.escape(text)}</b>"

    if target.startswith("!"):
        special = target[1:]
        if special.startswith("subteam^"):
            text = "@" + (label or special.split("^", 1)[1]).lstrip("@")
        elif special.startswith("date^"):
            # <!date^1712345678^{date_short} at {time}|fallback>
            text = label or special
        elif special in {"here", "channel", "everyone"}:
            text = "@" + special
        else:
            text = label or "@" + special
        return text, f"<b>{html_mod.escape(text)}</b>"

    url = slack_unescape(target)
    shown = slack_unescape(label) if sep else url
    if url.startswith("mailto:"):
        shown = shown or url[7:]
    safe_url = html_mod.escape(url, quote=True)
    return shown, f'<a href="{safe_url}">{html_mod.escape(shown)}</a>'


def _mrkdwn_to_html(escaped: str) -> str:
    """Apply Slack's mrkdwn styling to already-HTML-escaped text."""
    protected: list[str] = []

    def protect(fragment: str) -> str:
        protected.append(fragment)
        return f"\x01{len(protected) - 1}\x01"

    def _block(m):
        inner = m.group(1).strip("\n")
        return protect(f"<pre><code>{inner}</code></pre>")

    escaped = _CODE_BLOCK_RE.sub(_block, escaped)
    escaped = _INLINE_CODE_RE.sub(lambda m: protect(f"<code>{m.group(1)}</code>"), escaped)

    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC_RE.sub(r"<em>\1</em>", escaped)
    escaped = _STRIKE_RE.sub(r"<del>\1</del>", escaped)

    lines = []
    for line in escaped.split("\n"):
        if line.startswith("&gt;"):
            lines.append(
                '<span style="border-left:3px solid #ccc;padding-left:8px;color:#555">'
                f"{line[4:].lstrip()}</span>"
            )
        else:
            lines.append(line)
    escaped = "<br>\n".join(lines)

    return re.sub(r"\x01(\d+)\x01", lambda m: protected[int(m.group(1))], escaped)


def render_text(text: str, names: Names) -> tuple[str, str]:
    """Render Slack message text as (plain, html)."""
    if not text:
        return "", ""

    plains: list[str] = []
    html_tokens: list[str] = []
    cursor = 0
    with_sentinels: list[str] = []

    for match in _TOKEN_RE.finditer(text):
        # Unescape before the HTML pass, or Slack's own &lt; becomes &amp;lt;.
        literal = slack_unescape(text[cursor : match.start()])
        plains.append(literal)
        with_sentinels.append(literal)
        plain_tok, html_tok = _render_token(match.group(1), names)
        plains.append(plain_tok)
        html_tokens.append(html_tok)
        with_sentinels.append(_SENTINEL.format(len(html_tokens) - 1))
        cursor = match.end()

    tail = slack_unescape(text[cursor:])
    plains.append(tail)
    with_sentinels.append(tail)

    plain = "".join(plains)
    escaped = html_mod.escape("".join(with_sentinels))
    body_html = _mrkdwn_to_html(escaped)
    body_html = _SENTINEL_RE.sub(lambda m: html_tokens[int(m.group(1))], body_html)
    return plain, body_html


def extras(event: dict, names: Names) -> tuple[list[str], list[str]]:
    """Render files and attachments that carry content beyond ``text``."""
    plain_lines: list[str] = []
    html_lines: list[str] = []

    for f in event.get("files") or []:
        name = f.get("name") or f.get("title") or "file"
        link = f.get("permalink") or f.get("url_private") or ""
        size = f.get("size")
        suffix = f" ({size:,} bytes)" if isinstance(size, int) else ""
        plain_lines.append(f"[file] {name}{suffix} {link}".rstrip())
        safe_name = html_mod.escape(name)
        if link:
            html_lines.append(
                f'📎 <a href="{html_mod.escape(link, quote=True)}">{safe_name}</a>{suffix}'
            )
        else:
            html_lines.append(f"📎 {safe_name}{suffix}")

    for att in event.get("attachments") or []:
        parts = [att.get("title"), att.get("text") or att.get("fallback")]
        blob = "\n".join(p for p in parts if p)
        if not blob:
            continue
        att_plain, att_html = render_text(blob, names)
        plain_lines.append("| " + att_plain.replace("\n", "\n| "))
        html_lines.append(
            '<div style="border-left:3px solid #ddd;padding-left:8px;margin:4px 0">'
            f"{att_html}</div>"
        )

    return plain_lines, html_lines
