"""MockEmailProvider: default (non---live) EmailProvider.

Returns a canned verification message immediately -- no real inbox, no
real polling delay. Exists so PROVISION's control flow can be built and
tested without live email infra, per the locked stubbed-by-default
scoping decision (see DECISIONS.md D-031).
"""

import uuid
from datetime import datetime, timezone

from .email import EmailMessage


class MockEmailProvider:
    def __init__(self, *, alias_domain: str = "example.com") -> None:
        self._alias_domain = alias_domain

    def alias_for(self, identity_key: str) -> str:
        slug = identity_key.replace(":", "-").replace(".", "-")
        return f"credforge+{slug}@{self._alias_domain}"

    async def wait_for_message(
        self,
        *,
        to_addr: str,
        subject_contains: str | None = None,
        from_contains: str | None = None,
        timeout_s: float = 120,
        poll_interval_s: float = 5,
    ) -> EmailMessage:
        return EmailMessage(
            message_id=f"mock-{uuid.uuid4().hex[:12]}",
            from_addr=from_contains or "noreply@vendor.example",
            to_addr=to_addr,
            subject=subject_contains or "Verify your email",
            body_text="Mock verification email. Verify: https://vendor.example/verify/mock-token",
            received_at=datetime.now(timezone.utc),
        )
