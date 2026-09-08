from slack2email.config import EmailConfig, SmtpConfig
from slack2email.mailer import Mailer


def _mailer(**kw):
    email = EmailConfig(to="me@example.com", sender="me@example.com", **kw)
    return Mailer(SmtpConfig(), email)


def test_build_sets_headers():
    msg = _mailer().build(subject="s", plain="p", html="<p>h</p>")
    assert msg["To"] == "me@example.com"
    assert msg["Subject"] == "s"
    assert msg["Auto-Submitted"] == "auto-generated"


def test_conversation_threading_headers_match_across_messages():
    mailer = _mailer()
    a = mailer.build(subject="s", plain="p", html="", conversation_key="T1.C1", message_key="T1.C1.1")
    b = mailer.build(subject="s", plain="p", html="", conversation_key="T1.C1", message_key="T1.C1.2")
    assert a["Message-ID"] != b["Message-ID"]
    assert a["References"] == b["References"]


def test_threading_can_be_disabled():
    msg = _mailer(thread_by_conversation=False).build(
        subject="s", plain="p", html="", conversation_key="T1.C1", message_key="T1.C1.1"
    )
    assert msg["References"] is None


def test_multipart_alternative_when_html_present():
    msg = _mailer().build(subject="s", plain="p", html="<p>h</p>")
    assert msg.get_content_type() == "multipart/alternative"
    assert {p.get_content_type() for p in msg.iter_parts()} == {"text/plain", "text/html"}


def test_spool_written_when_send_fails(tmp_path):
    email = EmailConfig(to="me@example.com", sender="me@example.com")
    mailer = Mailer(SmtpConfig(host="127.0.0.1", port=1, timeout=1), email, spool_dir=tmp_path)
    msg = mailer.build(subject="s", plain="p", html="")
    assert mailer.send(msg, attempts=1) is False
    assert list(tmp_path.glob("*.eml"))
