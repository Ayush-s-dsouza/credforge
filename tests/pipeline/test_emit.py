from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from credforge.enums import AuthScheme, CredentialType, ReasonCode, SourceTier, Status
from credforge.models.artifact import HandoffArtifact
from credforge.models.state import (
    AppPipelineState,
    ClassifyResult,
    CompletenessGap,
    DiscoveryResult,
    EvidenceItem,
    GateResult,
    ProvisionResult,
    ResolveCandidate,
    ResolveResult,
    ValidateResult,
)
from credforge.pipeline.emit import EmitError, build_artifact
from credforge.providers.llm import DiscoveryExtraction


def _base_state(**overrides) -> AppPipelineState:
    defaults = dict(
        app_name="GitHub",
        input_key="github",
        identity_key="github.com",
        run_id="run_1",
        started_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return AppPipelineState(**defaults)


def _resolved() -> ResolveResult:
    return ResolveResult(
        resolved=True,
        chosen=ResolveCandidate(
            domain="github.com",
            docs_url="https://docs.github.com/en/rest",
            confidence=0.98,
            evidence_url="https://github.com/",
            evidence_snippet="GitHub is where you belong.",
        ),
    )


def _discovered(
    has_public_api: bool = True,
    *,
    source_tier: SourceTier | None = None,
    source_tier_reason: str | None = None,
) -> DiscoveryResult:
    return DiscoveryResult(
        reason_code=None,
        docs_url="https://docs.github.com/en/rest",
        docs_text="...",
        extraction=DiscoveryExtraction(
            has_public_api=has_public_api,
            base_url="https://api.github.com",
            developer_portal_url="https://github.com/settings/developers",
            rate_limit_notes="5000 requests per hour",
            pagination_style_hint="link_header",
            validation_endpoint="GET /user",
        ),
        source_tier=source_tier,
        source_tier_reason=source_tier_reason,
    )


def _classified() -> ClassifyResult:
    return ClassifyResult(
        auth_scheme=AuthScheme.OAUTH2_AUTH_CODE, redirect_uris_required=True,
        scopes_available=["repo", "user"], confidence=0.9,
    )


def _gate_auto() -> GateResult:
    return GateResult(
        status=Status.AUTO,
        reason_code=ReasonCode.ELIGIBLE_AUTO,
        evidence=[EvidenceItem(claim="ToS reviewed", source_url="https://github.com/terms", snippet="...")],
        completeness_gaps=[CompletenessGap(field="rate_limit_notes", reason="not stated in prose")],
    )


def _gate_hitl(reason_code: ReasonCode = ReasonCode.REQUIRES_PAYMENT) -> GateResult:
    return GateResult(status=Status.HITL, reason_code=reason_code, evidence=[])


def _provisioned() -> ProvisionResult:
    return ProvisionResult(
        status="provisioned",
        account_email="credforge+github-com@example.com",
        account_password_ref="vault://github.com/account_password",
        client_id="mock-client-abc",
        client_secret_ref="vault://github.com/client_secret",
        console_url="https://github.com/settings/developers/apps/mock",
        credential_type=CredentialType.OAUTH2_TOKEN_PAIR,
    )


def test_full_auto_state_produces_a_consistent_artifact() -> None:
    state = _base_state(
        resolve=_resolved(), discovery=_discovered(), classify=_classified(),
        gate=_gate_auto(), provision=_provisioned(),
        validation=ValidateResult(status="valid", http_status_code=200, checked_url="https://api.github.com/user"),
    )

    artifact = build_artifact(state)

    assert artifact.status == Status.AUTO
    assert artifact.reason_code == ReasonCode.ELIGIBLE_AUTO
    assert artifact.app.identity_key == "github.com"
    assert artifact.api.base_url == "https://api.github.com"
    assert artifact.api.auth_scheme == AuthScheme.OAUTH2_AUTH_CODE
    assert artifact.credential.account_email == "credforge+github-com@example.com"
    assert artifact.credential.account_password_ref == "vault://github.com/account_password"
    assert artifact.credential.client_id == "mock-client-abc"
    assert artifact.credential.client_secret_ref == "vault://github.com/client_secret"
    assert artifact.validation.status == "valid"
    # resolve's own evidence plus GATE's ToS evidence, both carried forward
    assert len(artifact.evidence) == 2
    assert len(artifact.completeness_gaps) == 1
    assert artifact.provenance.run_id == "run_1"


def test_hitl_state_never_carries_a_credential_even_if_state_has_one_somehow() -> None:
    # Failure drill: a HandoffArtifact must never be constructible with a
    # credential attached to a non-AUTO status -- this is a real invariant,
    # not just an emit.py convention, so it's tested at the model level.
    with pytest.raises(ValidationError):
        HandoffArtifact(
            status=Status.HITL,
            reason_code=ReasonCode.REQUIRES_PAYMENT,
            app={"app_name": "X", "identity_key": "x.com"},
            credential={
                "credential_type": "api_key",
                "api_key_ref": "vault://x.com/api_key",
            },
            provenance={
                "run_id": "r1", "resolved_at": datetime.now(timezone.utc),
                "emitted_at": datetime.now(timezone.utc), "credforge_version": "0.1.0",
            },
        )


def test_emit_never_reaches_provision_because_gate_never_populated_a_credential() -> None:
    # The realistic path: build_artifact() itself simply never constructs a
    # CredentialInfo unless provision.status == "provisioned", so a HITL
    # app naturally emits with credential=None -- no invariant violation to
    # even reach.
    state = _base_state(resolve=_resolved(), discovery=_discovered(), classify=_classified(), gate=_gate_hitl())

    artifact = build_artifact(state)

    assert artifact.status == Status.HITL
    assert artifact.credential is None
    assert artifact.validation is None


def test_auto_status_with_wrong_reason_code_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        HandoffArtifact(
            status=Status.AUTO,
            reason_code=ReasonCode.REQUIRES_PAYMENT,  # inconsistent with AUTO
            app={"app_name": "X", "identity_key": "x.com"},
            provenance={
                "run_id": "r1", "resolved_at": datetime.now(timezone.utc),
                "emitted_at": datetime.now(timezone.utc), "credforge_version": "0.1.0",
            },
        )


def test_artifact_is_frozen_after_construction() -> None:
    state = _base_state(resolve=_resolved(), discovery=_discovered(), classify=_classified(), gate=_gate_auto())
    artifact = build_artifact(state)

    with pytest.raises(ValidationError):
        artifact.status = Status.HITL


def test_emit_before_gate_or_a_failed_resolve_raises_a_clear_error() -> None:
    # Failure drill: EMIT cannot fabricate a status/reason_code out of
    # nothing -- calling it before GATE (and without even a failed RESOLVE
    # to fall back on) must fail loudly, never produce a garbage artifact.
    state = _base_state()  # resolve is None -- nothing has run at all

    with pytest.raises(EmitError):
        build_artifact(state)


def test_failed_resolve_still_produces_a_hitl_artifact() -> None:
    # D-039: the same class of bug D-038 fixed for DISCOVERY_FAILED, one
    # stage earlier -- a RESOLVE that never resolved is a terminal
    # outcome, not a reason for EMIT to have nothing to say.
    failed_resolve = ResolveResult(
        resolved=False,
        reason_code=ReasonCode.RESOLVE_AMBIGUOUS,
        alternates=[
            ResolveCandidate(
                domain="example.com", confidence=0.5,
                evidence_url="https://example.com/", evidence_snippet="Example Inc.",
            ),
            ResolveCandidate(
                domain="example.org", confidence=0.45,
                evidence_url="https://example.org/", evidence_snippet="Also Example.",
            ),
        ],
    )
    state = _base_state(identity_key="unresolved:example", resolve=failed_resolve)

    artifact = build_artifact(state)

    assert artifact.status == Status.HITL
    assert artifact.reason_code == ReasonCode.RESOLVE_AMBIGUOUS
    assert artifact.api is None
    assert artifact.credential is None
    assert artifact.validation is None
    assert len(artifact.evidence) == 2
    assert {e.source_url for e in artifact.evidence} == {"https://example.com/", "https://example.org/"}


def test_dry_run_state_with_no_provision_or_validation_emits_a_partial_but_valid_artifact() -> None:
    # An AUTO app that hasn't actually been provisioned yet (research-only
    # / --dry-run) is a legitimate artifact shape, not an error.
    state = _base_state(resolve=_resolved(), discovery=_discovered(), classify=_classified(), gate=_gate_auto())

    artifact = build_artifact(state)

    assert artifact.status == Status.AUTO
    assert artifact.credential is None
    assert artifact.validation is None


def test_unsupported_app_emits_with_no_credential_but_keeps_api_info_as_evidence() -> None:
    # UNSUPPORTED still means DISCOVER found and crawled a real page --
    # just one without a public API -- so api.docs_url is legitimately
    # kept (it's evidence of where credforge looked), while credential
    # stays None since PROVISION never runs for an UNSUPPORTED app.
    state = _base_state(
        resolve=_resolved(),
        discovery=_discovered(has_public_api=False),
        gate=GateResult(status=Status.UNSUPPORTED, reason_code=ReasonCode.NO_PUBLIC_API, evidence=[]),
    )

    artifact = build_artifact(state)

    assert artifact.status == Status.UNSUPPORTED
    assert artifact.api.docs_url == "https://docs.github.com/en/rest"
    assert artifact.credential is None


def test_source_tier_and_docs_url_selection_reason_reach_the_artifact() -> None:
    # D-049/D-054: docs_url_reason was dropped entirely by EMIT, and
    # source_tier didn't exist as a field at all -- both must now appear.
    # D-054 also moved *which* reason EMIT reads: DISCOVER's
    # source_tier_reason (the URL actually used), not RESOLVE's
    # chosen.docs_url_reason (which can describe a different, or a stale
    # pre-redirect, URL) -- deliberately given a *different* reason string
    # here so the test would fail if EMIT ever regressed to reading the
    # wrong one.
    resolved = ResolveResult(
        resolved=True,
        chosen=ResolveCandidate(
            domain="github.com",
            docs_url="https://docs.github.com/en/rest",
            docs_url_reason="stale RESOLVE-time reason -- must NOT appear in the artifact",
            confidence=0.98,
            evidence_url="https://github.com/",
            evidence_snippet="GitHub is where you belong.",
        ),
    )
    state = _base_state(
        resolve=resolved,
        discovery=_discovered(
            source_tier=SourceTier.HIGH,
            source_tier_reason="HIGH-tier (path segment 'rest'); matched content markers: foo, bar",
        ),
        classify=ClassifyResult(
            auth_scheme=AuthScheme.OAUTH2_AUTH_CODE, confidence=0.9, source_tier=SourceTier.HIGH,
        ),
        gate=_gate_auto(),
    )

    artifact = build_artifact(state)

    assert artifact.api.source_tier == SourceTier.HIGH
    # resolve's identity evidence + the new docs-source evidence + GATE's ToS evidence
    assert len(artifact.evidence) == 3
    docs_evidence = [e for e in artifact.evidence if "docs source selected" in e.claim]
    assert len(docs_evidence) == 1
    assert "HIGH-tier" in docs_evidence[0].claim
    assert docs_evidence[0].source_url == "https://docs.github.com/en/rest"
    assert "stale RESOLVE-time reason" not in docs_evidence[0].claim
    # The snippet is a real page quote, distinct from the claim text --
    # not the reason string repeated (the bug this replaced).
    assert docs_evidence[0].snippet == "..."
    assert docs_evidence[0].snippet != docs_evidence[0].claim


@pytest.mark.parametrize(
    "true_tier,stale_resolve_reason",
    [
        (SourceTier.HIGH, "LOW-tier (no official-reference or docs signal in URL shape)"),
        (SourceTier.LOW, "HIGH-tier (path segment 'rest')"),
        (SourceTier.MEDIUM, "HIGH-tier ('developer' subdomain)"),
    ],
)
def test_evidence_tier_never_contradicts_the_api_block_tier(
    true_tier: SourceTier, stale_resolve_reason: str
) -> None:
    # The exact bug a live Linear run surfaced: evidence said "HIGH-tier
    # ('developers' subdomain)" while api.source_tier said "low" for the
    # same run -- two contradictory claims about the same fact, not a
    # silent failure. RESOLVE's own chosen.docs_url_reason is set here to
    # a DELIBERATELY WRONG tier (simulating either D-028's fallback -- a
    # different URL entirely -- or D-054's original pre-redirect-vs-
    # final-URL bug) to prove EMIT no longer reads it for this claim at
    # all, regardless of what it says.
    resolved = ResolveResult(
        resolved=True,
        chosen=ResolveCandidate(
            domain="github.com",
            docs_url="https://docs.github.com/en/rest",
            docs_url_reason=stale_resolve_reason,
            confidence=0.98,
            evidence_url="https://github.com/",
            evidence_snippet="GitHub is where you belong.",
        ),
    )
    state = _base_state(
        resolve=resolved,
        discovery=_discovered(
            source_tier=true_tier,
            source_tier_reason=f"{true_tier.value.upper()}-tier (the real, current tier)",
        ),
        classify=ClassifyResult(auth_scheme=AuthScheme.OAUTH2_AUTH_CODE, confidence=0.9, source_tier=true_tier),
        gate=_gate_auto(),
    )

    artifact = build_artifact(state)

    docs_evidence = next(e for e in artifact.evidence if "docs source selected" in e.claim)
    # The tier named in the evidence claim and the tier in api.source_tier
    # must be the exact same value -- never independently derived, never
    # able to disagree.
    assert true_tier.value.upper() in docs_evidence.claim
    assert artifact.api.source_tier == true_tier
    assert stale_resolve_reason not in docs_evidence.claim
    for other_tier in SourceTier:
        if other_tier != true_tier:
            assert other_tier.value.upper() not in docs_evidence.claim


def test_none_auth_scheme_emits_an_explicit_no_credential_required_credential_block() -> None:
    # Open-Meteo's case: a genuinely open API needs no credential at all.
    # The artifact must say so explicitly (credential_type=NONE, every
    # ref field None) rather than have `credential: null` look identical
    # to "PROVISION never ran" -- the two are different claims.
    state = _base_state(
        resolve=_resolved(),
        discovery=_discovered(),
        classify=ClassifyResult(auth_scheme=AuthScheme.NONE, confidence=1.0),
        gate=_gate_auto(),
        provision=ProvisionResult(status="provisioned", credential_type=CredentialType.NONE),
    )

    artifact = build_artifact(state)

    assert artifact.status == Status.AUTO
    assert artifact.credential is not None
    assert artifact.credential.credential_type == CredentialType.NONE
    assert artifact.credential.api_key_ref is None
    assert artifact.credential.client_id is None
    assert artifact.credential.client_secret_ref is None
    assert artifact.credential.bearer_token_ref is None
    assert artifact.credential.account_email is None
    assert artifact.credential.account_password_ref is None
