import pytest

from credforge.providers.email import EmailTimeoutError
from credforge.providers.imap_email import ImapEmailProvider


def _provider() -> ImapEmailProvider:
    return ImapEmailProvider(
        host="imap.example.com", port=993, username="creds@example.com", password="secret", alias_domain="creds.example"
    )


def test_alias_for_uses_username_local_part_and_alias_domain() -> None:
    provider = _provider()
    assert provider.alias_for("github.com") == "creds+github-com@creds.example"


@pytest.mark.asyncio
async def test_wait_for_message_raises_email_timeout_error_when_nothing_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    # Failure drill: a real inbox that never receives the expected message
    # must surface a typed, catchable timeout -- not hang the process.
    provider = _provider()
    monkeypatch.setattr(provider, "_poll_once", lambda *args, **kwargs: None)

    with pytest.raises(EmailTimeoutError):
        await provider.wait_for_message(to_addr="creds+x@creds.example", timeout_s=0.05, poll_interval_s=0.01)


@pytest.mark.asyncio
async def test_wait_for_message_returns_as_soon_as_poll_finds_a_match(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    from credforge.providers.email import EmailMessage

    provider = _provider()
    expected = EmailMessage(
        message_id="m1", from_addr="noreply@vendor.example", to_addr="creds+x@creds.example",
        subject="Verify", body_text="body", received_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(provider, "_poll_once", lambda *args, **kwargs: expected)

    result = await provider.wait_for_message(to_addr="creds+x@creds.example", timeout_s=1, poll_interval_s=0.01)
    assert result is expected
