"""VALIDATE: confirms a provisioned credential actually works, via one or
more real read-only HTTP round-trips against the vendor's API.

Classification is a small deterministic heuristic over the HTTP status
code (plus a word-boundary-safe "expired" check on the response body to
distinguish an expired credential from a merely-invalid one) -- not an LLM
call. Status-code semantics are already effectively enumerable (401/403/
404/429 mean roughly the same thing across virtually every REST API), so
this is the same "a small heuristic formula, not an LLM call" reasoning as
D-009's confidence scoring, applied to a second already-classifiable
problem. See DECISIONS.md D-032.

Safety-critical design choice: VALIDATE always issues a GET, regardless of
the HTTP method DISCOVER's `validation_endpoint` field names. DISCOVER's
extraction is prose-derived and has already been observed (live, on
Salesforce) to name a *create* endpoint ("POST /contacts/v1/contacts") as
its best guess at a "cheap" endpoint to check. Blindly replaying that
method would mean VALIDATE creates a real contact record as a side effect
of merely checking whether a credential works -- exactly the kind of
surprising, unrequested write this project's repeated "do the safe
minimal thing, fail closed, document the boundary" pattern (robots.txt
fail-closed, response size caps, the ToS-URL guess list) exists to avoid.
See DECISIONS.md D-032.
"""

import re
from urllib.parse import urljoin

from ..enums import ReasonCode
from ..models.state import ValidateResult
from ..providers.fetch import FetchException, FetchProvider
from ..redaction import register_secret, scrub_secrets

_EXPIRED_RE = re.compile(r"\bexpired\b", re.IGNORECASE)

_HTTP_METHOD_PREFIX_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+", re.IGNORECASE)


def _resolve_check_url(validation_endpoint: str, base_url: str | None) -> str | None:
    """Strips a leading HTTP-method token (if any -- see module docstring
    for why the method itself is never honored) and resolves the remaining
    path against base_url. Returns None if the result still isn't an
    absolute URL (no base_url to resolve a relative path against)."""
    path_or_url = _HTTP_METHOD_PREFIX_RE.sub("", validation_endpoint.strip())

    if path_or_url.lower().startswith(("http://", "https://")):
        return path_or_url
    if base_url:
        return urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))
    return None


def _build_auth_headers(auth_scheme: str, credential: dict[str, str]) -> dict[str, str]:
    """One conventional placement per scheme -- not an attempt to cover
    every real vendor's quirks generically (that's the same open problem
    D-031 already declined to solve generically for signup forms).
    API_KEY is handled separately (see `_api_key_check_variants`) because
    it has genuine real-world placement ambiguity across *two* axes
    (header vs. query param) that a single header can't represent;
    everything else here has a de facto standard (Authorization:
    Bearer/Basic)."""
    if auth_scheme == "basic":
        import base64

        user = credential.get("username") or credential.get("client_id", "")
        pw = credential.get("password") or credential.get("client_secret", "")
        encoded = base64.b64encode(f"{user}:{pw}".encode()).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    if auth_scheme == "none":
        return {}
    # oauth2_auth_code / oauth2_client_credentials / oauth2_pkce / bearer_static
    token = credential.get("access_token") or credential.get("token") or credential.get("client_secret", "")
    return {"Authorization": f"Bearer {token}"}


def _api_key_check_variants(check_url: str, credential: dict[str, str]) -> list[tuple[str, dict[str, str]]]:
    """Real-world API_KEY placement varies by vendor on an axis a single
    header can't represent: a query parameter (NASA's real convention --
    `?api_key=...` -- and common across many public APIs) versus a custom
    header (`X-API-Key`). Query param tried first since it's the more
    common convention for bare API-key auth specifically; both are real,
    observed conventions, not a guess at an unknown one. See DECISIONS.md
    D-044."""
    key = credential.get("api_key") or credential.get("key") or next(iter(credential.values()), "")
    separator = "&" if "?" in check_url else "?"
    return [
        (f"{check_url}{separator}api_key={key}", {}),
        (check_url, {"X-API-Key": key}),
    ]


def _classify_status(status_code: int, body: str | None) -> ReasonCode | None:
    if 200 <= status_code < 300:
        return None
    if status_code == 401:
        if body and _EXPIRED_RE.search(body):
            return ReasonCode.VALIDATION_FAILED_CREDENTIAL_EXPIRED
        return ReasonCode.VALIDATION_FAILED_BAD_CREDENTIAL
    if status_code == 403:
        return ReasonCode.VALIDATION_FAILED_INSUFFICIENT_SCOPE
    if status_code == 404:
        return ReasonCode.VALIDATION_FAILED_WRONG_BASE_URL
    if status_code == 429:
        return ReasonCode.VALIDATION_FAILED_RATE_LIMITED
    return ReasonCode.VALIDATION_FAILED_UNKNOWN


async def validate(
    identity_key: str,
    *,
    validation_endpoint: str | None,
    base_url: str | None,
    auth_scheme: str,
    credential: dict[str, str],
    fetch: FetchProvider,
) -> ValidateResult:
    if not validation_endpoint:
        return ValidateResult(
            status="invalid",
            reason_code=ReasonCode.VALIDATION_FAILED_UNKNOWN,
            detail="no validation_endpoint available from DISCOVER -- nothing to check against",
        )

    check_url = _resolve_check_url(validation_endpoint, base_url)
    if check_url is None:
        return ValidateResult(
            status="invalid",
            reason_code=ReasonCode.VALIDATION_FAILED_UNKNOWN,
            detail=f"validation_endpoint {validation_endpoint!r} is a relative path but no base_url is known",
        )

    # Register every credential value before it's ever placed in a URL or
    # header -- the api_key query-param variant embeds the raw secret
    # directly in the request URL, and `checked_url` below is stored in
    # `ValidateResult`/the artifact, which is neither vaulted nor
    # guaranteed redacted downstream. See DECISIONS.md D-045.
    for value in credential.values():
        register_secret(value)

    if auth_scheme == "api_key":
        variants = _api_key_check_variants(check_url, credential)
    else:
        variants = [(check_url, _build_auth_headers(auth_scheme, credential))]

    last_result: ValidateResult | None = None
    for variant_url, variant_headers in variants:
        # scrub_secrets(), not the raw URL -- checked_url is stored in
        # ValidateResult and flows straight into the HandoffArtifact,
        # which is neither vaulted nor guaranteed to pass through
        # `logging` (where RedactionFilter would otherwise catch it).
        # The api_key query-param variant embeds the raw secret directly
        # in the URL; this must never survive into a stored value. See
        # DECISIONS.md D-045.
        safe_url = scrub_secrets(variant_url)

        try:
            result = await fetch.fetch(variant_url, method="GET", headers=variant_headers)
        except FetchException as exc:
            last_result = ValidateResult(
                status="invalid",
                reason_code=ReasonCode.VALIDATION_FAILED_UNKNOWN,
                checked_url=safe_url,
                detail=scrub_secrets(f"{exc.error.reason}: {exc.error.detail or ''}".strip(": ")),
            )
            continue

        reason_code = _classify_status(result.status_code, result.text)
        if reason_code is None:
            return ValidateResult(status="valid", http_status_code=result.status_code, checked_url=safe_url)

        last_result = ValidateResult(
            status="invalid",
            reason_code=reason_code,
            http_status_code=result.status_code,
            checked_url=safe_url,
            detail=f"HTTP {result.status_code} from {safe_url}",
        )
        # A non-credential failure (wrong base URL, rate limited, an
        # unclassifiable error) won't be fixed by trying a different
        # placement of the same key -- only a bad-credential rejection
        # is worth retrying with the next variant.
        if reason_code != ReasonCode.VALIDATION_FAILED_BAD_CREDENTIAL:
            return last_result

    assert last_result is not None  # variants is always non-empty
    return last_result
