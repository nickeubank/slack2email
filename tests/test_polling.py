"""Cover the polling fallback without touching the network."""

import queue
import threading

import pytest
from slack_sdk.errors import SlackApiError

from slack2email import render
from slack2email.config import Config, EmailConfig, ForwardConfig, SmtpConfig
from slack2email.state import Cursors, SeenStore
from slack2email.watcher import ConvInfo, PollingListener, Stats


class FakeResponse(dict):
    def __init__(self, data, status_code=200, headers=None):
        super().__init__(data)
        self.status_code = status_code
        self.headers = headers or {}


class FakeWeb:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def conversations_history(self, **kwargs):
        self.calls.append(kwargs)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def make_listener(tmp_path, pages, **forward_kw):
    cfg = Config(
        smtp=SmtpConfig(),
        email=EmailConfig(to="me@example.com"),
        forward=ForwardConfig(mode="poll", **forward_kw),
        workspaces=[],
    )
    obj = object.__new__(PollingListener)
    obj.cfg = cfg
    obj.web = FakeWeb(pages)
    obj.out = queue.Queue()
    obj.seen = SeenStore(tmp_path / "seen.json")
    obj.stats = Stats()
    obj.names = render.Names(None)
    obj.team_id = "T1"
    obj.team_name = "Acme"
    obj.user_id = "UME"
    obj.team_domain = "acme"
    obj.cursors = Cursors(tmp_path / "cursors.json")
    obj._stop = threading.Event()
    obj._conv_cache = {
        "C1": ConvInfo(key="T1:C1", kind="channel", label="#general", channel_id="C1")
    }
    return obj


def test_first_sight_sets_cursor_without_replaying_history(tmp_path):
    listener = make_listener(tmp_path, pages=[])
    listener._poll_channel("C1")
    assert listener.out.empty()
    assert listener.cursors.get("T1:C1") is not None
    assert listener.web.calls == []  # no history call at all


def test_messages_after_cursor_are_forwarded(tmp_path):
    pages = [FakeResponse({"messages": [
        {"ts": "200.0", "user": "U1", "text": "second"},
        {"ts": "100.0", "user": "U1", "text": "first"},
    ], "has_more": False})]
    listener = make_listener(tmp_path, pages)
    listener.cursors.set("T1:C1", "50.0")
    listener._poll_channel("C1")

    got = [listener.out.get_nowait() for _ in range(2)]
    assert [p.text for p in got] == ["first", "second"]   # oldest first
    assert listener.cursors.get("T1:C1") == "200.0"
    assert listener.web.calls[0]["oldest"] == "50.0"
    assert listener.web.calls[0]["inclusive"] is False


def test_own_messages_filtered_but_cursor_still_advances(tmp_path):
    pages = [FakeResponse({"messages": [{"ts": "300.0", "user": "UME", "text": "mine"}]})]
    listener = make_listener(tmp_path, pages)
    listener.cursors.set("T1:C1", "50.0")
    listener._poll_channel("C1")
    assert listener.out.empty()
    assert listener.cursors.get("T1:C1") == "300.0"


def test_pagination_is_followed(tmp_path):
    pages = [
        FakeResponse({"messages": [{"ts": "100.0", "user": "U1", "text": "a"}],
                      "has_more": True,
                      "response_metadata": {"next_cursor": "c2"}}),
        FakeResponse({"messages": [{"ts": "200.0", "user": "U1", "text": "b"}], "has_more": False}),
    ]
    listener = make_listener(tmp_path, pages, poll_spacing_seconds=0)
    listener.cursors.set("T1:C1", "50.0")
    listener._poll_channel("C1")
    assert listener.out.qsize() == 2
    assert listener.web.calls[1]["cursor"] == "c2"


def test_rate_limit_backs_off_and_does_not_advance_cursor(tmp_path):
    err = SlackApiError("rate limited", FakeResponse({"error": "ratelimited"},
                                                     status_code=429,
                                                     headers={"Retry-After": "0"}))
    listener = make_listener(tmp_path, [err])
    listener.cursors.set("T1:C1", "50.0")
    listener._poll_channel("C1")
    assert listener.out.empty()
    assert listener.cursors.get("T1:C1") == "50.0"


def test_channel_errors_are_survivable(tmp_path):
    err = SlackApiError("nope", FakeResponse({"error": "channel_not_found"}, status_code=200))
    listener = make_listener(tmp_path, [err])
    listener.cursors.set("T1:C1", "50.0")
    listener._poll_channel("C1")
    assert listener.out.empty()


def test_duplicate_messages_are_not_resent(tmp_path):
    msg = {"ts": "100.0", "user": "U1", "text": "once"}
    listener = make_listener(tmp_path, [FakeResponse({"messages": [msg]}),
                                        FakeResponse({"messages": [msg]})])
    listener.cursors.set("T1:C1", "50.0")
    listener._poll_channel("C1")
    listener.cursors.set("T1:C1", "50.0")  # simulate a cursor that didn't advance
    listener._poll_channel("C1")
    assert listener.out.qsize() == 1


def test_cursor_never_moves_backwards(tmp_path):
    c = Cursors(tmp_path / "c.json")
    c.set("k", "200.0")
    c.set("k", "100.0")
    assert c.get("k") == "200.0"


def test_cursors_persist(tmp_path):
    c = Cursors(tmp_path / "c.json")
    c.set("k", "1.0")
    c.flush()
    assert Cursors(tmp_path / "c.json").get("k") == "1.0"
