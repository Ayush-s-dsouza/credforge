"""Orchestrator: wires RESOLVE -> DISCOVER -> CLASSIFY -> GATE -> (PROVISION
-> VALIDATE, only if AUTO and not --dry-run) -> EMIT together for one app,
with real providers and the vault/registry Stage 5 needs. This is the one
place `run`/`batch` (cli/__init__.py) call into -- neither CLI command
re-implements pipeline sequencing. See DECISIONS.md D-036.

`dry_run=True` stops after GATE unconditionally, even for an AUTO app --
research-only, never touches the vault or a browser/email provider. This
is what the seed-batch coverage run uses so that measuring the AUTO/HITL/
UNSUPPORTED distribution across many apps doesn't also mock-provision an
account for every AUTO one.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..enums import PipelineStage, ReasonCode, Status, StageStatus
from ..models.artifact import HandoffArtifact
from ..models.registry_entities import RegistryEntry
from ..models.state import AppPipelineState, ClassifyResult, DiscoveryResult, ResolveResult
from ..providers.factory import ProviderBundle
from ..providers.fetch import FetchProvider
from ..providers.llm import Extractor
from ..registry.identity import slugify, unresolved_identity_key
from ..registry.store import AppendOnlyRegistry
from ..vault.crypto_vault import FernetVault
from .classify import classify
from .discover import discover
from .emit import build_artifact
from .explain import NULL_EXPLAIN, ExplainEvent, ExplainSink
from .gate import gate
from .provision import provision
from .resolve import resolve
from .validate import validate


async def _resolve_ambiguity_via_content(
    resolve_result: ResolveResult,
    *,
    identity_key: str,
    fetch: FetchProvider,
    extractor: Extractor,
    explain: ExplainSink,
) -> tuple[ResolveResult, DiscoveryResult | None]:
    """D-082: RESOLVE_AMBIGUOUS used to be terminal -- a ranking heuristic
    (the confidence-score gap between the top two candidates) stopping the
    whole run before DISCOVER, a stage that can actually read a page's
    content, ever got a chance to look. Tries each ambiguous candidate's
    domain through DISCOVER, completely unchanged, in the exact order
    RESOLVE ranked them (`alternates` is already that full ranked list --
    see resolve.py) -- the first one whose docs page verifies as a real
    public API wins, exactly the same "try in order, first real success
    wins" pattern DISCOVER already uses internally for docs-URL
    candidates (D-028), just applied one level up, across domains.

    `docs_url_candidates=[]` for every trial: RESOLVE deliberately never
    ranks/verifies docs URLs for an ambiguous candidate ("stop there
    without even attempting docs-URL discovery on a candidate we're not
    going to trust anyway" -- resolve.py's own docstring), so DISCOVER's
    own conventional subdomain/path guesses (`_candidate_list`) are all
    there is to try here, same as a bare, unverified domain always gets.

    Returns the original, still-ambiguous ResolveResult unchanged (and
    None) if no candidate's content ever panned out -- the caller's
    existing `if not state.resolve.resolved` terminal-stop handles that
    case exactly as it always has, no separate failure path needed here.
    """
    for candidate in resolve_result.alternates:
        explain.emit(
            ExplainEvent(
                stage=PipelineStage.RESOLVE,
                identity_key=candidate.domain,
                message=f"ambiguous resolve: trying {candidate.domain} through DISCOVER -- content, not score, decides",
            )
        )
        trial_discovery = await discover(
            candidate.domain, docs_url_candidates=[], fetch=fetch, extractor=extractor, explain=explain,
        )
        if trial_discovery.extraction is not None and trial_discovery.extraction.has_public_api:
            explain.emit(
                ExplainEvent(
                    stage=PipelineStage.RESOLVE,
                    identity_key=candidate.domain,
                    message=(
                        f"resolved {candidate.domain} via content, not RESOLVE's score -- RESOLVE was "
                        f"ambiguous ({', '.join(f'{c.domain} ({c.confidence})' for c in resolve_result.alternates)}); "
                        f"{candidate.domain} was the first candidate whose docs page verified as a real public API"
                    ),
                )
            )
            resolved = resolve_result.model_copy(
                update={"resolved": True, "reason_code": None, "chosen": candidate, "resolved_via_content_fallback": True}
            )
            return resolved, trial_discovery

    explain.emit(
        ExplainEvent(
            stage=PipelineStage.RESOLVE,
            identity_key=identity_key,
            message=(
                f"none of {len(resolve_result.alternates)} ambiguous candidate(s)' docs pages verified as "
                "a real public API -- still ambiguous, not guessing"
            ),
        )
    )
    return resolve_result, None


def settings_fingerprint(settings: Settings, *, live: bool) -> str:
    """A coarse fingerprint over the settings that affect PROVISION's
    decision -- not a full per-stage fingerprint (the plan's original
    design called for one per stage; scoped down here to what actually
    matters for the one stage that checks it, given the Stage 9 time
    budget). See DECISIONS.md D-036."""
    raw = f"live={live}|threshold={settings.resolve_confidence_threshold}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def run_app(
    app_name: str,
    *,
    providers: ProviderBundle,
    settings: Settings,
    registry: AppendOnlyRegistry,
    run_id: str,
    data_dir: Path,
    vault: FernetVault | None = None,
    dry_run: bool = False,
    live: bool = False,
    explain: ExplainSink = NULL_EXPLAIN,
) -> tuple[AppPipelineState, HandoffArtifact | None]:
    state = AppPipelineState(
        app_name=app_name,
        input_key=slugify(app_name),
        identity_key=unresolved_identity_key(app_name),
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
    )

    state.resolve = await resolve(
        app_name,
        search=providers.search,
        fetch=providers.fetch,
        confidence_threshold=settings.resolve_confidence_threshold,
        explain=explain,
    )

    # D-082: RESOLVE_AMBIGUOUS alone doesn't stop the run anymore -- give
    # DISCOVER a chance to settle it by content before falling back to the
    # terminal HITL path below. resolve_not_found/malformed_input/
    # low_confidence are untouched -- there's nothing to try for those
    # (no candidate list, or a single already-considered one).
    if not state.resolve.resolved and state.resolve.reason_code == ReasonCode.RESOLVE_AMBIGUOUS:
        state.resolve, state.discovery = await _resolve_ambiguity_via_content(
            state.resolve,
            identity_key=state.identity_key,
            fetch=providers.fetch,
            extractor=providers.extractor,
            explain=explain,
        )

    if not state.resolve.resolved:
        # A RESOLVE that never produced a confident single answer is a
        # terminal outcome, not a stage that "didn't finish" -- it gets a
        # real artifact (HITL, RESOLVE's own reason_code, alternates as
        # evidence) via EMIT's failed-RESOLVE path, same as
        # DISCOVERY_FAILED already does one stage later. See DECISIONS.md
        # D-039; this is the same class of bug D-038 fixed for DISCOVER.
        artifact = build_artifact(state)
        _persist_artifact(artifact, data_dir=data_dir)
        _record_emit_completed(registry, state=state, app_name=app_name, settings=settings, live=live)
        return state, artifact
    state.identity_key = state.resolve.chosen.domain

    if state.discovery is None:
        # Already set above when the content-fallback found its winner --
        # that trial run IS this app's real DISCOVER result, not just a
        # probe to be thrown away and redone.
        state.discovery = await discover(
            state.identity_key,
            docs_url_candidates=state.resolve.chosen.docs_url_candidates,
            fetch=providers.fetch,
            extractor=providers.extractor,
            explain=explain,
        )

    if state.discovery.reason_code == ReasonCode.DISCOVERY_FAILED:
        # DISCOVERY_FAILED still flows into GATE, not around it -- gate()
        # has its own DISCOVERY_FAILED precondition (returns HITL, zero
        # fetch calls, before ever touching `classify`) specifically so
        # this case gets a real artifact instead of silently having no
        # EMIT/report entry at all. See DECISIONS.md D-038. The
        # ClassifyResult below is a placeholder never actually inspected --
        # gate()'s precondition check returns before reaching it.
        state.classify = ClassifyResult(auth_scheme=None, confidence=0.0)
    else:
        state.classify = await classify(
            state.identity_key,
            docs_text=state.discovery.docs_text,
            docs_url=state.discovery.docs_url,
            discovery=state.discovery.extraction,
            source_tier=state.discovery.source_tier,
            extractor=providers.extractor,
            explain=explain,
        )

    state.gate = await gate(
        state.identity_key,
        discovery=state.discovery,
        classify=state.classify,
        fetch=providers.fetch,
        extractor=providers.extractor,
        explain=explain,
    )

    if state.gate.status == Status.AUTO and not dry_run:
        assert vault is not None  # required by the CLI whenever dry_run is False
        extraction = state.discovery.extraction
        # D-064: state.resolve.chosen.docs_url is the vendor's *documentation*
        # URL, not necessarily its signup/console URL -- SignupRecipe's own
        # docstring already names this distinction (OpenWeatherMap's docs and
        # signup form live at different URLs), and falling back to docs_url
        # here silently sends the browser to the wrong page whenever
        # DISCOVER's LLM extraction doesn't happen to populate
        # developer_portal_url that run. A recipe-pinned fallback is tried
        # first, same lazy-import fallback pattern as validation_endpoint
        # (D-063) -- fallback only, never overrides a real DISCOVER
        # extraction.
        from ..providers.signup_recipes import LIVE_SIGNUP_RECIPES

        recipe = LIVE_SIGNUP_RECIPES.get(state.identity_key)
        developer_portal_url = (
            (extraction.developer_portal_url if extraction else None)
            or (recipe.developer_portal_url_fallback if recipe is not None else None)
            or state.resolve.chosen.docs_url
            or f"https://{state.identity_key}"
        )
        state.provision = await provision(
            state.identity_key,
            app_name=app_name,
            developer_portal_url=developer_portal_url,
            redirect_uris=["https://credforge.local/callback"],
            auth_scheme=state.classify.auth_scheme,
            email=providers.email,
            browser=providers.browser,
            vault=vault,
            registry=registry,
            run_id=run_id,
            settings_fingerprint=settings_fingerprint(settings, live=live),
            explain=explain,
        )
        if state.provision.status in ("provisioned", "already_provisioned"):
            # A credential that hasn't been test-called doesn't count --
            # VALIDATE always runs here, even for NONE auth (an empty
            # credential dict, zero auth headers), because "this API
            # needs no credential" is itself a claim worth confirming
            # against the real API, not just asserting.
            credential: dict[str, str] = {}
            if state.provision.api_key_ref:
                credential.update(vault.retrieve(state.provision.api_key_ref))
            if state.provision.client_secret_ref:
                credential.update(vault.retrieve(state.provision.client_secret_ref))
            if state.provision.bearer_token_ref:
                credential.update(vault.retrieve(state.provision.bearer_token_ref))
            if state.provision.client_id:
                credential["client_id"] = state.provision.client_id

            validation_endpoint = extraction.validation_endpoint if extraction else None
            if validation_endpoint is None:
                # D-063: fallback only -- never overrides a real DISCOVER
                # extraction, and does not touch GATE's verdict at all.
                # `recipe` was already looked up above for
                # developer_portal_url_fallback (D-064) -- same recipe,
                # same identity_key, no need to look it up twice.
                if recipe is not None and recipe.validation_endpoint_fallback is not None:
                    validation_endpoint = recipe.validation_endpoint_fallback

            state.validation = await validate(
                state.identity_key,
                validation_endpoint=validation_endpoint,
                base_url=extraction.base_url if extraction else None,
                auth_scheme=state.classify.auth_scheme.value if state.classify.auth_scheme else "none",
                credential=credential,
                fetch=providers.fetch,
            )

    artifact = build_artifact(state)
    _persist_artifact(artifact, data_dir=data_dir)
    _record_emit_completed(registry, state=state, app_name=app_name, settings=settings, live=live)
    return state, artifact


def _persist_artifact(artifact: HandoffArtifact, *, data_dir: Path) -> Path:
    artifacts_dir = data_dir / "runs" / artifact.provenance.run_id / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    safe_name = artifact.app.identity_key.replace(".", "-").replace(":", "-").replace("/", "-")
    out_path = artifacts_dir / f"{safe_name}.json"
    out_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    return out_path


def _record_emit_completed(
    registry: AppendOnlyRegistry, *, state: AppPipelineState, app_name: str, settings: Settings, live: bool
) -> None:
    registry.append(
        RegistryEntry(
            identity_key=state.identity_key,
            app_name=app_name,
            run_id=state.run_id,
            stage=PipelineStage.EMIT,
            stage_status=StageStatus.COMPLETED,
            settings_fingerprint=settings_fingerprint(settings, live=live),
            recorded_at=datetime.now(timezone.utc),
        )
    )
