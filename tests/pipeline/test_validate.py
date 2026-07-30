from datetime import datetime, timezone

import pytest

from credforge.enums import ReasonCode
from credforge.pipeline.validate import validate
from credforge.providers.fetch import FetchException, FetchError, FetchResult


class FakeFetchProvider:
    def __init__(self, responses: dict[str, FetchResult | Exception] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, str, dict]] = []

    async def fetch(self, url: str, *, method: str = "GET", headers=None) -> FetchResult:
        self.calls.append((url, method, headers or {}))
        outcome = self._responses.get(url)
        if outcome is None:
            raise FetchException(FetchError(url=url, reason="connection_error"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _resp(status_code: int, text: str | None = None) -> FetchResult:
    return FetchResult(
        url="x", final_url="x", status_code=status_code, content_type="application/json",
        text=text, fetched_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_2xx_response_is_valid() -> None:
    fetch = FakeFetchProvider({"https://api.example.com/user": _resp(200)})

    result = await validate(
        "example.com", validation_endpoint="GET /user", base_url="https://api.example.com",
        auth_scheme="bearer_static", credential={"token": "real-token"}, fetch=fetch,
    )

    assert result.status == "valid"
    assert result.reason_code is None
    assert result.http_status_code == 200


@pytest.mark.asyncio
async def test_validate_always_issues_get_even_when_extracted_endpoint_is_post() -> None:
    # The safety-critical behavior under test: a POST-labeled
    # validation_endpoint (e.g. DISCOVER's real Salesforce extraction,
    # "POST /contacts/v1/contacts" -- a create-a-contact endpoint) must
    # never actually be POSTed to. VALIDATE always sends GET.
    fetch = FakeFetchProvider({"https://api.example.com/contacts/v1/contacts": _resp(200)})

    await validate(
        "example.com", validation_endpoint="POST /contacts/v1/contacts", base_url="https://api.example.com",
        auth_scheme="bearer_static", credential={"token": "t"}, fetch=fetch,
    )

    assert len(fetch.calls) == 1
    _, method, _ = fetch.calls[0]
    assert method == "GET"


@pytest.mark.asyncio
async def test_bearer_token_is_sent_as_authorization_header() -> None:
    fetch = FakeFetchProvider({"https://api.example.com/user": _resp(200)})

    await validate(
        "example.com", validation_endpoint="/user", base_url="https://api.example.com",
        auth_scheme="oauth2_client_credentials", credential={"access_token": "abc123"}, fetch=fetch,
    )

    _, _, headers = fetch.calls[0]
    assert headers["Authorization"] == "Bearer abc123"


@pytest.mark.asyncio
async def test_api_key_tries_query_param_first_then_header() -> None:
    # D-044: real API_KEY placement varies across two axes a single
    # header can't cover -- query param (NASA's real convention) is
    # tried first, header second, only if the query-param attempt itself
    # failed (not just "a different URL wasn't in this fake's map").
    fetch = FakeFetchProvider({"https://api.example.com/user": _resp(200)})

    await validate(
        "example.com", validation_endpoint="/user", base_url="https://api.example.com",
        auth_scheme="api_key", credential={"api_key": "key123"}, fetch=fetch,
    )

    assert len(fetch.calls) == 2
    query_url, _, query_headers = fetch.calls[0]
    assert query_url == "https://api.example.com/user?api_key=key123"
    assert query_headers == {}
    header_url, _, header_headers = fetch.calls[1]
    assert header_url == "https://api.example.com/user"
    assert header_headers["X-API-Key"] == "key123"


@pytest.mark.asyncio
async def test_api_key_succeeds_on_query_param_alone_real_nasa_shape() -> None:
    # The real vendor shape that motivated this: NASA's actual API
    # accepts the key only as ?api_key=..., never as a header. A single
    # successful attempt, no fallback needed.
    fetch = FakeFetchProvider({"https://api.nasa.gov/planetary/apod?api_key=DEMO_KEY": _resp(200)})

    result = await validate(
        "nasa.gov", validation_endpoint="GET /planetary/apod", base_url="https://api.nasa.gov",
        auth_scheme="api_key", credential={"api_key": "DEMO_KEY"}, fetch=fetch,
    )

    assert result.status == "valid"
    assert len(fetch.calls) == 1  # succeeded on the first (query-param) attempt -- no fallback needed


@pytest.mark.asyncio
async def test_api_key_stops_retrying_on_a_non_credential_failure() -> None:
    # A 404 (wrong base URL) or a rate limit won't be fixed by moving the
    # same key to a different placement -- only a bad-credential
    # rejection is worth retrying with the next variant.
    fetch = FakeFetchProvider({"https://api.example.com/user?api_key=key123": _resp(404, "Not Found")})

    result = await validate(
        "example.com", validation_endpoint="/user", base_url="https://api.example.com",
        auth_scheme="api_key", credential={"api_key": "key123"}, fetch=fetch,
    )

    assert result.reason_code == ReasonCode.VALIDATION_FAILED_WRONG_BASE_URL
    assert len(fetch.calls) == 1  # never tried the header variant


@pytest.mark.asyncio
async def test_basic_auth_is_base64_encoded() -> None:
    import base64

    fetch = FakeFetchProvider({"https://api.example.com/user": _resp(200)})

    await validate(
        "example.com", validation_endpoint="/user", base_url="https://api.example.com",
        auth_scheme="basic", credential={"client_id": "u", "client_secret": "p"}, fetch=fetch,
    )

    _, _, headers = fetch.calls[0]
    assert headers["Authorization"] == f"Basic {base64.b64encode(b'u:p').decode()}"


@pytest.mark.parametrize(
    "status_code,body,expected_reason",
    [
        (401, "Bad credentials", ReasonCode.VALIDATION_FAILED_BAD_CREDENTIAL),
        (401, "Your access token has expired", ReasonCode.VALIDATION_FAILED_CREDENTIAL_EXPIRED),
        (403, "Forbidden -- missing scope", ReasonCode.VALIDATION_FAILED_INSUFFICIENT_SCOPE),
        (404, "Not Found", ReasonCode.VALIDATION_FAILED_WRONG_BASE_URL),
        (429, "Too Many Requests", ReasonCode.VALIDATION_FAILED_RATE_LIMITED),
        (500, "Internal Server Error", ReasonCode.VALIDATION_FAILED_UNKNOWN),
    ],
)
@pytest.mark.asyncio
async def test_status_code_classification(status_code: int, body: str, expected_reason: ReasonCode) -> None:
    fetch = FakeFetchProvider({"https://api.example.com/user": _resp(status_code, body)})

    result = await validate(
        "example.com", validation_endpoint="/user", base_url="https://api.example.com",
        auth_scheme="bearer_static", credential={"token": "t"}, fetch=fetch,
    )

    assert result.status == "invalid"
    assert result.reason_code == expected_reason


@pytest.mark.asyncio
async def test_network_failure_is_wrapped_not_propagated() -> None:
    # A realistic-length fake token, deliberately -- a single-character
    # one ("t") would be shorter than register_secret()'s minimum-length
    # guard (D-045) and, before that guard existed, would have redacted
    # every letter "t" in this assertion's own expected text.
    fetch = FakeFetchProvider({})  # every fetch fails

    result = await validate(
        "example.com", validation_endpoint="/user", base_url="https://api.example.com",
        auth_scheme="bearer_static", credential={"token": "a-realistic-length-token-value"}, fetch=fetch,
    )

    assert result.status == "invalid"
    assert result.reason_code == ReasonCode.VALIDATION_FAILED_UNKNOWN
    assert "connection_error" in result.detail


@pytest.mark.asyncio
async def test_missing_validation_endpoint_is_reported_not_a_crash() -> None:
    fetch = FakeFetchProvider()

    result = await validate(
        "example.com", validation_endpoint=None, base_url="https://api.example.com",
        auth_scheme="bearer_static", credential={"token": "t"}, fetch=fetch,
    )

    assert result.status == "invalid"
    assert result.reason_code == ReasonCode.VALIDATION_FAILED_UNKNOWN
    assert fetch.calls == []


@pytest.mark.asyncio
async def test_checked_url_never_leaks_the_raw_api_key_from_the_query_param_variant() -> None:
    # D-045: a real security bug, found live against NASA's real API --
    # the query-param placement embeds the raw credential directly in the
    # request URL, and checked_url is stored in ValidateResult (which
    # flows straight into the persisted HandoffArtifact, neither vaulted
    # nor guaranteed to pass through logging's RedactionFilter). The
    # value used for the actual HTTP request must still be the real key
    # (fetch.calls proves that); only the *stored* checked_url must be
    # scrubbed.
    real_key = "a-real-looking-forty-char-api-key-abcdef01"
    fetch = FakeFetchProvider({f"https://api.example.com/user?api_key={real_key}": _resp(200)})

    result = await validate(
        "example.com", validation_endpoint="/user", base_url="https://api.example.com",
        auth_scheme="api_key", credential={"api_key": real_key}, fetch=fetch,
    )

    assert result.status == "valid"
    assert real_key not in result.checked_url
    assert "***REDACTED***" in result.checked_url
    # the real request itself still used the real key -- only the stored value is scrubbed
    assert fetch.calls[0][0] == f"https://api.example.com/user?api_key={real_key}"


@pytest.mark.asyncio
async def test_relative_endpoint_with_no_base_url_is_reported_not_a_crash() -> None:
    # A real, expected scenario per D-029: base_url can legitimately be
    # missing (a completeness_gap, not a blocker) and VALIDATE must
    # degrade gracefully, not raise, when it has nothing to resolve a
    # relative path against.
    fetch = FakeFetchProvider()

    result = await validate(
        "example.com", validation_endpoint="/user", base_url=None,
        auth_scheme="bearer_static", credential={"token": "t"}, fetch=fetch,
    )

    assert result.status == "invalid"
    assert result.reason_code == ReasonCode.VALIDATION_FAILED_UNKNOWN
    assert fetch.calls == []
