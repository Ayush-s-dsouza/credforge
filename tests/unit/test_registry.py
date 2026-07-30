from datetime import datetime, timezone

from credforge.enums import PipelineStage, StageStatus
from credforge.models.registry_entities import RegistryEntry
from credforge.registry.store import AppendOnlyRegistry


def _entry(**overrides) -> RegistryEntry:
    base = dict(
        identity_key="github.com",
        app_name="GitHub",
        run_id="run_test",
        stage=PipelineStage.RESOLVE,
        stage_status=StageStatus.COMPLETED,
        settings_fingerprint="fp1",
        recorded_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return RegistryEntry(**base)


def test_append_and_load_roundtrip(registry: AppendOnlyRegistry) -> None:
    registry.append(_entry())
    entries = registry.load_all()
    assert len(entries) == 1
    assert entries[0].identity_key == "github.com"


def test_latest_by_stage_prefers_most_recent(registry: AppendOnlyRegistry) -> None:
    registry.append(_entry(stage_status=StageStatus.STARTED))
    registry.append(_entry(stage_status=StageStatus.COMPLETED))
    latest = registry.latest_by_stage()
    assert latest[("github.com", "resolve")].stage_status == StageStatus.COMPLETED


def test_corrupted_line_is_skipped_not_fatal(registry: AppendOnlyRegistry) -> None:
    # Failure drill (1/2): a crash mid-write (or a manual edit) can leave one
    # truncated/garbled JSONL line. The registry must skip that line and keep
    # every well-formed entry around it -- losing the whole file to one bad
    # line would defeat the point of an append-only log.
    registry.append(_entry(identity_key="a.com"))
    with registry.path.open("a", encoding="utf-8") as f:
        f.write('{"identity_key": "b.com", "stage": "resolve"  GARBLED\n')
    registry.append(_entry(identity_key="c.com"))

    entries = registry.load_all()
    identity_keys = {e.identity_key for e in entries}
    assert identity_keys == {"a.com", "c.com"}


def test_find_open_provision_then_close(registry: AppendOnlyRegistry) -> None:
    # Failure drill (2/2): find_open_provision() is the idempotency guard
    # provision() will call before ever touching a browser/email provider --
    # it must return None once an account has been revoked, or a revoked
    # app would look re-provisionable and get double-provisioned.
    registry.append(
        _entry(
            stage=PipelineStage.PROVISION,
            stage_status=StageStatus.COMPLETED,
            email_alias="credforge+github@example.com",
            console_url="https://github.com/settings/developers",
            vault_ref="vault://github.com/oauth_client_secret",
        )
    )
    open_entry = registry.find_open_provision("github.com")
    assert open_entry is not None
    assert open_entry.console_url == "https://github.com/settings/developers"

    closed = registry.close("github.com", run_id="run_revoke")
    assert closed.closed is True
    assert registry.find_open_provision("github.com") is None
