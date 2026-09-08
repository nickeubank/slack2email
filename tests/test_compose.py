"""Exercise the full compose path with a stub mailer (no network)."""

import pytest

from slack2email.config import Config, EmailConfig, ForwardConfig, SmtpConfig
from slack2email.mailer import Mailer
from slack2email.state import SeenStore
from slack2email.watcher import ConvInfo, Forwarder, Pending


class RecordingMailer(Mailer):
    def __init__(self, cfg):
        super().__init__(cfg.smtp, cfg.email)
        self.sent = []

    def send(self, msg, attempts=3):
        self.sent.append(msg)
        return True


def _cfg(**forward_kw):
    return Config(
        smtp=SmtpConfig(),
        email=EmailConfig(to="me@example.com", sender="me@example.com"),
        forward=ForwardConfig(timezone="UTC", **forward_kw),
        workspaces=[],
    )


def _pending(text="hello <@U1>", ts="1712345678.000200", author="Alice", kind="channel",
             label="#general", thread_ts="", event=None):
    return Pending(
        conv=ConvInfo(key="T1:C1", kind=kind, label=label, channel_id="C1"),
        author=author, ts=ts, thread_ts=thread_ts, text=text,
        event=event or {}, team_domain="acme", workspace="Acme",
    )


@pytest.fixture
def forwarder(tmp_path):
    cfg = _cfg()
    mailer = RecordingMailer(cfg)
    return Forwarder(cfg, mailer, SeenStore(tmp_path / "seen.json"))


def test_single_message_subject_has_snippet(forwarder):
    forwarder._send([_pending(text="ship it")])
    msg = forwarder.mailer.sent[0]
    assert msg["Subject"] == "[Slack] #general — Alice: ship it"


def test_digest_subject_counts_messages(forwarder):
    forwarder._send([_pending(), _pending(ts="1712345679.0"), _pending(ts="1712345680.0")])
    assert forwarder.mailer.sent[0]["Subject"] == "[Slack] #general — 3 messages"


def test_dm_subject_is_labelled(forwarder):
    forwarder._send([_pending(kind="im", label="Alice Smith", text="hi")])
    assert forwarder.mailer.sent[0]["Subject"] == "[Slack] DM · Alice Smith — Alice: hi"


def test_body_contains_author_time_and_permalink(forwarder):
    forwarder._send([_pending(text="ship it")])
    plain = forwarder.mailer.sent[0].get_body(("plain",)).get_content()
    assert "Alice" in plain
    assert "https://acme.slack.com/archives/C1/p1712345678000200" in plain
    assert "ship it" in plain


def test_thread_replies_are_marked(forwarder):
    forwarder._send([_pending(thread_ts="1712345600.000100")])
    plain = forwarder.mailer.sent[0].get_body(("plain",)).get_content()
    assert "(in thread)" in plain


def test_html_part_is_built_and_links_out(forwarder):
    forwarder._send([_pending(text="see *this*")])
    html = forwarder.mailer.sent[0].get_body(("html",)).get_content()
    assert "<strong>this</strong>" in html
    assert "Open in Slack" in html


def test_empty_message_still_renders(forwarder):
    forwarder._send([_pending(text="")])
    plain = forwarder.mailer.sent[0].get_body(("plain",)).get_content()
    assert "(no text)" in plain


def test_file_attachment_appears(forwarder):
    forwarder._send([_pending(text="", event={"files": [{"name": "a.pdf", "permalink": "https://x"}]})])
    plain = forwarder.mailer.sent[0].get_body(("plain",)).get_content()
    assert "a.pdf" in plain


def test_stats_are_counted(forwarder):
    forwarder._send([_pending()])
    assert forwarder.stats.emails == 1
