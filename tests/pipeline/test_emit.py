from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from credforge.enums import AuthScheme, CredentialType, ReasonCode, Status
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


def _discovered(has_public_api: bool = True) -> DiscoveryResult:
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
