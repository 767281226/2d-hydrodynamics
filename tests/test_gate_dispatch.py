import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from hydrodynamics import (
    GateDispatchInput, GateDispatchOptions, GateDispatchSnapshot, GateOperation,
    GateWaterLevel, NetCDFWriter, OutputField, SimulationOutput, build_gate_dispatch_log,
    export_gate_dispatch_for_netcdf, gate_dispatch_paths, write_gate_dispatch_log,
)
from hydrodynamics.gate_dispatch import GATE_RULES, RULE_SOURCE, RULE_SOURCE_SHA256
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
