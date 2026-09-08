from slack2email.config import ForwardConfig
from slack2email.state import SeenStore
from slack2email.watcher import ConvInfo, _prettify_mpim, channel_allowed, should_forward

ME = "UME"


def test_plain_message_forwarded():
    assert should_forward({"user": "U1", "text": "hi"}, ForwardConfig(), ME)


def test_own_message_skipped_by_default():
    assert not should_forward({"user": ME, "text": "hi"}, ForwardConfig(), ME)


def test_own_message_forwarded_when_enabled():
    assert should_forward({"user": ME, "text": "hi"}, ForwardConfig(own_messages=True), ME)


def test_join_noise_skipped():
    assert not should_forward({"user": "U1", "subtype": "channel_join"}, ForwardConfig(), ME)


def test_edit_events_skipped():
    assert not should_forward({"subtype": "message_changed"}, ForwardConfig(), ME)


def test_hidden_skipped():
    assert not should_forward({"user": "U1", "hidden": True}, ForwardConfig(), ME)


def test_file_share_forwarded():
    assert should_forward({"user": "U1", "subtype": "file_share"}, ForwardConfig(), ME)


def test_bot_message_toggle():
    event = {"subtype": "bot_message", "username": "GitHub"}
    assert should_forward(event, ForwardConfig(), ME)
    assert not should_forward(event, ForwardConfig(bot_messages=False), ME)


def test_noise_flag_lets_everything_through():
    assert should_forward({"user": "U1", "subtype": "channel_join"}, ForwardConfig(noise=True), ME)


def _conv(label="#general", cid="C1"):
    return ConvInfo(key="T1:C1", kind="channel", label=label, channel_id=cid)


def test_allowlist_by_name():
    fwd = ForwardConfig(channel_allowlist=["#general"])
    assert channel_allowed(_conv(), fwd)
    assert not channel_allowed(_conv("#random", "C2"), fwd)


def test_allowlist_by_id():
    assert channel_allowed(_conv(), ForwardConfig(channel_allowlist=["C1"]))


def test_blocklist():
    assert not channel_allowed(_conv(), ForwardConfig(channel_blocklist=["general"]))


def test_no_lists_allows_everything():
    assert channel_allowed(_conv(), ForwardConfig())


def test_prettify_mpim():
    assert _prettify_mpim("mpdm-alice--bob--carol-1") == "alice, bob, carol"


def test_seen_store_dedupes_and_persists(tmp_path):
    path = tmp_path / "seen.json"
    store = SeenStore(path)
    assert store.add_if_new("T1:C1:100.1")
    assert not store.add_if_new("T1:C1:100.1")
    store.flush()

    reopened = SeenStore(path)
    assert not reopened.add_if_new("T1:C1:100.1")
    assert reopened.add_if_new("T1:C1:100.2")


def test_seen_store_survives_corrupt_file(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    assert SeenStore(path).add_if_new("k")


def test_seen_store_is_bounded(tmp_path):
    store = SeenStore(tmp_path / "seen.json", max_items=3)
    for i in range(5):
        store.add_if_new(f"k{i}")
    assert store.add_if_new("k0")   # evicted, so it looks new again
    assert not store.add_if_new("k4")
