"""Deterministic enums shared across the credforge pipeline.

Every one of these is a StrEnum so values serialize as plain strings in
JSON output (registry lines, artifacts, logs) without a custom encoder.
"""

from enum import StrEnum


class AuthScheme(StrEnum):
    OAUTH2_AUTH_CODE = "oauth2_auth_code"
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"
    OAUTH2_PKCE = "oauth2_pkce"
    OAUTH1 = "oauth1"
    API_KEY = "api_key"
    BASIC = "basic"
    BEARER_STATIC = "bearer_static"
    NONE = "none"
    NO_PUBLIC_API = "no_public_api"


class Status(StrEnum):
    AUTO = "AUTO"
    HITL = "HITL"
    UNSUPPORTED = "UNSUPPORTED"


class ReasonCode(StrEnum):
    ELIGIBLE_AUTO = "eligible_auto"

    RESOLVE_AMBIGUOUS = "resolve_ambiguous"
    RESOLVE_LOW_CONFIDENCE = "resolve_low_confidence"
    RESOLVE_NOT_FOUND = "resolve_not_found"
    MALFORMED_INPUT = "malformed_input"

    DISCOVERY_FAILED = "discovery_failed"
    DISCOVERY_INCOMPLETE = "discovery_incomplete"

    CLASSIFY_LOW_CONFIDENCE = "classify_low_confidence"

    NO_PUBLIC_API = "no_public_api"
    TOS_UNVERIFIABLE = "tos_unverifiable"
    TOS_PROHIBITS_AUTOMATION = "tos_prohibits_automation"
    REQUIRES_PAYMENT = "requires_payment"
    REQUIRES_BUSINESS_VERIFICATION = "requires_business_verification"
    REQUIRES_SALES_CONTACT = "requires_sales_contact"
    REQUIRES_PHONE_VERIFICATION = "requires_phone_verification"
    REQUIRES_CAPTCHA = "requires_captcha"
    REQUIRES_SSO_ONLY = "requires_sso_only"

    PROVISION_FAILED = "provision_failed"

    VALIDATION_FAILED_BAD_CREDENTIAL = "validation_failed_bad_credential"
    VALIDATION_FAILED_INSUFFICIENT_SCOPE = "validation_failed_insufficient_scope"
    VALIDATION_FAILED_WRONG_BASE_URL = "validation_failed_wrong_base_url"
    VALIDATION_FAILED_CREDENTIAL_EXPIRED = "validation_failed_credential_expired"
    VALIDATION_FAILED_RATE_LIMITED = "validation_failed_rate_limited"
    VALIDATION_FAILED_UNKNOWN = "validation_failed_unknown"

    INTERNAL_ERROR = "internal_error"


class PaginationStyle(StrEnum):
    NONE = "none"
    OFFSET_LIMIT = "offset_limit"
    PAGE_NUMBER = "page_number"
    CURSOR = "cursor"
    LINK_HEADER = "link_header"
    UNKNOWN = "unknown"


class CredentialType(StrEnum):
    OAUTH2_TOKEN_PAIR = "oauth2_token_pair"
    OAUTH1_TOKEN_PAIR = "oauth1_token_pair"
    API_KEY = "api_key"
    BASIC_AUTH = "basic_auth"
    BEARER_TOKEN = "bearer_token"
    NONE = "none"


class PipelineStage(StrEnum):
    RESOLVE = "resolve"
    DISCOVER = "discover"
    CLASSIFY = "classify"
    GATE = "gate"
    PROVISION = "provision"
    VALIDATE = "validate"
    EMIT = "emit"
    REPORT = "report"


class StageStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SourceTier(StrEnum):
    """How authoritative a docs-candidate URL's *shape* is, not its
    content -- HIGH: official API reference. MEDIUM: general docs/guide.
    LOW: everything else (blogs, tutorials, forums). See
    pipeline/source_authority.py and DECISIONS.md D-049."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ApiStyle(StrEnum):
    """The shape of the API DISCOVER actually crawled -- REST (many
    resource paths off a base_url) vs. GraphQL (one endpoint, cursor-based
    connections) vs. UNKNOWN (no clear positive signal for either).
    UNKNOWN is the conservative default: it changes nothing about how
    completeness gaps are read, unlike a confirmed GRAPHQL classification.
    credforge's schema (base_url + path-shaped validation_endpoint,
    REST-only VALIDATE) assumes REST by default; GraphQL is only partially
    modelled. See DECISIONS.md D-055."""

    REST = "rest"
    GRAPHQL = "graphql"
    UNKNOWN = "unknown"
