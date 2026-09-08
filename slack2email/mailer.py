"""SMTP delivery, with per-conversation mail threading and a failure spool."""

from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .config import EmailConfig, SmtpConfig

log = logging.getLogger(__name__)

MAIL_DOMAIN = "slack2email.local"


class Mailer:
    def __init__(self, smtp: SmtpConfig, email: EmailConfig, spool_dir: Path | None = None):
        self.smtp = smtp
        self.email = email
        self.spool_dir = spool_dir

    def _connect(self) -> smtplib.SMTP:
        if self.smtp.security == "ssl":
            server = smtplib.SMTP_SSL(self.smtp.host, self.smtp.port, timeout=self.smtp.timeout)
        else:
            server = smtplib.SMTP(self.smtp.host, self.smtp.port, timeout=self.smtp.timeout)
            if self.smtp.security == "starttls":
                server.starttls()
        if self.smtp.username and self.smtp.password:
            server.login(self.smtp.username, self.smtp.password)
        return server

    def build(
        self,
        *,
        subject: str,
        plain: str,
        html: str,
        conversation_key: str = "",
        message_key: str = "",
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.email.sender
        msg["To"] = self.email.to
        msg["Date"] = formatdate(localtime=True)
        msg["Auto-Submitted"] = "auto-generated"
        msg["X-Mailer"] = "slack2email"

        if message_key:
            msg["Message-ID"] = f"<s2e.{message_key}@{MAIL_DOMAIN}>"
        else:
            msg["Message-ID"] = make_msgid(domain=MAIL_DOMAIN)

        # Point every mail for one Slack conversation at a common synthetic root so
        # Gmail and Apple Mail collapse them into a single thread.
        if conversation_key and self.email.thread_by_conversation:
            root = f"<s2e-conv.{conversation_key}@{MAIL_DOMAIN}>"
            if msg["Message-ID"] != root:
                msg["In-Reply-To"] = root
                msg["References"] = root

        msg.set_content(plain or "(no text)")
        if html:
            msg.add_alternative(html, subtype="html")
        return msg

    def send(self, msg: EmailMessage, *, attempts: int = 3) -> bool:
        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                with self._connect() as server:
                    server.send_message(msg)
                return True
            except Exception as exc:
                if attempt == attempts:
                    log.error("giving up on %r after %d attempts: %s", msg["Subject"], attempts, exc)
                    self._spool(msg)
                    return False
                log.warning("send failed (attempt %d/%d): %s", attempt, attempts, exc)
                time.sleep(delay)
                delay *= 2
        return False

    def _spool(self, msg: EmailMessage) -> None:
        """Never silently drop a message: write undeliverable mail to disk."""
        if not self.spool_dir:
            return
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            path = self.spool_dir / f"{time.time():.6f}.eml"
            path.write_bytes(bytes(msg))
            log.error("spooled undelivered message to %s", path)
        except OSError as exc:
            log.error("could not spool undelivered message: %s", exc)

    def verify(self) -> None:
        """Raise if SMTP host/credentials don't work."""
        with self._connect():
            pass
