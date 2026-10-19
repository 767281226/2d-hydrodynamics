import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from hydrodynamics import (
    GateDatumConversionEvidence, GateDispatchInput, GateDispatchOptions, GateDispatchSnapshot, GateOperation,
    GateWaterLevel, NetCDFWriter, OutputField, SimulationOutput, build_gate_dispatch_log,
    check_time_origin_consistency, export_gate_dispatch_for_netcdf, gate_dispatch_paths,
    validate_gate_dispatch_exports, write_gate_dispatch_log,
)
from hydrodynamics.gate_dispatch import CSV_COLUMNS, GATE_RULES, RULE_SOURCE, RULE_SOURCE_SHA256, GateDispatchWriteError
from hydrodynamics.hourly_inundation import HourlyInundationSolver, write_hourly_netcdf


def water(value, datum="大沽高程"):
    return GateWaterLevel(value_m=value, vertical_datum=datum)


def snapshot(time=0, **values):
    return GateDispatchSnapshot(time_s=time, source="test fixture", **values)


def statuses(log):
    return {item.rule_id: item.status for item in log.final_evaluations}


def evaluate(**values):
    options = values.pop("options", GateDispatchOptions())
    return build_gate_dispatch_log([0], GateDispatchInput(snapshots=(snapshot(**values),), options=options))


def test_catalog_matches_reviewed_source_document():
    source = Path(RULE_SOURCE)
    if not source.exists():
        pytest.skip("source document is not available")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == RULE_SOURCE_SHA256
    assert len(GATE_RULES) == len({rule.rule_id for rule in GATE_RULES}) == 13


def test_documented_gate_dispatch_example_preserves_unconfirmed_source_categories():
    example = Path(__file__).parents[1] / "examples" / "gate_dispatch.example.json"
    data = GateDispatchInput.model_validate_json(example.read_text(encoding="utf-8"))
    assert data.options.snapshot_time_mode == "output_aligned"
    assert data.options.near_5_75_tolerance_source == "假设示例解释；不是源文档中的算法"
    assert all(snapshot.source_category == "undeclared" for snapshot in data.snapshots)
    assert all(operation.source_category == "undeclared" for operation in data.operations)


def test_reaching_4_2_and_discharge_700_have_different_inclusive_boundaries():
    log = evaluate(sixth_port_level=water(4.2), zaolinzhuang_discharge_m3s=700)
    assert statuses(log)["GDR-001"] == "triggered"
    assert statuses(log)["GDR-004"] == "not_triggered"
    log = evaluate(sixth_port_level=water(4.199), zaolinzhuang_discharge_m3s=700.01)
    assert statuses(log)["GDR-001"] == "not_triggered"
    assert statuses(log)["GDR-004"] == "triggered"
    assert statuses(evaluate(sixth_port_level=water(4.3, "1985国家高程基准")))["GDR-001"] == "indeterminate"


def test_near_requires_explicit_tolerance_and_forecast_and_xihe_judgement():
    log = evaluate(sixth_port_level=water(5.75), sixth_port_forecast_rising=True)
    assert statuses(log)["GDR-002"] == "indeterminate"
    options = GateDispatchOptions(near_5_75_tolerance_m=0.05)
    log = evaluate(sixth_port_level=water(5.79), sixth_port_forecast_rising=True, options=options)
    assert statuses(log)["GDR-002"] == "triggered"
    assert statuses(log)["GDR-003"] == "indeterminate"
    log = evaluate(sixth_port_level=water(5.81), sixth_port_forecast_rising=True, options=options)
    assert statuses(log)["GDR-002"] == "not_triggered"
    log = evaluate(sixth_port_level=water(5.75), sixth_port_forecast_rising=False, options=options)
    assert statuses(log)["GDR-002"] == "not_triggered"


def test_850_m3s_remains_main_channel_background_not_a_gate_target():
    options = GateDispatchOptions(near_5_75_tolerance_m=0.05)
    baseline = evaluate(sixth_port_level=water(5.75), sixth_port_forecast_rising=True, main_channel_discharge_m3s=850.0, options=options)
    changed = evaluate(sixth_port_level=water(5.75), sixth_port_forecast_rising=True, main_channel_discharge_m3s=0.0, options=options)
    assert statuses(baseline)["GDR-002"] == statuses(changed)["GDR-002"] == "triggered"
    evaluation = next(item for item in baseline.final_evaluations if item.rule_id == "GDR-002")
    context = next(item for item in evaluation.conditions if item.get("role") == "background_context")
    assert context["reference_value"] == 850.0 and context["operator"] == "context_only"


def test_zhongting_400_is_not_an_automatic_xihe_gate_threshold():
    assert statuses(evaluate(zhongting_discharge_m3s=399))["GDR-005"] == "indeterminate"
    assert statuses(evaluate(zhongting_discharge_m3s=399, xihe_diversion_suitable=False))["GDR-005"] == "not_triggered"
    assert statuses(evaluate(xihe_diversion_suitable=True))["GDR-005"] == "triggered"


def test_return_gates_require_positive_head_and_matching_datums():
    log = evaluate(dongdian_level=water(4.5), daqing_river_level=water(4.5))
    assert all(statuses(log)[f"GDR-{i:03d}"] == "not_triggered" for i in range(6, 10))
    log = evaluate(dongdian_level=water(4.6), daqing_river_level=water(4.5, "other"))
    assert statuses(log)["GDR-006"] == "indeterminate"
    log = evaluate(dongdian_level=water(4.6), daqing_river_level=water(4.5))
    assert all(statuses(log)[f"GDR-{i:03d}"] == "triggered" for i in range(6, 10))


def test_qingnan_separate_scope_rules_require_threshold_datum_and_full_use():
    values = dict(qingbei_level=water(7), qingbei_rising=True,
                  dongdian_sixth_port_level=water(7), dongdian_sixth_port_rising=True,
                  qingbei_fully_used=False)
    assert statuses(evaluate(**values))["GDR-010"] == "indeterminate"
    log = evaluate(**values, options=GateDispatchOptions(qingnan_threshold_datum="大沽高程"))
    assert statuses(log)["GDR-010"] == "triggered"
    assert statuses(log)["GDR-011"] == "not_triggered"


@pytest.mark.parametrize("season,threshold,rule_id", [(True, 3.0, "GDR-012"), (False, 3.8, "GDR-013")])
def test_tidal_gate_uses_season_and_strict_threshold_and_tide_window(season, threshold, rule_id):
    values = dict(main_flood_season=season, tidal_release_window=True, tidal_release_authorized=True)
    assert statuses(evaluate(tidal_gate_level=water(threshold, "大沽冻结"), **values))[rule_id] == "not_triggered"
    assert statuses(evaluate(tidal_gate_level=water(threshold + .01, "大沽冻结"), **values))[rule_id] == "triggered"
    values["tidal_release_window"] = None
    assert statuses(evaluate(tidal_gate_level=water(threshold + .01, "大沽冻结"), **values))[rule_id] == "indeterminate"
    values["tidal_release_window"] = False
    assert statuses(evaluate(tidal_gate_level=water(threshold + .01, "大沽冻结"), **values))[rule_id] == "not_triggered"


def test_dedup_missing_sample_and_condition_clear_do_not_generate_gate_operations():
    data = GateDispatchInput(snapshots=tuple(snapshot(t, sixth_port_level=water(v)) for t, v in (
        (0, 4.0), (3600, 4.3), (7200, 4.4), (14400, 4.4), (18000, 4.1))))
    log = build_gate_dispatch_log([0, 3600, 7200, 10800, 14400, 18000], data)
    events = [r for r in log.records if r["rule_id"] == "GDR-001"]
    assert [r["record_type"] for r in events] == ["condition_state", "trigger", "unavailable", "trigger", "condition_cleared"]
    assert events[-1]["action"] == ""
    assert not any(r["record_type"] == "operation" for r in log.records)
    assert log.indeterminate_sample_counts["GDR-001"] == 1


def test_full_snapshot_evaluations_keep_evidence_without_repeating_same_state_events():
    data = GateDispatchInput(snapshots=(
        snapshot(0, source_category="external_report", sixth_port_level=water(4.3)),
        snapshot(3600, source_category="reconstruction", sixth_port_level=water(4.4)),
    ))
    first = build_gate_dispatch_log([0, 3600], data)
    again = build_gate_dispatch_log([0, 3600], data)
    rule_evaluations = [item for item in first.evaluations if item["rule_id"] == "GDR-001"]
    rule_events = [item for item in first.records if item["rule_id"] == "GDR-001"]
    assert len(rule_evaluations) == 2
    assert len(rule_events) == 1 and rule_events[0]["record_type"] == "trigger"
    assert rule_evaluations[0]["snapshot_id"] != rule_evaluations[1]["snapshot_id"]
    assert [item["evaluation_id"] for item in rule_evaluations] == [item["evaluation_id"] for item in again.evaluations if item["rule_id"] == "GDR-001"]
    assert rule_evaluations[0]["conditions"][0]["operator"] == ">="
    assert rule_evaluations[0]["evidence"]["source_category"] == "external_report"
    assert "qingbei_level" in first.snapshots[0] and first.snapshots[0]["qingbei_level"] is None
    assert rule_events[0]["evaluation_id"] == rule_evaluations[0]["evaluation_id"]


def test_structured_operation_sources_separate_model_changes_from_reported_actions():
    operations = (
        GateOperation(time_s=0, gate_id="duliu_inlet", action="model flow", reason="prescribed", source="forcing", source_category="model_prescribed_change"),
        GateOperation(time_s=0, gate_id="xihe", action="reconstructed", reason="derived", source="reconstruction", source_category="reconstruction"),
        GateOperation(time_s=1800, gate_id="renzhuangzi", action="report", reason="source report", source="bulletin", source_category="external_report"),
        GateOperation(time_s=1800, gate_id="duliu_tidal", action="legacy", reason="old record", source="legacy input"),
    )
    log = build_gate_dispatch_log([0, 3600], GateDispatchInput(operations=operations))
    payload = log.to_dict("synthetic.nc")
    assert payload["reported_operation_count"] == 4  # V1.0 compatibility: all supplied operation records.
    assert payload["operation_source_category_counts"] == {
        "model_prescribed_change": 1, "reconstruction": 1, "external_report": 1, "undeclared": 1,
    }
    assert payload["model_prescribed_change_count"] == 1
    assert payload["asserted_external_action_count"] == 1
    model_event = next(item for item in log.records if item.get("operation_source_category") == "model_prescribed_change")
    assert model_event["action_stage"] == "model_input_change_not_verified_gate_action"


def test_independent_observation_times_are_preserved_without_frame_alignment_or_interpolation():
    options = GateDispatchOptions(snapshot_time_mode="independent_observations")
    op = GateOperation(time_s=1800, gate_id="duliu_inlet", rule_id="GDR-001", action="report", reason="reported", source="station report", source_category="external_report")
    inputs = GateDispatchInput(
        time_origin="2023-08-01T02:00:00+08:00",
        options=options,
        snapshots=(snapshot(1800, sixth_port_level=water(4.3)), snapshot(2700, sixth_port_level=water(4.4))),
        operations=(op,),
    )
    log = build_gate_dispatch_log([0, 3600], inputs)
    assert [item["time_s"] for item in log.snapshots] == [1800.0, 2700.0]
    assert {item["time_s"] for item in log.evaluations} == {1800.0, 2700.0}
    rule_evaluations = [item for item in log.evaluations if item["rule_id"] == "GDR-001"]
    assert len(rule_evaluations) == 2
    assert log.indeterminate_sample_counts["GDR-002"] == 2
    operation = next(item for item in log.records if item["record_type"] == "operation")
    assert operation["time_s"] == 1800.0
    assert operation["evaluation_id"] == rule_evaluations[0]["evaluation_id"]
    assert operation["action_stage"] == "external_action_report_transcribed"

    no_observations = build_gate_dispatch_log([0, 3600], GateDispatchInput(options=options))
    assert no_observations.evaluations == ()
    assert no_observations.records == ()
    with pytest.raises(ValueError, match="inside the output time window"):
        build_gate_dispatch_log([0, 3600], GateDispatchInput(options=options, snapshots=(snapshot(3601),)))


def test_tidal_water_condition_and_dispatch_arrangement_stages_are_separate():
    values = dict(tidal_gate_level=water(3.1, "大沽冻结"), main_flood_season=True)
    log = evaluate(**values)
    tidal = next(item for item in log.final_evaluations if item.rule_id == "GDR-012")
    assert tidal.water_condition_status == "triggered"
    assert tidal.action_stage == "request_arrangement"
    assert tidal.status == "indeterminate"
    assert evaluate(**values, tidal_release_authorized=False).final_evaluations[11].action_stage == "awaiting_arrangement"
    approval_without_reference = evaluate(**values, tidal_release_authorized=True, tidal_release_window=True).final_evaluations[11]
    assert approval_without_reference.status == "triggered"
    assert approval_without_reference.action_stage == "awaiting_authorization_evidence"
    assert "tidal_release_authorization_reference" in approval_without_reference.missing
    assert evaluate(**values, tidal_release_authorized=True, tidal_release_window=False,
                    tidal_release_authorization_reference="approval-1").final_evaluations[11].action_stage == "awaiting_tidal_window"
    assert evaluate(**values, tidal_release_authorized=True, tidal_release_window=True,
                    tidal_release_authorization_reference="approval-1").final_evaluations[11].action_stage == "awaiting_tidal_window_evidence"
    assert evaluate(**values, tidal_release_authorized=True, tidal_release_window=True,
                    tidal_release_authorization_reference="approval-1", tidal_release_window_reference="tide-window-1").final_evaluations[11].action_stage == "ready_for_manual_decision"
    evidence_log = build_gate_dispatch_log([0, 3600], GateDispatchInput(snapshots=(
        snapshot(0, **values, tidal_release_authorized=True, tidal_release_window=True),
        snapshot(3600, **values, tidal_release_authorized=True, tidal_release_window=True,
                 tidal_release_authorization_reference="approval-1", tidal_release_window_reference="tide-window-1"),
    )))
    tidal_events = [event for event in evidence_log.records if event["rule_id"] == "GDR-012"]
    assert [event["record_type"] for event in tidal_events] == ["trigger", "condition_state"]
    assert all(event["rule_status"] == "triggered" for event in tidal_events)
    renzhuangzi = next(item for item in evaluate(zaolinzhuang_discharge_m3s=701).final_evaluations if item.rule_id == "GDR-004")
    assert renzhuangzi.action_stage == "request_external_authority"


def test_threshold_basis_coverage_and_scoped_datum_evidence_are_not_applied():
    evidence = GateDatumConversionEvidence(
        source_datum="大沽高程", target_datum="1985国家高程基准", scope="下游东淀-文安洼区域",
        direction="target = source - 1.56 m", offset_m=-1.56,
        source_description="user-confirmed scoped conversion", evidence_ref="user-confirmed-input:datum-conversion",
        applied=False,
    )
    log = build_gate_dispatch_log([0], GateDispatchInput(
        datum_conversion_evidence=evidence,
        snapshots=(snapshot(sixth_port_level=water(4.3, "1985国家高程基准"), tidal_gate_level=water(3.1, "大沽高程")),),
    ))
    references = {item.rule_id: item.threshold_reference for item in log.final_evaluations}
    assert references["GDR-001"]["source_paragraph_number_1based"] == 3
    assert references["GDR-001"]["thresholds"][0]["comparison_threshold"] == 4.2
    assert references["GDR-001"]["thresholds"][0]["datum"] == "大沽高程"
    assert references["GDR-012"]["thresholds"][0]["datum"] == "大沽冻结"
    assert next(item for item in log.final_evaluations if item.rule_id == "GDR-001").status == "indeterminate"
    payload = log.to_dict("datum.nc")
    assert payload["datum_reference_context"]["scoped_project_evidence"]["offset_m"] == -1.56
    assert payload["datum_reference_context"]["scoped_project_evidence_applied"] is False
    assert any("1.668 m" in note["relation"] and note["applied"] is False for note in payload["datum_reference_context"]["appendix_notes"])
    assert any(item.get("source_paragraph_number") == 20 and item.get("breach_inference") is False for item in payload["rule_coverage"]["manual_review_only"])
    with pytest.raises(ValidationError, match="preserve the scoped target"):
        GateDatumConversionEvidence(
            source_datum="大沽高程", target_datum="1985国家高程基准", scope="全国",
            direction="target = source - 1.668 m", offset_m=-1.668,
            source_description="unscoped appendix offset", evidence_ref="bad", applied=False,
        )


def test_time_origin_comparison_accepts_equivalent_zones_and_rejects_conflicts():
    input_origin = "2023-08-01T02:00:00+08:00"
    result_origin = "2023-07-31T18:00:00Z"
    assert check_time_origin_consistency(input_origin, result_origin) == input_origin
    with pytest.raises(ValueError, match="same instant"):
        check_time_origin_consistency(input_origin, "2023-08-01T03:00:00+08:00")
    log = build_gate_dispatch_log([0, 3600], GateDispatchInput(time_origin=input_origin), time_origin=result_origin)
    assert log.inputs.time_origin == input_origin
    assert log.to_dict("same.nc")["time_origins"]["resolution"] == "same_instant"
    with pytest.raises(ValueError, match="same instant"):
        build_gate_dispatch_log([0, 3600], GateDispatchInput(time_origin=input_origin), time_origin="2023-08-01T03:00:00+08:00")


def test_origin_conflict_is_rejected_before_generic_hourly_or_existing_nc_outputs(tmp_path):
    origin = "2023-08-01T02:00:00+08:00"
    conflicting = "2023-08-01T03:00:00+08:00"
    field = OutputField("water_depth", [[[1]], [[2]]], 1, 1, 30, 30, 0, 0, times=[0, 3600])
    generic_path = tmp_path / "conflict_generic.nc"
    output = SimulationOutput([field], metadata={"event_start": conflicting}, gate_dispatch=GateDispatchInput(time_origin=origin))
    with pytest.raises(ValueError, match="same instant"):
        NetCDFWriter.write(output, generic_path)
    assert not generic_path.exists() and not any(path.exists() for path in gate_dispatch_paths(generic_path))

    solver = HourlyInundationSolver(np.zeros((1, 1)), np.ones((1, 1), dtype=bool), dx=50, dy=50, xmin=0, ymin=0)
    hourly_path = tmp_path / "conflict_hourly.nc"
    with pytest.raises(ValueError, match="same instant"):
        write_hourly_netcdf(hourly_path, solver, times_s=[0, 3600], inflow=[0, 1], outflow=[0, 0], rain_mm=[0, 0],
                            coordinate_system="local", vertical_datum="local", extra_attrs={"event_start": conflicting},
                            gate_dispatch=GateDispatchInput(time_origin=origin))
    assert solver.time == solver.volume == 0
    assert not hourly_path.exists() and not any(path.exists() for path in gate_dispatch_paths(hourly_path))

    import netCDF4
    existing_path = tmp_path / "conflict_existing.nc"
    with netCDF4.Dataset(existing_path, "w") as dataset:
        dataset.createDimension("time", 2)
        dataset.createVariable("time", "f8", ("time",))[:] = [0, 3600]
        dataset.format = "2d-hydrodynamics-netcdf"
        dataset.event_start = origin
        dataset.gate_dispatch_time_origin = conflicting
    before = existing_path.read_bytes()
    with pytest.raises(ValueError, match="same instant"):
        export_gate_dispatch_for_netcdf(existing_path)
    assert existing_path.read_bytes() == before
    assert not any(path.exists() for path in gate_dispatch_paths(existing_path))


def test_companion_pair_rollback_preserves_both_existing_files(tmp_path, monkeypatch):
    import hydrodynamics.gate_dispatch as gate_module

    result = tmp_path / "rollback.nc"
    csv_path, json_path = gate_dispatch_paths(result)
    csv_path.write_bytes(b"previous CSV")
    json_path.write_bytes(b"previous JSON")
    old_csv, old_json = csv_path.read_bytes(), json_path.read_bytes()
    log = build_gate_dispatch_log([0, 3600])
    real_replace = gate_module.os.replace
    failed = False

    def fail_json_once(source, target):
        nonlocal failed
        if Path(target) == json_path and not failed:
            failed = True
            raise OSError("synthetic second-file install failure")
        return real_replace(source, target)

    monkeypatch.setattr(gate_module.os, "replace", fail_json_once)
    with pytest.raises(GateDispatchWriteError, match="synthetic second-file install failure"):
        write_gate_dispatch_log(result, log, overwrite=True)
    assert failed
    assert csv_path.read_bytes() == old_csv
    assert json_path.read_bytes() == old_json


def test_failed_csv_restore_keeps_the_only_recovery_copy_and_reports_its_path(tmp_path, monkeypatch):
    import hydrodynamics.gate_dispatch as gate_module

    result = tmp_path / "recoverable.nc"
    csv_path, json_path = gate_dispatch_paths(result)
    original_csv = b"the only original CSV recovery copy"
    csv_path.write_bytes(original_csv)
    json_path.write_bytes(b"original JSON")
    real_replace = gate_module.os.replace

    def fail_second_install_and_csv_restore(source, target):
        if Path(target) == json_path:
            raise OSError("synthetic JSON install failure")
        if Path(target) == csv_path and ".backup." in Path(source).name:
            raise OSError("synthetic CSV restore failure")
        return real_replace(source, target)

    monkeypatch.setattr(gate_module.os, "replace", fail_second_install_and_csv_restore)
    with pytest.raises(GateDispatchWriteError, match="recovery backup retained at") as error:
        write_gate_dispatch_log(result, build_gate_dispatch_log([0, 1]), overwrite=True)
    backups = [path for path in tmp_path.glob(csv_path.name + ".backup.*") if path.read_bytes() == original_csv]
    assert len(backups) == 1
    assert str(backups[0]) in str(error.value)


def test_pair_reader_rejects_tampered_csv_action_even_if_its_hash_is_rewritten(tmp_path):
    result = tmp_path / "tampered.nc"
    operation = GateOperation(time_s=0, gate_id="duliu_inlet", action="original action", reason="fixture", source="fixture")
    csv_path, json_path = write_gate_dispatch_log(result, build_gate_dispatch_log([0], GateDispatchInput(operations=(operation,))))
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns, rows = reader.fieldnames, list(reader)
    operation_row = next(row for row in rows if row["record_type"] == "operation")
    operation_row["action"] = "tampered action"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload["output_consistency"]["csv_sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="CSV action does not match JSON record"):
        validate_gate_dispatch_exports(result)


def test_consistency_reader_checks_every_csv_scalar_even_if_csv_hash_is_updated(tmp_path):
    result = tmp_path / "scalar-check.nc"
    operation = GateOperation(time_s=0, gate_id="duliu_inlet", action="original action", reason="fixture", source="fixture")
    log = build_gate_dispatch_log([0], GateDispatchInput(operations=(operation,)))
    csv_path, json_path = write_gate_dispatch_log(result, log)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns, rows = reader.fieldnames, list(reader)
    operation_row = next(row for row in rows if row["record_type"] == "operation")
    operation_row["action"] = "tampered CSV action"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload["output_consistency"]["csv_sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="CSV action does not match JSON record"):
        validate_gate_dispatch_exports(result)


@pytest.mark.parametrize("header_change", ["missing", "extra", "duplicate"])
def test_pair_reader_requires_fixed_columns_when_manifest_and_hash_are_rewritten(tmp_path, header_change):
    result = tmp_path / f"header-{header_change}.nc"
    csv_path, json_path = write_gate_dispatch_log(result, build_gate_dispatch_log([0]))
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns, rows = list(reader.fieldnames), list(reader)
    if header_change == "missing":
        columns.remove("opening_before_ratio")
    elif header_change == "extra":
        columns.append("unrecognized_column")
    else:
        columns.append("opening_before_ratio")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload["output_consistency"]["csv_columns"] = columns
    payload["output_consistency"]["csv_sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="fixed CSV_COLUMNS"):
        validate_gate_dispatch_exports(result)


def test_reported_operation_preserves_source_and_values_without_interpolated_rule_evidence(tmp_path):
    operation = GateOperation(time_s=1800, gate_id="duliu_inlet", rule_id="GDR-001",
                              action="调开度", reason="controller event", source="controller:step-1",
                              approval_ref="approval-7", opening_before_ratio=.2, opening_after_ratio=.8)
    data = GateDispatchInput(time_origin="2023-08-01T02:00:00+08:00",
                             snapshots=(snapshot(sixth_port_level=water(4.3)),), operations=(operation,))
    log = build_gate_dispatch_log([0, 3600], data)
    csv_path, json_path = write_gate_dispatch_log(tmp_path / "a.nc", log)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    event = next(row for row in rows if row["record_type"] == "operation")
    assert event["occurred_at"] == "2023-08-01T02:30:00+08:00"
    assert event["operation_source"] == "controller:step-1"
    assert event["opening_after_ratio"] == "0.8"
    assert event["rule_status"] == "indeterminate"
    assert json.loads(json_path.read_text(encoding="utf-8"))["reported_operation_count"] == 1


@pytest.mark.parametrize("values", [
    {"time_s": -1}, {"time_s": True}, {"time_s": float("nan")},
    {"time_s": "1"}, {"qingbei_rising": "true"}, {"zaolinzhuang_discharge_m3s": -1},
    {"main_channel_discharge_m3s": -1},
    {"unknown_field": 1},
])
def test_invalid_snapshot_inputs_have_field_validation_errors(values):
    with pytest.raises(ValidationError):
        GateDispatchSnapshot(**{"time_s": 0, "source": "fixture", **values})


def test_input_time_order_and_operation_identity_and_values_are_validated():
    with pytest.raises(ValidationError, match="timezone"):
        GateDispatchInput(time_origin="2023-08-01T02:00:00")
    with pytest.raises(ValidationError, match="strictly increasing"):
        GateDispatchInput(snapshots=(snapshot(1), snapshot(1)))
    fields = dict(time_s=0, gate_id="duliu_inlet", action="open", reason="test", source="fixture")
    with pytest.raises(ValidationError, match="same gate_id"):
        GateOperation(**fields, rule_id="GDR-012")
    with pytest.raises(ValidationError, match="opening_after_ratio"):
        GateOperation(**fields, opening_after_ratio=1.01)
    with pytest.raises(ValidationError, match="repeat"):
        op = GateOperation(**fields)
        GateDispatchInput(operations=(op, op))
    with pytest.raises(ValueError, match="exactly"):
        build_gate_dispatch_log([0, 3600], GateDispatchInput(snapshots=(snapshot(1),)))
    with pytest.raises(ValueError, match="window"):
        build_gate_dispatch_log([0], GateDispatchInput(operations=(GateOperation(**{**fields, "time_s": 1}),)))
    with pytest.raises(ValueError, match="strictly increasing"):
        build_gate_dispatch_log([1, 0])


def test_default_log_distinguishes_unknown_from_zero_operations(tmp_path):
    log = build_gate_dispatch_log([0, 3600])
    _, json_path = write_gate_dispatch_log(tmp_path / "empty.nc", log)
    result = json.loads(json_path.read_text(encoding="utf-8"))
    assert result["trigger_count"] == result["reported_operation_count"] == 0
    assert all(v == 2 for v in result["indeterminate_sample_counts"].values())
    assert all(r["rule_status"] == "indeterminate" for r in result["records"])


def test_generic_result_writer_adds_companions_and_rejects_sidecar_conflicts(tmp_path):
    pytest.importorskip("netCDF4")
    field = OutputField("water_depth", [[[1]], [[2]]], 1, 1, 30, 30, 0, 0, times=[0, 3600])
    path = tmp_path / "generic.nc"
    csv_path, json_path = gate_dispatch_paths(path)
    csv_path.write_text("existing")
    with pytest.raises(FileExistsError, match="gate dispatch"):
        NetCDFWriter.write(SimulationOutput([field]), path)
    assert not path.exists() and csv_path.read_text() == "existing"
    NetCDFWriter.write(SimulationOutput([field]), path, overwrite=True)
    result = json.loads(json_path.read_text(encoding="utf-8"))
    assert result["provided_snapshot_count"] == 0
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert tuple(csv_path.read_text(encoding="utf-8-sig").splitlines()[0].split(",")) == CSV_COLUMNS
    assert validate_gate_dispatch_exports(path)["status"] == "consistent"


def test_hourly_export_records_operations_and_leaves_numeric_results_identical(tmp_path):
    netcdf = pytest.importorskip("netCDF4")
    levels = tuple(snapshot(t, sixth_port_level=water(v)) for t, v in ((0, 4.0), (3600, 4.3), (7200, 4.5)))
    op = GateOperation(time_s=3600, gate_id="duliu_inlet", rule_id="GDR-001", action="test operation",
                       reason="test fixture", source="test controller", opening_after_ratio=.8)
    dispatch = GateDispatchInput(snapshots=levels, operations=(op,))
    for name, data in (("baseline", None), ("logged", dispatch)):
        solver = HourlyInundationSolver(np.array([[2., 1.], [1.5, .5]]), np.ones((2, 2), dtype=bool),
                                        dx=50, dy=50, xmin=0, ymin=0)
        write_hourly_netcdf(tmp_path / f"{name}.nc", solver, times_s=[0, 3600, 7200],
                            inflow=[0, 2, 3], outflow=[0, .1, .2], rain_mm=[0, 0, 0],
                            coordinate_system="EPSG:4548", vertical_datum="1985国家高程基准",
                            extra_attrs={"event_start": "2023-08-01T02:00:00+08:00"}, gate_dispatch=data)
    with netcdf.Dataset(tmp_path / "baseline.nc") as baseline, netcdf.Dataset(tmp_path / "logged.nc") as logged:
        for name in baseline.variables:
            np.testing.assert_array_equal(baseline.variables[name][:], logged.variables[name][:])
        assert logged.gate_dispatch_csv == "logged_gate_dispatch.csv"
        assert logged.gate_dispatch_reported_operation_count == 1
    result = json.loads((tmp_path / "logged_gate_dispatch.json").read_text(encoding="utf-8"))
    triggers = [r for r in result["records"] if r["record_type"] == "trigger" and r["rule_id"] == "GDR-001"]
    assert len(triggers) == 1 and triggers[0]["occurred_at"] == "2023-08-01T03:00:00+08:00"


def test_invalid_dispatch_times_are_rejected_before_hourly_solver_changes(tmp_path):
    solver = HourlyInundationSolver(np.zeros((1, 1)), np.ones((1, 1), dtype=bool), dx=50, dy=50, xmin=0, ymin=0)
    with pytest.raises(ValueError, match="exactly"):
        write_hourly_netcdf(tmp_path / "bad.nc", solver, times_s=[0, 3600], inflow=[0, 1],
                            outflow=[0, 0], rain_mm=[0, 0], coordinate_system="local", vertical_datum="local",
                            gate_dispatch=GateDispatchInput(snapshots=(snapshot(1),)))
    assert solver.time == solver.volume == 0
    assert not (tmp_path / "bad.nc").exists()


def test_existing_netcdf_log_export_reads_only_and_uses_explicit_origin(tmp_path):
    netcdf = pytest.importorskip("netCDF4")
    path = tmp_path / "existing.nc"
    with netcdf.Dataset(path, "w") as ds:
        ds.createDimension("time", 2)
        ds.createVariable("time", "f8", ("time",))[:] = [0, 3600]
        ds.format = "2d-hydrodynamics-netcdf"
        ds.event_start = "2023-08-01T02:00:00+08:00"
        ds.outflow_note = "reconstructed total; not separate gate operations"
    before = path.read_bytes()
    _, report = export_gate_dispatch_for_netcdf(path)
    assert path.read_bytes() == before
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["time_origin"] == "2023-08-01T02:00:00+08:00"
    assert result["time_origins"]["resolution"] == "inherited_from_result_origin"
    assert result["result_context"]["outflow_note"].startswith("reconstructed")
    assert result["reported_operation_count"] == 0


def test_sidecar_directory_is_rejected_even_when_overwrite_is_enabled(tmp_path):
    path = tmp_path / "result.nc"
    gate_dispatch_paths(path)[1].mkdir()
    output = SimulationOutput([OutputField("water_depth", [[1]], 1, 1, 30, 30, 0, 0)])
    with pytest.raises(IsADirectoryError, match="must be a file"):
        NetCDFWriter.write(output, path, overwrite=True)
    assert not path.exists() and not gate_dispatch_paths(path)[0].exists()


@pytest.mark.parametrize("units,values,error", [("h", [0, 1], "time.units"), ("s", [0, None], "missing")])
def test_existing_result_import_rejects_wrong_time_units_and_masked_times(tmp_path, units, values, error):
    netcdf = pytest.importorskip("netCDF4")
    path = tmp_path / "invalid.nc"
    with netcdf.Dataset(path, "w") as ds:
        ds.createDimension("time", 2)
        var = ds.createVariable("time", "f8", ("time",))
        var.units = units
        for i, value in enumerate(values):
            if value is not None:
                var[i] = value
    with pytest.raises(ValueError, match=error):
        export_gate_dispatch_for_netcdf(path)
    assert not any(p.exists() for p in gate_dispatch_paths(path))
