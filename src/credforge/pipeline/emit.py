"""EMIT: the one conversion point from mutable AppPipelineState to the
frozen HandoffArtifact. See DECISIONS.md D-002 (why these are two
separate model families) and D-033 (this stage's own choices).

Deliberately synchronous, unlike every stage before it -- this is pure
transformation over data every earlier stage already computed, with zero
I/O of its own. A caller reading `def build_artifact(...)` with no
`async` immediately knows this stage does no network/disk work, the same
way PROVISION's wider signature (D-031) tells a reader it's the one stage
that does.

Two paths produce a real artifact, not one: the normal GATE-derived path,
and a RESOLVE-failure path (D-039) -- a RESOLVE that never produced a
confident single answer is a terminal outcome too (a human needs to
either supply the right domain or confirm the app has no automatable
signup), not an absence of one. Every app that's run through the
orchestrator gets *some* artifact; none are silently dropped.
"""

from datetime import datetime, timezone

from .. import __version__
from ..enums import Status
from ..models.artifact import ApiInfo, AppInfo, CredentialInfo, HandoffArtifact, Provenance, TosInfo, ValidationInfo
from ..models.state import AppPipelineState, EvidenceItem


class EmitError(Exception):
    """Raised when build_artifact() is called on a state with neither a
    GATE result nor a definitively-failed RESOLVE result -- EMIT cannot
    fabricate a status/reason_code out of nothing."""


def build_artifact(state: AppPipelineState, *, credforge_version: str = __version__) -> HandoffArtifact:
    if state.gate is not None:
        return _build_from_gate(state, credforge_version=credforge_version)
    if state.resolve is not None and not state.resolve.resolved:
        return _build_from_failed_resolve(state, credforge_version=credforge_version)
    raise EmitError(
        f"cannot emit an artifact for {state.identity_key!r} -- "
        "neither a GATE result nor a definitively-failed RESOLVE result is available yet"
    )


def _build_from_failed_resolve(state: AppPipelineState, *, credforge_version: str) -> HandoffArtifact:
    assert state.resolve is not None and state.resolve.reason_code is not None  # guaranteed by build_artifact's check

    evidence = [
        EvidenceItem(
            claim=f"candidate considered: {alt.domain} (confidence {alt.confidence:.2f})",
            source_url=alt.evidence_url,
            snippet=alt.evidence_snippet,
        )
        for alt in state.resolve.alternates
    ]

    return HandoffArtifact(
        status=Status.HITL,
        reason_code=state.resolve.reason_code,
        app=AppInfo(app_name=state.app_name, identity_key=state.identity_key),
        evidence=evidence,
        provenance=Provenance(
            run_id=state.run_id,
            resolved_at=state.started_at,
            emitted_at=datetime.now(timezone.utc),
            credforge_version=credforge_version,
        ),
    )


def _build_from_gate(state: AppPipelineState, *, credforge_version: str) -> HandoffArtifact:
    assert state.gate is not None  # guaranteed by build_artifact's check
    app = AppInfo(app_name=state.app_name, identity_key=state.identity_key)

    # The docs_url DISCOVER actually used, not RESOLVE's original top pick --
    # they can differ (D-028's fallback-through-candidates), and that's the
    # page CLASSIFY's source_tier is actually computed from.
    docs_url = state.discovery.docs_url if state.discovery and state.discovery.docs_url else (
        state.resolve.chosen.docs_url if state.resolve and state.resolve.chosen else None
    )
    extraction = state.discovery.extraction if state.discovery else None
    api: ApiInfo | None = None
    if docs_url or extraction is not None:
        api = ApiInfo(
            docs_url=docs_url,
            base_url=extraction.base_url if extraction else None,
            developer_portal_url=extraction.developer_portal_url if extraction else None,
            auth_scheme=state.classify.auth_scheme if state.classify else None,
            auth_required=state.classify.auth_required if state.classify else None,
            rate_limit_notes=extraction.rate_limit_notes if extraction else None,
            pagination_style_hint=extraction.pagination_style_hint if extraction else None,
            validation_endpoint=extraction.validation_endpoint if extraction else None,
            scopes_available=state.classify.scopes_available if state.classify else [],
            redirect_uris_required=state.classify.redirect_uris_required if state.classify else False,
            source_tier=state.classify.source_tier if state.classify else None,
            classify_confidence=state.classify.confidence if state.classify else None,
            api_style=state.discovery.api_style if state.discovery else None,
        )

    credential: CredentialInfo | None = None
    # "already_provisioned" (the idempotency-guard reuse path) still
    # carries a real, existing credential -- it must appear in the
    # artifact exactly like a freshly-provisioned one, not show up as
    # `credential: null` just because this particular run skipped the
    # signup flow.
    if state.provision is not None and state.provision.status in ("provisioned", "already_provisioned"):
        assert state.provision.credential_type is not None  # guaranteed by provision.py's success path
        credential = CredentialInfo(
            account_email=state.provision.account_email,
            account_password_ref=state.provision.account_password_ref,
            credential_type=state.provision.credential_type,
            api_key_ref=state.provision.api_key_ref,
            client_id=state.provision.client_id,
            client_secret_ref=state.provision.client_secret_ref,
            bearer_token_ref=state.provision.bearer_token_ref,
            console_url=state.provision.console_url,
            recipe_generated_by=state.provision.recipe_generated_by,
            recipe_generated_at=state.provision.recipe_generated_at,
        )

    validation_info: ValidationInfo | None = None
    if state.validation is not None:
        validation_info = ValidationInfo(
            status=state.validation.status,
            reason_code=state.validation.reason_code,
            http_status_code=state.validation.http_status_code,
            checked_url=state.validation.checked_url,
        )

    evidence: list[EvidenceItem] = []
    if state.resolve and state.resolve.chosen:
        evidence.append(
            EvidenceItem(
                claim=f"resolved identity to {state.resolve.chosen.domain}",
                source_url=state.resolve.chosen.evidence_url,
                snippet=state.resolve.chosen.evidence_snippet,
            )
        )
        # D-082: identity was decided by DISCOVER actually reading the
        # page, not by RESOLVE's own ranking -- RESOLVE was ambiguous.
        # Every candidate RESOLVE considered is recorded here too, same
        # as the terminal-ambiguous path already does, so a consumer of
        # this artifact can see what was ruled out and why the winner
        # wasn't simply "the top score."
        if state.resolve.resolved_via_content_fallback:
            evidence.append(
                EvidenceItem(
                    claim=(
                        f"{state.resolve.chosen.domain} was chosen by content, not score -- RESOLVE's own "
                        "ranking was ambiguous (top candidates too close); DISCOVER settled it by actually "
                        "reading each candidate's page until one verified as a real public API"
                    ),
                    source_url=state.resolve.chosen.evidence_url,
                    snippet=state.resolve.chosen.evidence_snippet,
                )
            )
            evidence.extend(
                EvidenceItem(
                    claim=f"ambiguous candidate considered: {alt.domain} (RESOLVE confidence {alt.confidence:.2f})",
                    source_url=alt.evidence_url,
                    snippet=alt.evidence_snippet,
                )
                for alt in state.resolve.alternates
            )
    # D-054: this evidence item now reads DISCOVER's source_tier_reason --
    # computed once, from the same docs_url that ends up in `api.docs_url`
    # and `api.source_tier` above -- instead of RESOLVE's
    # `chosen.docs_url_reason`, which describes RESOLVE's own top-ranked
    # candidate and can be a *different* URL (D-028's fallback-through-
    # candidates) or even, for the same URL, a stale pre-redirect
    # description (the real bug this replaced: a conventional subdomain
    # guess like "developers.example.com" redirecting to
    # "example.com/developers" before RESOLVE ever recorded its reason).
    # The snippet is a real quote from the crawled page, not the reason
    # text repeated -- the reason already IS the claim; the snippet's job
    # is independent corroboration, matching every other evidence item's
    # shape (see the "resolved identity to" item above).
    if state.discovery and state.discovery.docs_url and state.discovery.source_tier_reason:
        # D-066: noted directly in the claim, not a separate field -- a
        # browser-rendered page is real, legitimate evidence, but the
        # artifact should say so explicitly rather than leave it
        # indistinguishable from an ordinary plain-HTTP fetch.
        rendered_note = " (browser-rendered -- plain HTTP fetch found a JS app shell)" if state.discovery.docs_rendered_with_browser else ""
        evidence.append(
            EvidenceItem(
                claim=f"docs source selected: {state.discovery.source_tier_reason}{rendered_note}",
                source_url=state.discovery.docs_url,
                snippet=(state.discovery.docs_text or "")[:200].strip(),
            )
        )
    evidence.extend(state.gate.evidence)

    # D-067: None only when GATE never reached its ToS-checking logic at
    # all (a precondition -- DISCOVERY_FAILED/NO_PUBLIC_API/
    # CLASSIFY_LOW_CONFIDENCE -- returned first); every other outcome
    # (AUTO, a real block, or AUTO-with-unverifiable-ToS) always sets it.
    tos_info = (
        TosInfo(
            status=state.gate.tos_status,
            checked_url=state.gate.tos_checked_url,
            discovery_method=state.gate.tos_discovery_method,
        )
        if state.gate.tos_status is not None
        else None
    )

    return HandoffArtifact(
        status=state.gate.status,
        reason_code=state.gate.reason_code,
        app=app,
        api=api,
        credential=credential,
        validation=validation_info,
        tos=tos_info,
        evidence=evidence,
        completeness_gaps=state.gate.completeness_gaps,
        provenance=Provenance(
            run_id=state.run_id,
            resolved_at=state.started_at,
            emitted_at=datetime.now(timezone.utc),
            credforge_version=credforge_version,
        ),
    )
