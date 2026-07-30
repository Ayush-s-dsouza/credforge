"""ImapEmailProvider: real IMAP polling (EmailProvider, --live only).

Uses stdlib imaplib + email -- deliberately no extra dependency. Unlike the
browser side, where real signup automation genuinely needs a real browser
(Playwright, an optional extra), polling an inbox for a message doesn't
need anything beyond the standard library, so it doesn't get one either.
See DECISIONS.md D-031.

Polling, not IMAP IDLE/push: simpler, and it's what wait_for_message's own
signature already commits to (poll_interval_s as a caller-supplied knob).
Each poll opens a fresh connection rather than holding one open across the
whole wait -- avoids a real-world IMAP server timing out an idle
connection during a long verification wait, at the cost of one login per
poll. Acceptable at this call volume (one wait per provisioned app).
"""

import asyncio
import email as email_module
import imaplib
from datetime import datetime, timezone
from email.message import Message

from .email import EmailMessage, EmailTimeoutError


class ImapEmailProvider:
    def __init__(self, *, host: str, port: int, username: str, password: str, alias_domain: str) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._alias_domain = alias_domain

    def alias_for(self, identity_key: str) -> str:
        local_part = self._username.split("@")[0]
        slug = identity_key.replace(":", "-").replace(".", "-")
        return f"{local_part}+{slug}@{self._alias_domain}"

    async def wait_for_message(
        self,
        *,
        to_addr: str,
        subject_contains: str | None = None,
        from_contains: str | None = None,
        timeout_s: float = 120,
        poll_interval_s: float = 5,
    ) -> EmailMessage:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s

        while True:
            message = await asyncio.to_thread(self._poll_once, to_addr, subject_contains, from_contains)
            if message is not None:
                return message
            if loop.time() >= deadline:
                raise EmailTimeoutError(f"no matching message for {to_addr!r} within {timeout_s}s")
            await asyncio.sleep(poll_interval_s)

    def _poll_once(
        self, to_addr: str, subject_contains: str | None, from_contains: str | None
    ) -> EmailMessage | None:
        conn = imaplib.IMAP4_SSL(self._host, self._port)
        try:
            conn.login(self._username, self._password)
            conn.select("INBOX")
            status, data = conn.search(None, "TO", f'"{to_addr}"')
            if status != "OK" or not data or not data[0]:
                return None

            for msg_id in reversed(data[0].split()):  # newest first
                fetch_status, msg_data = conn.fetch(msg_id, "(RFC822)")
                if fetch_status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                parsed = email_module.message_from_bytes(msg_data[0][1])
                subject = parsed.get("Subject", "")
                from_addr = parsed.get("From", "")
                if subject_contains and subject_contains.lower() not in subject.lower():
                    continue
                if from_contains and from_contains.lower() not in from_addr.lower():
                    continue
                return EmailMessage(
                    message_id=parsed.get("Message-ID", msg_id.decode()),
                    from_addr=from_addr,
                    to_addr=to_addr,
                    subject=subject,
                    body_text=_extract_text(parsed),
                    received_at=datetime.now(timezone.utc),
                )
            return None
        finally:
            try:
                conn.logout()
            except OSError:
                pass


def _extract_text(parsed: Message) -> str:
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = parsed.get_payload(decode=True)
    return payload.decode(parsed.get_content_charset() or "utf-8", errors="replace") if payload else ""
