from datetime import datetime, timezone
from pathlib import Path

from credforge.enums import ReasonCode, Status
from credforge.models.artifact import AppInfo, HandoffArtifact, Provenance
from credforge.pipeline.report import build_report, generate_report, load_artifacts


def _artifact(identity_key: str, app_name: str, status: Status, reason_code: ReasonCode, **kwargs) -> HandoffArtifact:
    return HandoffArtifact(
        status=status,
        reason_code=reason_code,
        app=AppInfo(app_name=app_name, identity_key=identity_key),
        provenance=Provenance(
            run_id="run_1", resolved_at=datetime.now(timezone.utc),
            emitted_at=datetime.now(timezone.utc), credforge_version="0.1.0",
        ),
        **kwargs,
    )


def test_build_report_counts_and_buckets_by_status() -> None:
    artifacts = [
        _artifact("a.com", "A", Status.AUTO, ReasonCode.ELIGIBLE_AUTO),
        _artifact("b.com", "B", Status.AUTO, ReasonCode.ELIGIBLE_AUTO),
        _artifact("c.com", "C", Status.HITL, ReasonCode.REQUIRES_PAYMENT),
        _artifact("d.com", "D", Status.UNSUPPORTED, ReasonCode.NO_PUBLIC_API),
    ]

    report = build_report("run_1", artifacts)

    assert report.total_apps == 4
    assert report.status_counts == {"AUTO": 2, "HITL": 1, "UNSUPPORTED": 1}
    assert report.reason_code_counts == {"eligible_auto": 2, "requires_payment": 1, "no_public_api": 1}
    assert sorted(report.auto_apps) == ["a.com", "b.com"]
    assert report.unsupported_apps == ["d.com"]
    assert len(report.needs_attention) == 1
    assert report.needs_attention[0].identity_key == "c.com"
    assert report.skipped_files == 0


def test_needs_attention_is_sorted_by_reason_code_for_scanability() -> None:
    artifacts = [
        _artifact("z.com", "Z", Status.HITL, ReasonCode.TOS_UNVERIFIABLE),
        _artifact("a.com", "A", Status.HITL, ReasonCode.REQUIRES_PAYMENT),
        _artifact("m.com", "M", Status.HITL, ReasonCode.REQUIRES_PAYMENT),
    ]

    report = build_report("run_1", artifacts)

    reason_codes = [item.reason_code.value for item in report.needs_attention]
    assert reason_codes == sorted(reason_codes)


def test_completeness_gaps_are_carried_into_the_attention_item() -> None:
    from credforge.models.state import CompletenessGap

    artifacts = [
        _artifact(
            "c.com", "C", Status.HITL, ReasonCode.TOS_UNVERIFIABLE,
            completeness_gaps=[CompletenessGap(field="base_url", reason="missing")],
        ),
    ]

    report = build_report("run_1", artifacts)

    assert report.needs_attention[0].completeness_gap_fields == ["base_url"]


def test_empty_artifact_list_produces_a_report_with_all_zero_counts_not_a_crash() -> None:
    report = build_report("run_1", [])

    assert report.total_apps == 0
    assert report.status_counts == {}
    assert report.needs_attention == []
    assert report.auto_apps == []


def test_load_artifacts_skips_a_corrupted_file_without_failing_the_whole_load(tmp_path: Path) -> None:
    # Failure drill: one bad artifact file must not sink the whole report,
    # same principle as AppendOnlyRegistry.load_all() skipping a corrupted
    # registry line (D-006).
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    good = _artifact("a.com", "A", Status.AUTO, ReasonCode.ELIGIBLE_AUTO)
    (artifacts_dir / "a-com.json").write_text(good.model_dump_json(), encoding="utf-8")
    (artifacts_dir / "corrupted.json").write_text("{not valid json at all", encoding="utf-8")

    artifacts, skipped = load_artifacts(artifacts_dir)

    assert len(artifacts) == 1
    assert artifacts[0].app.identity_key == "a.com"
    assert skipped == 1


def test_load_artifacts_on_a_missing_directory_returns_empty_not_a_crash(tmp_path: Path) -> None:
    artifacts, skipped = load_artifacts(tmp_path / "does-not-exist")
    assert artifacts == []
    assert skipped == 0


def test_generate_report_writes_report_json_to_the_expected_path(tmp_path: Path) -> None:
    run_id = "run_trace_1"
    artifacts_dir = tmp_path / "runs" / run_id / "artifacts"
    artifacts_dir.mkdir(parents=True)
    artifact = _artifact("a.com", "A", Status.AUTO, ReasonCode.ELIGIBLE_AUTO)
    (artifacts_dir / "a-com.json").write_text(artifact.model_dump_json(), encoding="utf-8")

    report = generate_report(run_id, data_dir=tmp_path)

    report_path = tmp_path / "runs" / run_id / "report.json"
    assert report_path.exists()
    assert report.total_apps == 1
    assert report.run_id == run_id
