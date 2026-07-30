from datetime import datetime, timezone
from pathlib import Path

import pytest

from credforge.config import Settings
from credforge.enums import PipelineStage, StageStatus
from credforge.pipeline.orchestrator import run_app
from credforge.providers.factory import ProviderBundle
from credforge.providers.fetch import FetchResult
from credforge.providers.llm import ClassifyExtraction, DiscoveryExtraction, TosGateExtraction
from credforge.providers.mock_browser import MockBrowserDriver
from credforge.providers.mock_email import MockEmailProvider
from credforge.providers.search import SearchResult
from credforge.registry.store import AppendOnlyRegistry
from credforge.vault.crypto_vault import FernetVault, generate_key


class FakeSearch:
    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results

    async def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        return self._results


class FakeFetch:
    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self._responses = responses or {}  # url -> text

    async def fetch(self, url: str, *, method: str = "GET", headers=None) -> FetchResult:
        if url in self._responses:
            return _ok(self._responses[url], url=url)
        from credforge.providers.fetch import FetchError, FetchException

        raise FetchException(FetchError(url=url, reason="connection_error"))


class FakeExtractor:
    async def extract_discovery(self, *, docs_text: str, docs_url: str) -> DiscoveryExtraction:
        return DiscoveryExtraction(has_public_api=True, base_url="https://api.example.com", validation_endpoint="GET /me")

    async def extract_classification(self, **kwargs) -> ClassifyExtraction:
        return ClassifyExtraction(auth_scheme="api_key", redirect_uris_required=False, scopes_available=[], confidence=0.95)

    async def extract_tos_gate_signals(self, *, tos_text: str, tos_url: str) -> TosGateExtraction:
        return TosGateExtraction(
            prohibits_automation=False, requires_payment=False, requires_business_verification=False,
            requires_sales_contact=False, requires_phone_verification=False, requires_captcha=False,
            requires_sso_only=False,
        )


def _ok(text: str, *, url: str = "x", content_type: str = "text/html") -> FetchResult:
    return FetchResult(
        url=url, final_url=url, status_code=200, content_type=content_type,
        text=text, fetched_at=datetime.now(timezone.utc),
    )


LONG_TEXT = "This page documents our REST API endpoints and authentication. " * 10


def _providers(*, resolvable: bool = True, domain: str = "example.com") -> ProviderBundle:
    results = (
        [SearchResult(title="Example", url=f"https://{domain}/", snippet="Example Inc.", rank=1)]
        if resolvable
        else []
    )
    fetch = FakeFetch(
        {
            f"https://{domain}/docs": LONG_TEXT,
            f"https://{domain}/terms": LONG_TEXT,
        }
    )
    return ProviderBundle(
        search=FakeSearch(results), fetch=fetch, extractor=FakeExtractor(),
        email=MockEmailProvider(), browser=MockBrowserDriver(),
    )


@pytest.mark.asyncio
async def test_resolve_failure_still_produces_a_hitl_artifact_not_a_silent_no_op(tmp_path: Path) -> None:
    # D-039: a RESOLVE that never produced a confident single answer is a
    # terminal outcome, not a stage that "didn't finish" -- no app should
    # be absent from batch output.
    providers = _providers(resolvable=False)
    registry = AppendOnlyRegistry(tmp_path / "registry.jsonl")

    state, artifact = await run_app(
        "NoSuchApp", providers=providers, settings=Settings(), registry=registry,
        run_id="run_1", data_dir=tmp_path, dry_run=True,
    )

    assert artifact is not None
    assert state.gate is None  # GATE genuinely never ran -- there's no domain to check
    assert artifact.status.value == "HITL"
    assert artifact.reason_code == state.resolve.reason_code
    assert artifact.credential is None
    assert artifact.api is None

    artifacts_dir = tmp_path / "runs" / "run_1" / "artifacts"
    assert list(artifacts_dir.glob("*.json"))
    entries = registry.load_all()
    assert any(e.stage == PipelineStage.EMIT and e.stage_status == StageStatus.COMPLETED for e in entries)


@pytest.mark.asyncio
async def test_discovery_failed_still_produces_a_hitl_artifact_not_a_silent_no_op(tmp_path: Path) -> None:
    # The real bug this fixes: DISCOVERY_FAILED used to short-circuit
    # before GATE entirely, even though gate() already has a correct,
    # tested precondition for exactly this case. That made the app
    # invisible to REPORT -- no artifact, no registry EMIT entry, nothing.
    providers = _providers()
    providers = ProviderBundle(
        search=providers.search, fetch=FakeFetch({}), extractor=providers.extractor,  # every fetch fails
        email=providers.email, browser=providers.browser,
    )
    registry = AppendOnlyRegistry(tmp_path / "registry.jsonl")

    state, artifact = await run_app(
        "Example", providers=providers, settings=Settings(), registry=registry,
        run_id="run_1", data_dir=tmp_path, dry_run=True,
    )

    assert artifact is not None
    assert artifact.status.value == "HITL"
    assert artifact.reason_code.value == "discovery_failed"
    entries = registry.load_all()
    assert any(e.stage == PipelineStage.EMIT and e.stage_status == StageStatus.COMPLETED for e in entries)


@pytest.mark.asyncio
async def test_dry_run_never_provisions_even_for_an_auto_app(tmp_path: Path) -> None:
    providers = _providers()
    registry = AppendOnlyRegistry(tmp_path / "registry.jsonl")

    state, artifact = await run_app(
        "Example", providers=providers, settings=Settings(), registry=registry,
        run_id="run_1", data_dir=tmp_path, vault=None, dry_run=True,
    )

    assert artifact is not None
    assert artifact.status.value == "AUTO"
    assert artifact.credential is None
    assert state.provision is None
    assert state.validation is None

    artifacts_dir = tmp_path / "runs" / "run_1" / "artifacts"
    assert list(artifacts_dir.glob("*.json"))

    entries = registry.load_all()
    assert any(e.stage == PipelineStage.EMIT and e.stage_status == StageStatus.COMPLETED for e in entries)


@pytest.mark.asyncio
async def test_non_dry_run_auto_app_provisions_and_validates(tmp_path: Path) -> None:
    providers = _providers()
    registry = AppendOnlyRegistry(tmp_path / "registry.jsonl")
    vault = FernetVault(key=generate_key(), path=tmp_path / "vault.json")

    state, artifact = await run_app(
        "Example", providers=providers, settings=Settings(), registry=registry,
        run_id="run_1", data_dir=tmp_path, vault=vault, dry_run=False,
    )

    assert artifact is not None
    assert artifact.credential is not None
    assert artifact.credential.client_secret_ref == "vault://example.com/client_secret"
    assert artifact.credential.account_password_ref == "vault://example.com/account_password"
    assert state.provision is not None
    assert state.provision.status == "provisioned"
    # a real (fake) fetch was attempted for VALIDATE's validation_endpoint
    assert state.validation is not None


@pytest.mark.asyncio
async def test_hitl_app_never_reaches_provision(tmp_path: Path) -> None:
    class NoApiExtractor(FakeExtractor):
        async def extract_discovery(self, *, docs_text: str, docs_url: str) -> DiscoveryExtraction:
            return DiscoveryExtraction(has_public_api=False)

    providers = _providers()
    providers = ProviderBundle(
        search=providers.search, fetch=providers.fetch, extractor=NoApiExtractor(),
        email=providers.email, browser=providers.browser,
    )
    registry = AppendOnlyRegistry(tmp_path / "registry.jsonl")

    state, artifact = await run_app(
        "Example", providers=providers, settings=Settings(), registry=registry,
        run_id="run_1", data_dir=tmp_path, dry_run=False,
    )

    assert artifact is not None
    assert artifact.status.value == "UNSUPPORTED"
    assert state.provision is None


@pytest.mark.asyncio
async def test_a_batch_of_n_apps_always_produces_n_artifacts(tmp_path: Path) -> None:
    # The property the user asked to have tested directly: no app is ever
    # absent from batch output, regardless of which stage it fails at.
    unresolvable_providers = _providers(resolvable=False)
    registry = AppendOnlyRegistry(tmp_path / "registry.jsonl")

    app_plan = [
        ("Example1", _providers(domain="example1.com")),   # clears all the way to AUTO (dry_run)
        ("NoSuchApp1", unresolvable_providers),             # fails at RESOLVE
        ("Example2", _providers(domain="example2.com")),
        ("NoSuchApp2", unresolvable_providers),
        ("Example3", _providers(domain="example3.com")),
    ]

    for app_name, providers in app_plan:
        state, artifact = await run_app(
            app_name, providers=providers, settings=Settings(), registry=registry,
            run_id="run_batch", data_dir=tmp_path, dry_run=True,
        )
        assert artifact is not None, f"{app_name} produced no artifact"

    artifacts_dir = tmp_path / "runs" / "run_batch" / "artifacts"
    # 5 distinct identity_keys (3 real resolved domains, 2 synthetic
    # "unresolved:<slug>" keys for the apps that never resolved) -> 5
    # distinct artifact files on disk, not just 5 in-memory results.
    assert len(list(artifacts_dir.glob("*.json"))) == 5
    entries = registry.load_all()
    assert sum(1 for e in entries if e.stage == PipelineStage.EMIT and e.stage_status == StageStatus.COMPLETED) == 5
