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
from ..models.state import AppPipelineState, ClassifyResult
from ..providers.factory import ProviderBundle
from ..registry.identity import slugify, unresolved_identity_key
from ..registry.store import AppendOnlyRegistry
from ..vault.crypto_vault import FernetVault
from .classify import classify
from .discover import discover
from .emit import build_artifact
from .explain import NULL_EXPLAIN, ExplainSink
from .gate import gate
from .provision import provision
from .resolve import resolve
from .validate import validate


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
        developer_portal_url = (
            (extraction.developer_portal_url if extraction else None)
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

            state.validation = await validate(
                state.identity_key,
                validation_endpoint=extraction.validation_endpoint if extraction else None,
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
