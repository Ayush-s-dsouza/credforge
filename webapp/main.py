"""FastAPI wrapper around credforge's existing pipeline orchestrator.

No pipeline logic lives here -- this calls the exact same
`pipeline.orchestrator.run_app()` the CLI's `run` command calls, streaming
its ExplainSink events out over SSE instead of printing them to a
terminal. Mock provisioning by default; live provisioning (real Playwright
+ IMAP) only for the small, fixed set of vendors with a registered
SignupRecipe, chosen per-request and disclosed in the stream -- see
DECISIONS.md D-042/D-046/D-048.
"""

import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from credforge import __version__ as credforge_version  # noqa: E402
from credforge.config import Settings  # noqa: E402
from credforge.pipeline.explain import ExplainEvent  # noqa: E402
from credforge.pipeline.orchestrator import run_app  # noqa: E402
from credforge.providers.factory import build_providers  # noqa: E402
from credforge.providers.search import SearchProviderError  # noqa: E402
from credforge.redaction import scrub_secrets  # noqa: E402
from credforge.registry.store import AppendOnlyRegistry  # noqa: E402
from credforge.run_context import new_run_id  # noqa: E402
from credforge.vault.crypto_vault import FernetVault  # noqa: E402

from credforge.pipeline.resolve import _match_recipe_identity  # noqa: E402
from credforge.providers.signup_recipes import LIVE_SIGNUP_RECIPES  # noqa: E402

ACCESS_TOKEN = os.environ.get("WEB_ACCESS_TOKEN", "")
RUN_CAP = int(os.environ.get("RUN_CAP", "100"))
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "3"))
EXAMPLES_DIR = REPO_ROOT / "examples"
LIVE_RUN_CAP = int(os.environ.get("LIVE_RUN_CAP", "20"))

# Which ProviderBundle to hand to run_app must be decided *before*
# run_app's own RESOLVE stage runs and resolves the real domain -- so this
# reuses RESOLVE's own recipe-identity matcher (D-048) rather than
# duplicating a second, driftable copy of "which vendors have a recipe."
# Purely cosmetic display names for the one-click buttons -- no matching
# logic lives here, just a label for what _match_recipe_identity() already
# resolved by domain.
_RECIPE_DISPLAY_NAMES = {
    "nasa.gov": "NASA API",
    "openweathermap.org": "OpenWeatherMap",
    "alphavantage.co": "Alpha Vantage",
    "ipinfo.io": "IPinfo",
}

# What actually happens for each, as of the last real live verification --
# not aspirational copy. Alpha Vantage is the one of these four that goes
# all the way through: GATE reaches AUTO (D-057/D-058/D-059/D-060 fixed a
# real chain of false blocks -- scoped payment/sales-contact language, a
# missed ToS URL, an unreadable PDF ToS) and PROVISION+VALIDATE now
# succeed for real (D-061 fixed a stale extraction regex that had looked
# like vendor-side dedupe but wasn't -- confirmed false via a captured
# real network response before writing any dedupe-detection code). The
# other three are each blocked at a different, diagnosed point -- see
# OPS.md's recipe-ability section for the real evidence behind each.
_RECIPE_OUTCOME_NOTES = {
    # Not "and validated" -- VALIDATE only runs when DISCOVER's own,
    # per-run LLM extraction happens to populate validation_endpoint from
    # Alpha Vantage's docs text, which it doesn't do every run (real,
    # observed non-determinism, unrelated to anything fixed today). A
    # live run that acquires a credential but skips VALIDATE is a real,
    # correct outcome, not a bug -- the run log states which happened.
    "alphavantage.co": "reaches AUTO, credential acquired (validation depends on this run's extraction)",
    "nasa.gov": "JS-rendered docs, stops at discovery",
    "ipinfo.io": "CAPTCHA",
    "openweathermap.org": "CAPTCHA",
}


# Set at deploy time (`scripts/deploy_railway.sh`), not baked in by Railway
# itself -- this service has no GitHub connection (repoTriggers is empty,
# every deploy so far has been a manual `railway up`), so none of
# Railway's own git-derived variables (which only populate for a
# GitHub-triggered build) are ever set. Read fresh per request, not cached
# at import time, so a variable set without a redeploy (rare, but
# possible via `railway variable set`) is still reflected immediately.
_PROCESS_STARTED_AT = time.time()

settings = Settings()
registry = AppendOnlyRegistry(settings.data_dir / "registry.jsonl")
vault = (
    FernetVault(key=settings.vault_key.get_secret_value(), path=settings.data_dir / "vault" / "secrets.vault")
    if settings.vault_key
    else None
)

providers_mock = build_providers(settings, live=False)
_live_enabled = bool(settings.imap_host and settings.imap_user and settings.imap_password)
providers_live = build_providers(settings, live=True) if _live_enabled else None

COUNTER_PATH = settings.data_dir / "run_counter.json"
LIVE_COUNTER_PATH = settings.data_dir / "live_run_counter.json"
COUNTER_PATH.parent.mkdir(parents=True, exist_ok=True)
_counter_lock = asyncio.Lock()
_live_counter_lock = asyncio.Lock()


def _read_json_counter(path: Path) -> int:
    if path.exists():
        try:
            return int(json.loads(path.read_text(encoding="utf-8")).get("count", 0))
        except Exception:
            return 0
    return 0


def _write_json_counter(path: Path, n: int) -> None:
    path.write_text(json.dumps({"count": n}), encoding="utf-8")


# Per-IP rate limit -- in-memory, resets on restart, good enough for "a few
# runs per minute" abuse prevention on a demo deployment.
_ip_hits: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(ip: str) -> bool:
    now = time.monotonic()
    dq = _ip_hits[ip]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_PER_MIN:
        return False
    dq.append(now)
    return True


# App name only -- letters/numbers/spaces/./- , explicitly no "://" or
# scheme-looking input. The client can never hand this a fetch target.
_APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-]{0,63}$")


def _validate_app_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name or not _APP_NAME_RE.match(name) or "://" in name.lower() or name.lower().startswith("http"):
        raise HTTPException(400, "invalid app name -- letters/numbers/spaces/hyphens only, no URLs")
    return name


def _assert_no_raw_credential(artifact) -> None:
    """Defense in depth on top of two structural guarantees: CredentialInfo's
    schema only ever carries `*_ref` (vault refs) or `client_id` (public, not
    secret) -- never a raw value (D-041) -- and scrub_secrets() already ran
    on the serialized JSON. This asserts the schema guarantee actually holds
    for THIS artifact before it ever reaches a response body."""
    cred = artifact.credential
    if cred is None:
        return
    for field_name in ("api_key_ref", "client_secret_ref", "bearer_token_ref", "account_password_ref"):
        value = getattr(cred, field_name, None)
        if value is not None and not str(value).startswith("vault://"):
            raise RuntimeError(f"refusing to emit artifact: {field_name} is not a vault:// reference")


def _check_token(k: str | None) -> None:
    if ACCESS_TOKEN and k != ACCESS_TOKEN:
        raise HTTPException(403, "invalid or missing access token")


class SSEExplainSink:
    """Same ExplainSink protocol the CLI's ConsoleExplainSink implements --
    pushes onto an asyncio.Queue instead of printing, with elapsed_ms per
    event so the browser can render a ticking counter per stage."""

    def __init__(self, queue: asyncio.Queue, start: float) -> None:
        self.queue = queue
        self.start = start

    def emit(self, event: ExplainEvent) -> None:
        self.queue.put_nowait(
            {
                "type": "stage",
                "stage": event.stage.value,
                "identity_key": event.identity_key,
                "message": scrub_secrets(event.message),
                "elapsed_ms": int((time.monotonic() - self.start) * 1000),
            }
        )


app = FastAPI(title="credforge")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
def status() -> dict:
    return {
        "runs_used": _read_json_counter(COUNTER_PATH),
        "run_cap": RUN_CAP,
        "runs_remaining": max(0, RUN_CAP - _read_json_counter(COUNTER_PATH)),
        "live_runs_used": _read_json_counter(LIVE_COUNTER_PATH),
        "live_run_cap": LIVE_RUN_CAP,
        "live_runs_remaining": max(0, LIVE_RUN_CAP - _read_json_counter(LIVE_COUNTER_PATH)),
        "live_enabled": _live_enabled,
        "live_recipe_vendors": [
            {
                "label": _RECIPE_DISPLAY_NAMES.get(domain, domain),
                "domain": domain,
                "outcome_note": _RECIPE_OUTCOME_NOTES.get(domain, "outcome not yet characterized"),
            }
            for domain in sorted(LIVE_SIGNUP_RECIPES)
        ],
        "search_provider": "brave" if settings.brave_api_key else "ddg",
    }


@app.get("/api/version")
def version() -> dict:
    """What's actually running right now -- one request, no inference from
    artifact contents. This exists because a stale Railway deploy was once
    demoed as if it had a fix that was only in the local repo: the running
    instance's commit and the git remote's HEAD had silently diverged, and
    nothing short of reading pipeline output closely enough to notice a
    missing field would have caught it. `commit_sha` is `"unknown"` if this
    process was never told one -- e.g. a local `uvicorn` run, or a deploy
    where the deploy script's variable-set step was skipped -- an honest
    gap, not a guess at a value nobody actually confirmed."""
    commit_sha = os.environ.get("GIT_COMMIT_SHA", "unknown")
    return {
        "commit_sha": commit_sha,
        "commit_sha_short": commit_sha[:7] if commit_sha != "unknown" else "unknown",
        "credforge_version": credforge_version,
        "process_started_at": _PROCESS_STARTED_AT,
        "process_uptime_seconds": round(time.time() - _PROCESS_STARTED_AT, 1),
    }


@app.get("/api/examples")
def examples() -> dict:
    seed_batch = []
    report = None
    for f in sorted((EXAMPLES_DIR / "seed_batch").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if f.name == "report.json":
            report = data
        else:
            seed_batch.append(data)
    nasa_path = EXAMPLES_DIR / "nasa_live_credential.json"
    hitl_path = EXAMPLES_DIR / "hitl_task_etsy.md"
    return {
        "seed_batch": seed_batch,
        "report": report,
        "nasa_live_credential": json.loads(nasa_path.read_text(encoding="utf-8")) if nasa_path.exists() else None,
        "hitl_task_etsy": hitl_path.read_text(encoding="utf-8") if hitl_path.exists() else None,
    }


@app.post("/api/run")
async def api_run(request: Request, k: str | None = Query(None)) -> StreamingResponse:
    _check_token(k)
    body = await request.json()
    app_name = _validate_app_name(body.get("app_name", ""))

    ip = request.headers.get("x-forwarded-for", (request.client.host if request.client else "unknown"))
    ip = ip.split(",")[0].strip()
    if not _check_rate_limit(ip):
        raise HTTPException(429, "rate limit exceeded -- a few runs per minute per IP, try again shortly")

    recipe_match = _match_recipe_identity(app_name)  # (domain, docs_url) or None
    recipe_label = _RECIPE_DISPLAY_NAMES.get(recipe_match[0], recipe_match[0]) if recipe_match else None
    use_live = bool(recipe_match and _live_enabled)

    async with _counter_lock:
        used = _read_json_counter(COUNTER_PATH)
        if used >= RUN_CAP:
            raise HTTPException(429, f"global run cap ({RUN_CAP}) reached for this deployment -- see the examples below")
        _write_json_counter(COUNTER_PATH, used + 1)

    if use_live:
        async with _live_counter_lock:
            live_used = _read_json_counter(LIVE_COUNTER_PATH)
            if live_used >= LIVE_RUN_CAP:
                raise HTTPException(
                    429,
                    f"live-provisioning cap ({LIVE_RUN_CAP}) reached for this deployment -- "
                    "see the pre-computed NASA example instead",
                )
            _write_json_counter(LIVE_COUNTER_PATH, live_used + 1)

    active_providers = providers_live if use_live else providers_mock

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        start = time.monotonic()
        sink = SSEExplainSink(queue, start)
        run_id = new_run_id()
        # Labelling only -- what actually runs is unchanged (D-048/D-052/D-053).
        # The old copy ("Mocked provisioning...") read as if the whole run
        # were fake; in fact search, fetch, LLM extraction, classification,
        # and gating are always real -- only the signup stage ever uses a
        # stand-in, and for a vendor with no recipe it never even executes.
        queue.put_nowait(
            {
                "type": "mode",
                "live": use_live,
                "message": (
                    "LIVE CREDENTIAL ACQUISITION -- real signup, real credential, "
                    "validated with a real API call."
                    if use_live
                    else (
                        "RESEARCH + GATING -- real search, fetch and extraction. "
                        "No signup recipe for this vendor, so credential acquisition is not attempted."
                        if not recipe_match
                        else (
                            "RESEARCH + GATING -- real search, fetch and extraction. "
                            f"A signup recipe exists for {recipe_label}, but live provisioning is "
                            "disabled on this deployment, so credential acquisition is not attempted."
                        )
                    )
                ),
            }
        )

        async def runner() -> None:
            try:
                state, artifact = await run_app(
                    app_name,
                    providers=active_providers,
                    settings=settings,
                    registry=registry,
                    run_id=run_id,
                    data_dir=settings.data_dir,
                    vault=vault,
                    dry_run=False,
                    live=use_live,
                    explain=sink,
                )
                if artifact is None:
                    # Every terminal outcome produces a real artifact (D-038/D-039) --
                    # this branch is a defensive fallback, not the expected path.
                    stopped_at = "DISCOVER" if state.discovery is not None else "RESOLVE"
                    await queue.put({"type": "stopped", "stage": stopped_at})
                else:
                    # `evidence` entries shaped "candidate considered: <domain> (confidence <n>)"
                    # are how resolve_ambiguous/resolve_low_confidence candidates surface --
                    # the frontend parses these into clickable re-run cards.
                    safe_json = scrub_secrets(artifact.model_dump_json())
                    _assert_no_raw_credential(artifact)
                    artifact_dict = json.loads(safe_json)
                    # D-062: PROVISION still runs (mocked) for an AUTO app
                    # even on a non-live run, matching the CLI's own
                    # default mocked-PROVISION behavior (D-031) -- but the
                    # mode banner already told this run's viewer that
                    # credential acquisition was "not attempted". Left
                    # unmarked, the credential block below it (real vault
                    # refs, a mock-client-/mock-secret- prefixed value)
                    # would silently contradict that banner -- same class
                    # of bug as the source-tier contradiction (D-054): two
                    # parts of the same output asserting different things
                    # about the same fact. Marked explicitly, not
                    # suppressed -- this project's own rule throughout is
                    # to state which mode ran, never hide it.
                    if not use_live and artifact_dict.get("credential") is not None:
                        artifact_dict["credential"]["mocked"] = True
                    await queue.put({"type": "done", "artifact": artifact_dict})
            except SearchProviderError:
                await queue.put(
                    {
                        "type": "search_unavailable",
                        "message": (
                            "Search provider unavailable -- DuckDuckGo is unofficial and rate-limits "
                            "datacenter IPs; this is exactly why search sits behind a provider protocol "
                            "in this project. See the pre-computed examples below for real output."
                        ),
                    }
                )
            except Exception as exc:  # one run's crash never takes down the server
                await queue.put({"type": "error", "message": scrub_secrets(f"{type(exc).__name__}: {exc}")})
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
