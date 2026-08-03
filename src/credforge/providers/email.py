"""EmailProvider: the interface PROVISION uses for signup email verification.

MockEmailProvider (Stage 5, default) returns a canned message immediately.
ImapEmailProvider (Stage 5, --live only) polls a real IMAP inbox. Both
satisfy this same Protocol, so provision.py never branches on which one
it's holding.
"""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class EmailMessage(BaseModel):
    message_id: str
    from_addr: str
    to_addr: str
    subject: str
    body_text: str
    received_at: datetime


class EmailTimeoutError(Exception):
    pass


class EmailProvider(Protocol):
    def alias_for(self, identity_key: str, *, suffix: str | None = None) -> str: ...

    async def wait_for_message(
        self,
        *,
        to_addr: str,
        subject_contains: str | None = None,
        from_contains: str | None = None,
        timeout_s: float = 120,
        poll_interval_s: float = 5,
    ) -> EmailMessage: ...
