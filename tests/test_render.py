import pytest

from slack2email import render


class FakeNames(render.Names):
    def __init__(self):
        super().__init__(client=None)
        self._users = {"U1": "Alice Smith", "U2": "bob"}
        self._channels = {"C1": "general"}


@pytest.fixture
def names():
    return FakeNames()


def test_user_mention_resolves(names):
    plain, html = render.render_text("hey <@U1> ping", names)
    assert plain == "hey @Alice Smith ping"
    assert "<b>@Alice Smith</b>" in html


def test_channel_mention_resolves(names):
    plain, _ = render.render_text("see <#C1|general>", names)
    assert plain == "see #general"


def test_link_with_label(names):
    plain, html = render.render_text("<https://example.com|the docs>", names)
    assert plain == "the docs"
    assert '<a href="https://example.com">the docs</a>' in html


def test_bare_link(names):
    plain, html = render.render_text("<https://example.com/a?b=1&amp;c=2>", names)
    assert plain == "https://example.com/a?b=1&c=2"
    assert "b=1&amp;c=2" in html


def test_specials(names):
    plain, _ = render.render_text("<!here> and <!subteam^S1|@team>", names)
    assert plain == "@here and @team"


def test_slack_entities_unescaped_in_plain(names):
    plain, _ = render.render_text("5 &lt; 6 &amp;&amp; 7 &gt; 6", names)
    assert plain == "5 < 6 && 7 > 6"


def test_html_output_is_escaped(names):
    _, html = render.render_text("&lt;script&gt;alert(1)&lt;/script&gt;", names)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_injection_via_link_label_is_escaped(names):
    _, html = render.render_text('<https://x.com|<img src=x onerror=1>>', names)
    assert "<img" not in html


def test_mrkdwn_styles(names):
    _, html = render.render_text("*bold* _it_ ~no~ `code`", names)
    assert "<strong>bold</strong>" in html
    assert "<em>it</em>" in html
    assert "<del>no</del>" in html
    assert "<code>code</code>" in html


def test_code_block_is_not_restyled(names):
    _, html = render.render_text("```a *b* c```", names)
    assert "<pre><code>" in html
    assert "<strong>" not in html


def test_blockquote(names):
    _, html = render.render_text("&gt; quoted", names)
    assert "border-left" in html


def test_empty_text(names):
    assert render.render_text("", names) == ("", "")


def test_permalink_and_thread():
    assert render.permalink("acme", "C1", "1712345678.000200") == (
        "https://acme.slack.com/archives/C1/p1712345678000200"
    )
    assert "thread_ts=1712345600.000100" in render.permalink(
        "acme", "C1", "1712345678.000200", "1712345600.000100"
    )
    assert render.permalink("", "C1", "1") == ""


def test_ts_to_datetime_utc():
    dt = render.ts_to_datetime("1712345678.000200", "UTC")
    assert dt.strftime("%Y-%m-%d %H:%M") == "2024-04-05 19:34"


def test_extras_renders_files(names):
    plain, html = render.extras(
        {"files": [{"name": "notes.pdf", "permalink": "https://x/f", "size": 1234}]}, names
    )
    assert "notes.pdf" in plain[0] and "https://x/f" in plain[0]
    assert 'href="https://x/f"' in html[0]


def test_extras_renders_attachment_text(names):
    plain, _ = render.extras({"attachments": [{"title": "T", "text": "hello <@U1>"}]}, names)
    assert "@Alice Smith" in plain[0]
