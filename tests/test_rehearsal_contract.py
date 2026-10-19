from __future__ import annotations

import json
import builtins
import tomllib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hydrodynamics.rehearsal_contract import (
    DownstreamPolicy,
    ExecutionEnvelope,
    ProductCoverage,
    RehearsalContractError,
    ResultProductSelector,
    ResultSetManifest,
    ScenarioDraft,
    _load_synthetic_profile,
    evaluate_scenario,
    export_schemas,
    load_scenario_document,
    main as rehearsal_main,
    normalize_output_times,
    validate_execution_envelope,
    validate_selector_against_manifest,
)


EXAMPLES = Path(__file__).parents[1] / "examples" / "rehearsal"


def _base_scenario(**overrides):
    data = {
        "format": "rehearsal-scenario-v1",
        "schema_version": "1.0",
        "scenario_name": "synthetic contract test",
        "description": "test only",
        "creation_type": "MANUAL",
        "template_ref": None,
        "source_document_plan_id": None,
        "region_ids": ["SYNTHETIC_REGION_1"],
        "region_aliases": [],
        "event_id": None,
        "time_origin_local": None,
        "time_zone": "Asia/Shanghai",
        "duration_hours": 2.0,
        "output_step_minutes": 60.0,
        "engine_mode": "triangular_closed_experimental",
        "input_profile_ref": {"profile_id": "synthetic_closed_box_v1", "profile_version": "1.0"},
        "upper_boundaries": [],
        "rule_snapshot_ref": None,
        "initial_condition": {"mode": "PROFILE_REF", "profile_ref": {"profile_id": "synthetic_closed_box_v1", "profile_version": "1.0"}},
        "rainfall": {"mode": "EXPLICIT_NONE", "profile_ref": None},
        "downstream_policy": {"mode": "SYNTHETIC_CLOSED_DOMAIN", "profile_ref": {"profile_id": "synthetic_closed_box_v1", "profile_version": "1.0"}, "user_input_required": False, "applied": False},
        "requested_products": [
            {"product_code": "water_depth", "required": True, "scope_id": "SYNTHETIC_CLOSED_SCOPE", "aggregation": "FRAME"},
            {"product_code": "maximum_depth", "required": True, "scope_id": "SYNTHETIC_CLOSED_SCOPE", "aggregation": "MAX_OVER_WINDOW"},
        ],
        "metric_policy_version": "synthetic-demo-policy-v1",
        "historical_event_refs": [],
    }
    data.update(overrides)
    return data


def _boundary_source(source_mode: str) -> dict:
    base = {
        "source_mode": source_mode,
        "section_id": f"section-{source_mode.lower()}",
        "series_ref": {
            "series_id": f"series-{source_mode.lower()}",
            "dataset_version_id": "dataset-v1",
            "content_sha256": None,
            "quantity": "discharge",
            "unit": "m3/s",
            "direction": "inflow",
            "vertical_datum": None,
            "time_basis": "SCENARIO_RELATIVE_S",
            "temporal_policy": "piecewise_constant",
            "points": [{"time_s": 0.0, "value": 0.01}, {"time_s": 7200.0, "value": 0.01}],
        },
    }
    if source_mode == "FORECAST":
        base.update({"forecast_dataset_id": "forecast-1", "forecast_version_id": "f-v1",
                     "issued_at_local": "2023-08-01 01:00:00", "issued_time_zone": "Asia/Shanghai"})
    elif source_mode == "DESIGN":
        base.update({"design_dataset_id": "design-1", "design_version_id": "d-v1", "design_method_id": "method-v1",
                     "return_period_years": 20.0, "approval_ref": "approval-1"})
    elif source_mode == "HISTORICAL":
        base.update({"event_id": "event-1", "observation_dataset_id": "obs-1", "observation_version_id": "obs-v1",
                     "observation_kind": "OBSERVED", "station_or_section_ref": "station-1"})
    elif source_mode == "MANUAL":
        base.update({"rationale": "explicit synthetic input", "input_source_ref": "test-user-entry"})
    return base


def test_normalizes_output_axis_with_exact_terminal_frame():
    axis = normalize_output_times(120.0, 60.0)
    assert len(axis) == 121
    assert axis[0] == 0.0
    assert axis[-1] == 432_000.0
    assert axis[1] == 3_600.0
    assert normalize_output_times(2.5, 60.0) == [0.0, 3_600.0, 7_200.0, 9_000.0]


@pytest.mark.parametrize(
    "duration,step,message",
    [
        (True, 60.0, "booleans"),
        (1.0, False, "booleans"),
        (float("nan"), 60.0, "finite"),
        (1.0, 1e-12, "minimum"),
        (1.0, 0.9999999998611111, "at least"),  # terminal remainder collapses below the minimum gap
        (8760.0, 0.01, "point limit"),
    ],
)
def test_output_axis_rejects_invalid_or_unrepresentable_limits(duration, step, message):
    with pytest.raises(RehearsalContractError, match=message):
        normalize_output_times(duration, step)


@pytest.mark.parametrize("duration,step", [("120", 60), (120, "60")])
def test_public_output_axis_api_rejects_numeric_strings(duration, step):
    with pytest.raises(RehearsalContractError, match="numeric int/float"):
        normalize_output_times(duration, step)


def test_rejects_bool_numeric_string_extra_and_hidden_lower_boundary_values():
    raw = _base_scenario(duration_hours=True)
    with pytest.raises(ValidationError):
        ScenarioDraft.model_validate(raw)
    raw = _base_scenario(output_step_minutes="60")
    with pytest.raises(ValidationError):
        ScenarioDraft.model_validate(raw)
    raw = _base_scenario(supported=True)
    with pytest.raises(ValidationError):
        ScenarioDraft.model_validate(raw)
    raw = _base_scenario(downstream_policy={"mode": "CLOSED_WALL", "water_level_m": 0.0})
    with pytest.raises(ValidationError):
        ScenarioDraft.model_validate(raw)
    policy = DownstreamPolicy()
    assert policy.mode == "DEFERRED_UNKNOWN"
    assert policy.user_input_required is False
    assert policy.applied is False


@pytest.mark.parametrize("source_mode", ["FORECAST", "DESIGN", "HISTORICAL", "MANUAL"])
def test_strict_upper_boundary_contracts_preserve_all_four_source_modes(source_mode):
    raw = _base_scenario(upper_boundaries=[_boundary_source(source_mode)])
    scenario = ScenarioDraft.model_validate(raw)
    assert scenario.upper_boundaries[0].source_mode == source_mode
    assert scenario.upper_boundaries[0].series_ref.points[0].value == 0.01
    report = evaluate_scenario(scenario)
    assert report.can_save is True
    assert report.can_compute is False
    assert any(issue.code == "SYNTHETIC_CLOSED_PROFILE_HAS_OPEN_BOUNDARY" for issue in report.blockers)


def test_boundary_source_rejects_negative_unsigned_flow_wrong_unit_or_missing_datum():
    raw = _base_scenario(upper_boundaries=[_boundary_source("MANUAL")])
    raw["upper_boundaries"][0]["series_ref"]["points"][0]["value"] = -0.01
    with pytest.raises(ValidationError, match="non-negative"):
        ScenarioDraft.model_validate(raw)
    raw = _base_scenario(upper_boundaries=[_boundary_source("MANUAL")])
    raw["upper_boundaries"][0]["series_ref"]["unit"] = "m/s"
    with pytest.raises(ValidationError):
        ScenarioDraft.model_validate(raw)
    raw = _base_scenario(upper_boundaries=[_boundary_source("MANUAL")])
    series = raw["upper_boundaries"][0]["series_ref"]
    series.update({"quantity": "water_level", "unit": "m", "direction": None, "vertical_datum": None})
    with pytest.raises(ValidationError, match="vertical_datum"):
        ScenarioDraft.model_validate(raw)


def test_gate_action_geometry_or_opening_fields_are_not_reinterpreted_as_model_physics():
    raw = _base_scenario(rule_snapshot_ref={"rule_id": "rule-1", "rule_version": "v1", "opening_height_m": 3.2})
    with pytest.raises(ValidationError):
        ScenarioDraft.model_validate(raw)
    boundary = _boundary_source("MANUAL")
    boundary["opening_ratio"] = 1.0
    with pytest.raises(ValidationError):
        ScenarioDraft.model_validate(_base_scenario(upper_boundaries=[boundary]))


def test_nonexistent_and_ambiguous_local_times_are_rejected():
    base = _base_scenario(time_zone="America/New_York", time_origin_local="2024-03-10 02:30:00")
    with pytest.raises(ValidationError, match="nonexistent"):
        ScenarioDraft.model_validate(base)
    base["time_origin_local"] = "2024-11-03 01:30:00"
    with pytest.raises(ValidationError, match="ambiguous"):
        ScenarioDraft.model_validate(base)


def test_time_origin_must_be_present_as_timestamp_or_explicit_null():
    raw = _base_scenario()
    raw.pop("time_origin_local")
    with pytest.raises(ValidationError, match="time_origin_local"):
        ScenarioDraft.model_validate(raw)


def test_synthetic_preflight_uses_pinned_inputs_and_never_claims_publishability():
    scenario = load_scenario_document(EXAMPLES / "scenario_synthetic_closed_box.yaml")
    report = evaluate_scenario(scenario)
    assert report.can_save is True
    assert report.can_compute is True
    assert report.can_publish is False
    assert report.execution_readiness == "READY_FOR_SYNTHETIC_COMPUTE"
    assert report.provenance == "SYNTHETIC_MODEL"
    assert report.usage == "TEST_ONLY"
    assert report.production_approved is False
    assert report.output_frame_count == 121
    assert report.output_times_s[-1] == 432_000.0
    assert report.output_step_is_solver_dt is False
    assert report.downstream_user_input_required is False
    assert report.downstream_applied is False
    assert report.resolved_input_sha256 is not None
    state = next(item for item in report.product_capabilities if item.product_code == "water_depth")
    assert state.raw_available is True
    assert state.adapter_ready is False
    assert state.quality_passed is False
    assert state.publishable is False
    assert _load_synthetic_profile().profile.classification == "SYNTHETIC_ONLY"


def test_default_unknown_lower_boundary_is_savable_but_blocks_compute():
    raw = _base_scenario(downstream_policy={"mode": "DEFERRED_UNKNOWN"})
    scenario = ScenarioDraft.model_validate(raw)
    report = evaluate_scenario(scenario)
    assert report.can_save is True
    assert report.can_compute is False
    assert any(issue.code == "DOWNSTREAM_DATA_UNRESOLVED" for issue in report.blockers)
    assert report.downstream_user_input_required is False
    assert report.downstream_applied is False


def test_untrusted_profile_and_optional_support_claim_cannot_enable_compute():
    raw = _base_scenario(input_profile_ref={"profile_id": "real-domain", "profile_version": "1"})
    scenario = ScenarioDraft.model_validate(raw)
    report = evaluate_scenario(scenario)
    assert report.can_compute is False
    assert any(issue.code == "INPUT_PROFILE_NOT_IN_TRUSTED_REGISTRY" for issue in report.blockers)
    with pytest.raises(ValidationError):
        ScenarioDraft.model_validate({**raw, "engine_supported": True})


def test_tampered_pinned_profile_cannot_enable_preflight(monkeypatch):
    import hydrodynamics.rehearsal_contract as contract

    scenario = load_scenario_document(EXAMPLES / "scenario_synthetic_closed_box.yaml")
    monkeypatch.setattr(contract, "_PROFILE_SHA256", "0" * 64)
    report = evaluate_scenario(scenario)
    assert report.can_compute is False
    assert any(issue.code == "TRUSTED_PROFILE_INVALID" for issue in report.blockers)


def test_product_capability_matrix_blocks_required_but_reports_optional_unavailable_product():
    required = ScenarioDraft.model_validate(_base_scenario(requested_products=[
        {"product_code": "flood_loss", "required": True, "scope_id": "SYNTHETIC_CLOSED_SCOPE", "aggregation": "MAX_OVER_WINDOW"}
    ]))
    report_required = evaluate_scenario(required)
    assert report_required.can_compute is False
    assert any(issue.code == "ENGINE_PRODUCT_UNSUPPORTED" for issue in report_required.blockers)

    optional = ScenarioDraft.model_validate(_base_scenario(requested_products=[
        {"product_code": "flood_loss", "required": False, "scope_id": "SYNTHETIC_CLOSED_SCOPE", "aggregation": "MAX_OVER_WINDOW"}
    ]))
    report_optional = evaluate_scenario(optional)
    assert report_optional.can_compute is True
    assert report_optional.can_publish is False
    assert report_optional.product_capabilities[0].raw_available is False


def test_source_breakpoint_conflict_is_reported_without_silent_axis_mutation():
    source = _boundary_source("MANUAL")
    source["series_ref"]["points"] = [
        {"time_s": 0.0, "value": 0.01},
        {"time_s": 1_800.0, "value": 0.02},
        {"time_s": 7_200.0, "value": 0.01},
    ]
    raw = _base_scenario(upper_boundaries=[source])
    scenario = ScenarioDraft.model_validate(raw)
    report = evaluate_scenario(scenario)
    assert report.output_times_s == [0.0, 3_600.0, 7_200.0]
    assert report.proposed_output_times_s == [1_800.0]
    assert any(issue.code == "SOURCE_BREAKPOINT_NOT_ON_OUTPUT_AXIS" for issue in report.blockers)


def test_catalog_is_verified_read_only_and_transfer_scope_stays_unmapped():
    from hydrodynamics.rehearsal_catalog import load_builtin_rehearsal_catalog

    catalog = load_builtin_rehearsal_catalog()
    assert {item.source_id for item in catalog.sources} == {"S1", "S2", "S3", "S4"}
    assert all(item.review_status == "VERIFIED_BODY" for item in catalog.sources)
    assert all(item.runnable is False and item.engineering_validated is False for item in catalog.templates)
    transfer = next(item for item in catalog.events if item.event_key == "TJ23-XIQING-JINGHAI-EVAC-STATUS")
    assert transfer.mapped_region_id is None
    assert transfer.scope_alias == "XIQING_JINGHAI_RETENTION_AREA_REPORT"
    assert transfer.can_drive_model is False and transfer.gate_commands_sent == 0
    dongdian = next(item for item in catalog.templates if item.template_key == "TJ_2023_DONGDIAN_PUBLIC_HISTORY_V1")
    assert transfer.event_key in dongdian.event_keys
    assert report_has_unmapped_event_warning(dongdian.event_keys)
    eco = next(item for item in catalog.events if item.event_key == "TJ25-XIHE-ECO-REPORTED")
    assert eco.event_time_local is None
    assert eco.raw_time_text == "日前"
    assert eco.reported_at_local == "2025-03-07 09:15:00"
    assert eco.normalization_policy == "NO_EVENT_TIME_INFERENCE"
    assert eco.source_timezone_explicit is False


def test_catalog_loader_does_not_depend_on_ambient_zoneinfo_data(monkeypatch):
    from hydrodynamics.rehearsal_catalog import load_builtin_rehearsal_catalog

    real_import = builtins.__import__

    def reject_tzdata(name, *args, **kwargs):
        if name == "tzdata" or name.startswith("tzdata."):
            raise ModuleNotFoundError("simulated Windows environment without tzdata")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_tzdata)
    catalog = load_builtin_rehearsal_catalog()
    assert len(catalog.sources) == 4


def test_zoneinfo_is_declared_as_a_core_dependency_not_only_database_extra():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert any(dependency.startswith("tzdata>=") for dependency in pyproject["project"]["dependencies"])
    assert "tzdata" not in str(pyproject["project"]["optional-dependencies"].get("database", []))


def report_has_unmapped_event_warning(event_keys: list[str]) -> bool:
    raw = _base_scenario(historical_event_refs=event_keys)
    report = evaluate_scenario(ScenarioDraft.model_validate(raw))
    return any(issue.code == "HISTORICAL_SCOPE_UNMAPPED" for issue in report.warnings)


def test_template_clone_remains_an_incomplete_non_runnable_draft():
    from hydrodynamics.rehearsal_catalog import clone_builtin_template

    raw = clone_builtin_template("TJ_2023_DONGDIAN_PUBLIC_HISTORY_V1")
    scenario = ScenarioDraft.model_validate(raw)
    report = evaluate_scenario(scenario)
    assert scenario.creation_type == "BUILTIN_CLONE"
    assert scenario.input_profile_ref is None
    assert scenario.initial_condition.mode == "UNKNOWN"
    assert scenario.rainfall.mode == "UNKNOWN"
    assert scenario.downstream_policy.mode == "DEFERRED_UNKNOWN"
    assert scenario.region_ids == []
    assert report.can_save is False
    assert report.can_compute is False
    assert any(issue.code == "HISTORY_IS_NOT_BOUNDARY_INPUT" for issue in report.warnings)


def test_execution_envelope_preserves_platform_and_legacy_id_namespaces():
    scenario = load_scenario_document(EXAMPLES / "scenario_synthetic_closed_box.yaml")
    report = evaluate_scenario(scenario)
    envelope = ExecutionEnvelope.model_validate({
        "format": "rehearsal-execution-envelope-v1",
        "tenant_id": "tenant-test",
        "project_id": "project-test",
        "scenario_id": "scenario-test",
        "scenario_version_id": "scenario-version-1",
        "platform_run_id": "platform-run-1",
        "attempt_id": "attempt-1",
        "attempt_no": 1,
        "script_id": "20261019-1792400000000000000",
        "local_run_id": "20261019-1792400000000000000",
        "model_case_id": "rehearsal-synthetic-box-v1",
        "input_sha256": report.resolved_input_sha256,
        "engine_mode": "triangular_closed_experimental",
        "execution_origin": "PLATFORM_MODEL",
    })
    validate_execution_envelope(
        envelope, scenario, report, trusted_tenant_id="tenant-test", trusted_project_id="project-test",
        trusted_scenario_id="scenario-test", trusted_scenario_version_id="scenario-version-1",
        trusted_platform_run_id="platform-run-1", trusted_attempt_id="attempt-1", trusted_attempt_no=1,
        trusted_script_id="20261019-1792400000000000000",
    )
    bad = envelope.model_dump()
    bad["script_id"] = bad["platform_run_id"]
    bad["local_run_id"] = bad["platform_run_id"]
    with pytest.raises(ValidationError, match="platform_run_id"):
        ExecutionEnvelope.model_validate(bad)


def test_execution_envelope_rejects_stale_tenant_or_resolved_input_identity():
    scenario = load_scenario_document(EXAMPLES / "scenario_synthetic_closed_box.yaml")
    report = evaluate_scenario(scenario)
    envelope = ExecutionEnvelope.model_validate({
        "format": "rehearsal-execution-envelope-v1", "tenant_id": "tenant-a", "project_id": "project-a",
        "scenario_id": "scenario-a", "scenario_version_id": "version-a", "platform_run_id": "run-a",
        "attempt_id": "attempt-a", "attempt_no": 1, "script_id": "20261019-1792400000000000001",
        "local_run_id": "20261019-1792400000000000001", "model_case_id": "rehearsal-synthetic-box-v1",
        "input_sha256": report.resolved_input_sha256, "engine_mode": "triangular_closed_experimental",
    })
    with pytest.raises(RehearsalContractError, match="tenant/project"):
        validate_execution_envelope(envelope, scenario, report, trusted_tenant_id="tenant-b", trusted_project_id="project-a",
                                    trusted_scenario_id="scenario-a", trusted_scenario_version_id="version-a",
                                    trusted_platform_run_id="run-a", trusted_attempt_id="attempt-a", trusted_attempt_no=1,
                                    trusted_script_id="20261019-1792400000000000001")
    bad = envelope.model_copy(update={"input_sha256": "0" * 64})
    with pytest.raises(RehearsalContractError, match="input_sha256"):
        validate_execution_envelope(bad, scenario, report, trusted_tenant_id="tenant-a", trusted_project_id="project-a",
                                    trusted_scenario_id="scenario-a", trusted_scenario_version_id="version-a",
                                    trusted_platform_run_id="run-a", trusted_attempt_id="attempt-a", trusted_attempt_no=1,
                                    trusted_script_id="20261019-1792400000000000001")


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"script_id": "worker-script", "local_run_id": "worker-script"}, "script/local run ID differs"),
        ({"platform_run_id": "another-run"}, "platform_run_id differs"),
        ({"attempt_id": "stale-attempt"}, "attempt identity differs"),
        ({"attempt_no": 2}, "attempt identity differs"),
    ],
)
def test_worker_cannot_replace_backend_assigned_run_attempt_or_script(patch, message):
    scenario = load_scenario_document(EXAMPLES / "scenario_synthetic_closed_box.yaml")
    report = evaluate_scenario(scenario)
    base = {
        "format": "rehearsal-execution-envelope-v1", "tenant_id": "tenant-A", "project_id": "project-A",
        "scenario_id": "scenario-A", "scenario_version_id": "version-A", "platform_run_id": "platform-run-A",
        "attempt_id": "attempt-A", "attempt_no": 1, "script_id": "script-assigned-A", "local_run_id": "script-assigned-A",
        "model_case_id": report.resolved_model_case_id, "input_sha256": report.resolved_input_sha256,
        "engine_mode": scenario.engine_mode,
    }
    envelope = ExecutionEnvelope.model_validate({**base, **patch})
    with pytest.raises(RehearsalContractError, match=message):
        validate_execution_envelope(envelope, scenario, report, trusted_tenant_id="tenant-A", trusted_project_id="project-A",
                                    trusted_scenario_id="scenario-A", trusted_scenario_version_id="version-A",
                                    trusted_platform_run_id="platform-run-A", trusted_attempt_id="attempt-A",
                                    trusted_attempt_no=1, trusted_script_id="script-assigned-A")


def test_stale_preflight_report_cannot_authorize_changed_current_scenario():
    scenario = load_scenario_document(EXAMPLES / "scenario_synthetic_closed_box.yaml")
    stale_report = evaluate_scenario(scenario)
    raw = scenario.model_dump(mode="json")
    raw["rainfall"] = {"mode": "UNKNOWN", "profile_ref": None}
    changed = ScenarioDraft.model_validate(raw)
    current_report = evaluate_scenario(changed)
    assert stale_report.can_compute is True
    assert current_report.can_compute is False
    envelope = ExecutionEnvelope.model_validate({
        "format": "rehearsal-execution-envelope-v1", "tenant_id": "tenant-A", "project_id": "project-A",
        "scenario_id": "scenario-A", "scenario_version_id": "version-A", "platform_run_id": "platform-run-A",
        "attempt_id": "attempt-A", "attempt_no": 1, "script_id": "script-assigned-A", "local_run_id": "script-assigned-A",
        "model_case_id": stale_report.resolved_model_case_id, "input_sha256": stale_report.resolved_input_sha256,
        "engine_mode": scenario.engine_mode,
    })
    with pytest.raises(RehearsalContractError, match="preflight report is stale"):
        validate_execution_envelope(envelope, changed, stale_report, trusted_tenant_id="tenant-A", trusted_project_id="project-A",
                                    trusted_scenario_id="scenario-A", trusted_scenario_version_id="version-A",
                                    trusted_platform_run_id="platform-run-A", trusted_attempt_id="attempt-A",
                                    trusted_attempt_no=1, trusted_script_id="script-assigned-A")


def test_forged_preflight_cannot_authorize_an_untrusted_profile():
    scenario = load_scenario_document(EXAMPLES / "scenario_synthetic_closed_box.yaml")
    raw = scenario.model_dump(mode="json")
    raw["input_profile_ref"] = {"profile_id": "untrusted-real-domain", "profile_version": "1.0"}
    untrusted = ScenarioDraft.model_validate(raw)
    actual_report = evaluate_scenario(untrusted)
    assert actual_report.can_compute is False
    forged_report = actual_report.model_copy(update={
        "can_compute": True, "resolved_input_sha256": "a" * 64,
        "resolved_model_case_id": "rehearsal-synthetic-box-v1",
    })
    envelope = ExecutionEnvelope.model_validate({
        "format": "rehearsal-execution-envelope-v1", "tenant_id": "tenant-A", "project_id": "project-A",
        "scenario_id": "scenario-A", "scenario_version_id": "version-A", "platform_run_id": "platform-run-A",
        "attempt_id": "attempt-A", "attempt_no": 1, "script_id": "script-assigned-A", "local_run_id": "script-assigned-A",
        "model_case_id": "rehearsal-synthetic-box-v1", "input_sha256": "a" * 64,
        "engine_mode": "triangular_closed_experimental",
    })
    with pytest.raises(RehearsalContractError, match="preflight report is stale"):
        validate_execution_envelope(envelope, untrusted, forged_report, trusted_tenant_id="tenant-A", trusted_project_id="project-A",
                                    trusted_scenario_id="scenario-A", trusted_scenario_version_id="version-A",
                                    trusted_platform_run_id="platform-run-A", trusted_attempt_id="attempt-A",
                                    trusted_attempt_no=1, trusted_script_id="script-assigned-A")


def test_execution_envelope_cannot_be_created_for_unassessed_scenario():
    scenario = ScenarioDraft.model_validate(_base_scenario(input_profile_ref=None))
    report = evaluate_scenario(scenario)
    assert report.can_compute is False
    envelope = ExecutionEnvelope.model_validate({
        "format": "rehearsal-execution-envelope-v1", "tenant_id": "tenant-a", "project_id": "project-a",
        "scenario_id": "scenario-a", "scenario_version_id": "version-a", "platform_run_id": "run-a",
        "attempt_id": "attempt-a", "attempt_no": 1, "script_id": "20261019-1792400000000000002",
        "local_run_id": "20261019-1792400000000000002", "model_case_id": "case-a",
        "input_sha256": "0" * 64, "engine_mode": "triangular_closed_experimental",
    })
    with pytest.raises(RehearsalContractError, match="can_compute is false"):
        validate_execution_envelope(envelope, scenario, report, trusted_tenant_id="tenant-a", trusted_project_id="project-a",
                                    trusted_scenario_id="scenario-a", trusted_scenario_version_id="version-a",
                                    trusted_platform_run_id="run-a", trusted_attempt_id="attempt-a", trusted_attempt_no=1,
                                    trusted_script_id="20261019-1792400000000000002")


def _manifest() -> ResultSetManifest:
    return ResultSetManifest.model_validate({
        "result_set_id": "result-1", "metric_policy_version": "policy-1", "frame_times_s": [0.0, 10.0], "duration_s": 10.0,
        "scopes": [{"scope_id": "scope-a", "scope_hash": "a" * 64}],
        "product_coverage": [{
            "product_code": "water_depth", "scope_id": "scope-a", "scope_hash": "a" * 64,
            "aggregation": "FRAME", "frame_index": 1, "time_s": 10.0,
            "window_start_s": None, "window_end_s": None, "metric_policy_version": "policy-1",
            "status": "COMPLETE", "expected_count": 2, "valid_count": 2,
        }],
    })


def test_result_product_selector_matches_exact_scope_frame_aggregation_and_policy():
    manifest = _manifest()
    selector = ResultProductSelector.model_validate({
        "result_set_id": "result-1", "product_code": "water_depth", "scope_id": "scope-a", "scope_hash": "a" * 64,
        "aggregation": "FRAME", "frame_index": 1, "time_s": 10.0,
        "window_start_s": None, "window_end_s": None, "metric_policy_version": "policy-1",
    })
    assert validate_selector_against_manifest(selector, manifest).available is True
    mismatch = selector.model_copy(update={"time_s": 0.0})
    result = validate_selector_against_manifest(mismatch, manifest)
    assert result.status == "SELECTOR_MISMATCH"
    mismatch_scope = selector.model_copy(update={"scope_hash": "b" * 64})
    assert validate_selector_against_manifest(mismatch_scope, manifest).available is False
    with pytest.raises(ValidationError, match="FRAME"):
        ResultProductSelector.model_validate({
            "result_set_id": "result-1", "product_code": "water_depth", "scope_id": "scope-a", "scope_hash": "a" * 64,
            "aggregation": "FRAME", "frame_index": 1, "time_s": 10.0,
            "window_start_s": 0.0, "window_end_s": 10.0, "metric_policy_version": "policy-1",
        })


def test_selector_rejects_manifest_frame_time_and_scope_hash_inconsistency():
    bad_coverage = _manifest().model_dump(mode="json")
    bad_coverage["product_coverage"][0]["time_s"] = 0.0
    with pytest.raises(ValidationError, match="FRAME coverage time_s"):
        ResultSetManifest.model_validate(bad_coverage)
    bad_scope = _manifest().model_dump(mode="json")
    bad_scope["product_coverage"][0]["scope_hash"] = "b" * 64
    with pytest.raises(ValidationError, match="scope_id/scope_hash"):
        ResultSetManifest.model_validate(bad_scope)


def test_json_schema_is_strict_and_contains_discriminated_boundary_sources():
    schema = export_schemas("scenario")
    assert schema["additionalProperties"] is False
    assert "source_mode" in json.dumps(schema)
    assert "downstream_policy" in schema["properties"]
    assert "output_step_minutes" in schema["properties"]


def test_checked_in_scenario_schema_matches_the_current_model():
    schema_path = EXAMPLES / "rehearsal_scenario_v1.schema.json"
    checked_in = json.loads(schema_path.read_text(encoding="utf-8"))
    assert checked_in == export_schemas("scenario")


def test_cli_schema_catalog_validate_and_clone_exit_codes(tmp_path, capsys):
    assert rehearsal_main(["catalog", "list", "--kind", "sources"]) == 0
    sources = json.loads(capsys.readouterr().out)["items"]
    assert {item["source_id"] for item in sources} == {"S1", "S2", "S3", "S4"}

    assert rehearsal_main(["catalog", "show", "events", "TJ23-DONGDIAN-ACTIVATE"]) == 0
    event = json.loads(capsys.readouterr().out)
    assert event["event_time_local"] == "2023-08-01 02:00:00"
    assert event["can_drive_model"] is False

    schema_path = tmp_path / "scenario.schema.json"
    assert rehearsal_main(["schema", "--model", "scenario", "--output", str(schema_path)]) == 0
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert rehearsal_main(["schema", "--model", "scenario", "--output", str(schema_path)]) == 2
    capsys.readouterr()

    clone_path = tmp_path / "history-clone.yaml"
    assert rehearsal_main(["template", "clone", "TJ_2023_DONGDIAN_PUBLIC_HISTORY_V1", "--output", str(clone_path)]) == 0
    clone = yaml.safe_load(clone_path.read_text(encoding="utf-8"))
    assert clone["downstream_policy"]["mode"] == "DEFERRED_UNKNOWN"
    assert clone["requested_products"] == []
    assert rehearsal_main(["template", "clone", "TJ_2023_DONGDIAN_PUBLIC_HISTORY_V1", "--output", str(clone_path)]) == 2
    capsys.readouterr()

    assert rehearsal_main(["validate", str(EXAMPLES / "scenario_historical_clone_draft.yaml"), "--mode", "preflight"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["can_compute"] is False
    assert report["downstream_user_input_required"] is False
    assert rehearsal_main(["validate", str(EXAMPLES / "scenario_synthetic_closed_box.yaml"), "--mode", "preflight", "--require-compute"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["can_compute"] is True
    assert rehearsal_main(["validate", str(EXAMPLES / "scenario_invalid_boolean_duration.yaml")]) == 2
    capsys.readouterr()
    assert rehearsal_main(["validate", str(EXAMPLES / "scenario_invalid_numeric_string_duration.yaml")]) == 2
    capsys.readouterr()
    assert rehearsal_main(["validate", str(EXAMPLES / "scenario_invalid_lower_boundary.yaml")]) == 2
    capsys.readouterr()
