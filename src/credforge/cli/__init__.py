"""credforge CLI -- typer app wiring `resolve`/`run`/`batch`/`report`/
`revoke` to the pipeline via pipeline/orchestrator.py. Thin by design: every
command parses arguments, opens the providers/vault/registry it needs, and
calls straight into orchestrator.py or pipeline/report.py -- no pipeline
logic lives here. See DECISIONS.md D-036.
"""

import asyncio
import csv
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..config import Settings
from ..enums import PipelineStage, StageStatus
from ..pipeline.discover_signup import discover_signup
from ..pipeline.explain import NULL_EXPLAIN, ExplainEvent, ExplainSink
from ..pipeline.orchestrator import run_app
from ..pipeline.report import RunReport, generate_report
from ..pipeline.resolve import resolve as resolve_stage
from ..providers.factory import ProviderBundle, build_providers
from ..redaction import scrub_secrets
from ..registry.identity import unresolved_identity_key
from ..registry.store import AppendOnlyRegistry
from ..run_context import new_run_id
from ..vault.crypto_vault import FernetVault

app = typer.Typer(help="credforge -- SaaS integration credential research + provisioning agent")
console = Console()


class ConsoleExplainSink:
    def emit(self, event: ExplainEvent) -> None:
        # scrub_secrets, not RedactionFilter -- this never goes through
        # `logging` at all, so the logging.Filter never sees it. See
        # DECISIONS.md D-043.
        message = scrub_secrets(event.message)
        console.print(f"  [dim]\\[{event.stage.value}][/dim] {event.identity_key}: {message}")


def _explain_sink(explain: bool) -> ExplainSink:
    return ConsoleExplainSink() if explain else NULL_EXPLAIN


def _open_vault(settings: Settings) -> FernetVault:
    if not settings.vault_key:
        console.print("[red]CREDFORGE_VAULT_KEY is not set.[/red] Generate one with:")
        console.print('  python -c "from credforge.vault.crypto_vault import generate_key; print(generate_key())"')
        raise typer.Exit(1)
    return FernetVault(key=settings.vault_key.get_secret_value(), path=settings.data_dir / "vault" / "secrets.vault")


def _open_registry(settings: Settings) -> AppendOnlyRegistry:
    return AppendOnlyRegistry(settings.data_dir / "registry.jsonl")


def _load_app_names(csv_path: Path) -> list[str]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = [row for row in csv.reader(f) if row and row[0].strip()]
    if rows and rows[0][0].strip().lower() in ("app_name", "app", "name"):
        rows = rows[1:]
    return [row[0].strip() for row in rows if row[0].strip()]


def _print_report_table(report: RunReport) -> None:
    table = Table(title=f"Run {report.run_id}")
    table.add_column("status")
    table.add_column("count", justify="right")
    for status, count in report.status_counts.items():
        table.add_row(status, str(count))
    console.print(table)

    if report.needs_attention:
        attention = Table(title="Needs attention (HITL)")
        attention.add_column("identity_key")
        attention.add_column("reason_code")
        attention.add_column("gaps")
        for item in report.needs_attention:
            attention.add_row(item.identity_key, item.reason_code.value, ", ".join(item.completeness_gap_fields))
        console.print(attention)

    if report.skipped_files:
        console.print(f"[yellow]{report.skipped_files} artifact file(s) could not be read and were skipped.[/yellow]")


@app.command()
def resolve(app_name: str) -> None:
    """Run RESOLVE only -- a quick check of what credforge would find, no other stage."""
    settings = Settings()
    providers = build_providers(settings)
    result = asyncio.run(
        resolve_stage(
            app_name, search=providers.search, fetch=providers.fetch,
            confidence_threshold=settings.resolve_confidence_threshold,
        )
    )
    console.print_json(result.model_dump_json())


@app.command()
def run(
    app_name: str,
    live: bool = typer.Option(False, "--live", help="Use real IMAP/Playwright providers instead of mocks"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Stop after GATE -- never provisions, even for an AUTO app"),
    explain: bool = typer.Option(False, "--explain", help="Print each stage's reasoning as it runs"),
) -> None:
    """Run the full pipeline for one app and emit its handoff artifact."""
    settings = Settings()
    providers = build_providers(settings, live=live)
    vault = None if dry_run else _open_vault(settings)
    registry = _open_registry(settings)
    run_id = new_run_id()

    state, artifact = asyncio.run(
        run_app(
            app_name, providers=providers, settings=settings, registry=registry, run_id=run_id,
            data_dir=settings.data_dir, vault=vault, dry_run=dry_run, live=live, explain=_explain_sink(explain),
        )
    )

    if artifact is None:
        stopped_at = "DISCOVER" if state.discovery is not None else "RESOLVE"
        reason = state.discovery.reason_code if state.discovery else (state.resolve.reason_code if state.resolve else None)
        console.print(f"[yellow]Stopped at {stopped_at}[/yellow] -- {reason}")
        raise typer.Exit(1)

    console.print(f"[bold]{artifact.status.value}[/bold] / {artifact.reason_code.value}  (run_id={run_id})")
    console.print_json(artifact.model_dump_json())


async def _run_batch(
    app_names: list[str],
    *,
    already_done: set[str],
    providers: ProviderBundle,
    settings: Settings,
    registry: AppendOnlyRegistry,
    run_id: str,
    vault: FernetVault | None,
    dry_run: bool,
    live: bool,
    explain: ExplainSink,
) -> None:
    for i, app_name in enumerate(app_names, start=1):
        if app_name in already_done:
            console.print(f"[{i}/{len(app_names)}] {app_name} ... [dim]skipped (already completed)[/dim]")
            continue
        console.print(f"[{i}/{len(app_names)}] {app_name} ...", end=" ")
        try:
            state, artifact = await run_app(
                app_name, providers=providers, settings=settings, registry=registry, run_id=run_id,
                data_dir=settings.data_dir, vault=vault, dry_run=dry_run, live=live,
                explain=explain,
            )
        except Exception as exc:  # one app's crash never stops the batch
            console.print(f"[red]ERROR[/red] {type(exc).__name__}: {exc}")
            continue

        if artifact is None:
            console.print("[yellow]stopped before GATE[/yellow]")
        else:
            console.print(f"{artifact.status.value} / {artifact.reason_code.value}")


@app.command()
def batch(
    csv_path: Path,
    live: bool = typer.Option(False, "--live"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    explain: bool = typer.Option(False, "--explain"),
) -> None:
    """Run every app in a CSV (one app name per row, optional header) through
    the full pipeline. One app's failure or crash never stops the batch.
    An app with a prior completed EMIT entry for the exact same app_name is
    skipped -- a simplified, name-based form of the resumability the
    registry was designed to support; see DECISIONS.md D-036 for why this
    isn't the full identity_key-based dedup PROVISION itself already has."""
    settings = Settings()
    providers = build_providers(settings, live=live)
    vault = None if dry_run else _open_vault(settings)
    registry = _open_registry(settings)
    run_id = new_run_id()

    app_names = _load_app_names(csv_path)
    already_done = {
        e.app_name
        for e in registry.load_all()
        if e.stage == PipelineStage.EMIT and e.stage_status == StageStatus.COMPLETED
    }

    # One event loop for the entire batch, not one per app (D-050): the
    # providers built above -- specifically HttpxFetchProvider's single
    # shared httpx.AsyncClient -- are constructed once and reused across
    # every app. A fresh `asyncio.run()` per app tears down and recreates
    # the event loop each time, and httpx's connection pool is bound to
    # whichever loop was active on its first real request; reusing it from
    # a *new* loop on the next app raises "RuntimeError: Event loop is
    # closed" -- found live, mid-batch, non-deterministically (it depends
    # on real connection-pool timing, not every app triggers it).
    asyncio.run(
        _run_batch(
            app_names, already_done=already_done, providers=providers, settings=settings, registry=registry,
            run_id=run_id, vault=vault, dry_run=dry_run, live=live, explain=_explain_sink(explain),
        )
    )

    report = generate_report(run_id, data_dir=settings.data_dir)
    console.print(f"\nrun_id: {run_id}")
    _print_report_table(report)


@app.command(name="discover-signup")
def discover_signup_cmd(
    app_name: str,
    live: bool = typer.Option(
        False, "--live",
        help="Actually fill and submit the form, extract and vault a real credential, and emit the recipe. "
        "Default is dry-run: locate, read, and classify the form, print what it WOULD do, submit nothing.",
    ),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window instead of running headless"),
) -> None:
    """DISCOVER_SIGNUP: generate a SignupRecipe for a vendor with no existing
    recipe. Only proceeds if GATE independently clears the app AUTO. Dry-run
    by default -- see DECISIONS.md D-065."""
    settings = Settings()
    providers = build_providers(settings, live=live)
    vault = _open_vault(settings) if live else None
    registry = _open_registry(settings)
    run_id = new_run_id()

    result = asyncio.run(
        discover_signup(
            app_name, providers=providers, settings=settings, registry=registry,
            vault=vault, run_id=run_id, live=live, headed=headed,
        )
    )
    console.print_json(result.model_dump_json())


@app.command()
def report(run_id: str) -> None:
    """Print the aggregate report for a run_id (regenerated from its artifacts/ dir)."""
    settings = Settings()
    r = generate_report(run_id, data_dir=settings.data_dir)
    _print_report_table(r)


@app.command()
def revoke(identity: str) -> None:
    """Mark a provisioned account closed in the registry, and print the
    manual steps credforge cannot do for you -- actually revoking the app
    on the vendor's own console is a human action, not an automatable one."""
    settings = Settings()
    registry = _open_registry(settings)
    # Accepts either a resolved domain ("github.com") or a raw app name --
    # a pragmatic heuristic (contains a dot => treat as a domain), not full
    # identity resolution; see DECISIONS.md D-036.
    identity_key = identity if "." in identity else unresolved_identity_key(identity)

    try:
        entry = registry.close(identity_key, run_id=new_run_id())
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"Marked {identity_key} closed in the registry.")
    console.print(f"Console URL (revoke the credential there yourself): {entry.console_url}")
    console.print("Manual steps credforge does not do for you:")
    console.print("  1. Open the console URL above and delete/revoke the OAuth app or API key.")
    console.print("  2. The vault ciphertext for this credential is NOT deleted (append-only, D-006) --")
    console.print("     it simply won't be reused by a future provision() call for this identity.")


if __name__ == "__main__":
    app()
