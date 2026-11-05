"""End-to-end glue driver: config contract, input cross-checks, full run, --check and --resume."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("netCDF4"); pytest.importorskip("shapefile"); pytest.importorskip("pyproj")

from hydrodynamics import large_run_driver as driver
from hydrodynamics.frame_sink import NativeUgridNetcdfSink
from hydrodynamics.large_run_driver import (LargeRunDriverError, check_case, driver_config_json_schema, load_driver_config,
                                            parse_driver_config, prepare_case, run_large_case)
from hydrodynamics.large_run_profile import RIVER_ASSUMED_SECTIONS_NOTICE
from hydrodynamics.triangular_mesh import TriangularMesh

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "large_run_output"
X0, Y0 = 474300.0, 4300010.0
MESH_ID = "tiny-square-mesh"


# ---------------------------------------------------------------- fixtures
def _tiny_mesh(n: int = 10, dx: float = 1.0):
    nodes = [(X0 + i * dx, Y0 + j * dx) for j in range(n + 1) for i in range(n + 1)]
    faces = []
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i; b = a + 1; c = a + n + 2; d = a + n + 1
            faces += [(a, b, c), (a, c, d)]
    return nodes, faces


def _side_edges(mesh: TriangularMesh, side: str) -> list[int]:
    xs = [p[0] for p in mesh.node_xy]
    target = min(xs) if side == "west" else max(xs)
    return [i for i, ((a, b), (_, right)) in enumerate(zip(mesh.edge_nodes, mesh.edge_faces))
            if right == -1 and mesh.node_xy[a][0] == target and mesh.node_xy[b][0] == target]


def make_case(root: Path, *, name: str = "run", times=(0.0, 5.0, 10.0), checkpoint_interval_s: float | None = 5.0,
              export: bool = True, csv_times=(0.0, 5.0, 10.0, 15.0), inflow: float = 0.5, outflow: float = 0.2) -> Path:
    """Write mesh artifact, terrain npz, sections audit, forcing CSV and a driver config; return the config path."""
    root.mkdir(parents=True, exist_ok=True)
    nodes, faces = _tiny_mesh()
    face_id = [1001 + k for k in range(len(faces))]
    mesh = TriangularMesh(nodes, faces, "EPSG:4548", mesh_id=MESH_ID, face_id=face_id)
    west, east = _side_edges(mesh, "west"), _side_edges(mesh, "east")
    artifact = {"format": "triangle-domain-mesh-only-artifact-v1", "mesh_id": MESH_ID, "mesh_only": True, "coordinate_system": "EPSG:4548",
                "node_xy": [list(p) for p in nodes], "face_nodes": [list(f) for f in faces], "face_id": face_id,
                "boundary_groups": {"west": [list(mesh.edge_nodes[i]) for i in west], "east": [list(mesh.edge_nodes[i]) for i in east]},
                "geometry_tolerance_m": 1e-8, "minimum_face_area_m2": 1e-12}
    (root / "domain-mesh.json").write_text(json.dumps(artifact), encoding="utf-8")
    cx = np.asarray([p[0] for p in mesh.face_centroid]) - X0
    rng = np.random.default_rng(1)
    order = rng.permutation(len(faces))  # terrain rows are deliberately shuffled: matching is by face_id
    np.savez(root / "terrain.npz", face_id=np.asarray(face_id, dtype=np.int64)[order], elevation_m=(0.5 - 0.01 * cx)[order])
    audit = {"format": "assumed-river-sections-audit-v1", "mesh_id": MESH_ID, "crs": "EPSG:4548",
             "section_status": "ASSUMED_USER_CONFIRMED_NON_AUTHORITATIVE",
             "sections": {"west_in": {"role": "inflow", "name_zh": "西侧入流断面（假定）", "discharge_series_column": "inflow_m3s",
                                      "edge_indices": west, "edge_nodes": [list(mesh.edge_nodes[i]) for i in west], "width_m": 10.0},
                          "east_out": {"role": "outflow", "name_zh": "东侧出流断面（假定）", "discharge_series_column": "outflow_m3s",
                                       "edge_indices": east, "width_m": 10.0}}}
    (root / "sections_audit.json").write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    lines = ["time_s,inflow_m3s,outflow_m3s,extra"] + [f"{t},{inflow},{outflow},1" for t in csv_times]
    (root / "forcing.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = {"format": "large-run-driver-config-v1", "schema_version": "1.0",
              "mesh": {"path": "domain-mesh.json", "expected_sha256": None},
              "terrain": {"path": "terrain.npz", "dem_path": "dem_not_present.tif", "dem_sha256": "ab" * 32, "mapping_report_path": None,
                          "method": "synthetic per-face bed (test)", "vertical_datum": None, "vertical_unit": "m"},
              "sections": {"path": "sections_audit.json",
                           "discharge_series_status": {"west_in": "RECONSTRUCTED_NOT_MEASURED", "east_out": "COMPUTED_OUTFLOW"},
                           "distribution": {}},
              "forcing": {"path": "forcing.csv", "rainfall": None},
              "run": {"hours": None, "output_times_s": list(times), "initial_depth_m": 0.02, "manning_n": 0.03,
                      "long_run_options": {"kernel": "numpy", "progress_interval_steps": 50, "checkpoint_keep_count": 5},
                      "checkpoint_interval_s": checkpoint_interval_s, "time_origin": "2023-08-01T02:00:00+08:00",
                      "run_id": f"tiny-{name}", "case_id": "tiny_case", "coordinate_system": "EPSG:4548"},
              "output": {"directory": name, "profile": {"disk_preflight": False}, "export": export}}
    path = root / f"{name}.config.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _config_dict(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _ledger_rows(run_dir: Path) -> list[dict]:
    rows = [json.loads(line) for line in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return [{k: v for k, v in row.items() if k != "interval_wall_clock_s"} for row in rows]


# ---------------------------------------------------------------- contract
def test_schema_and_example_match_the_model():
    schema = json.loads((EXAMPLES / "large_run_driver_config.schema.json").read_text(encoding="utf-8"))
    assert schema == driver_config_json_schema()
    example = json.loads((EXAMPLES / "large_run_driver_config.example.json").read_text(encoding="utf-8"))
    config = parse_driver_config(example)
    assert config.run.hours == 288 and config.run.times_s()[-1] == 288 * 3600.0
    assert config.run.options().checkpoint_interval_s == 21600.0
    assert config.sections.discharge_series_status["A"] == "RECONSTRUCTED_NOT_MEASURED"
    assert config.forcing.rainfall is None
    for section in ("mesh", "terrain", "sections", "forcing", "run", "output"):
        model = getattr(driver, {"mesh": "MeshSection", "terrain": "TerrainSection", "sections": "SectionsSection",
                                 "forcing": "ForcingSection", "run": "RunSection", "output": "OutputSection"}[section])
        assert set(schema["$defs"][model.__name__]["properties"]) == set(model.model_fields)


def test_inline_profile_socio_paths_resolve_against_the_config_file(tmp_path):
    example = json.loads((EXAMPLES / "large_run_driver_config.example.json").read_text(encoding="utf-8"))
    example["output"]["profile"] = {"socio_exposure": {"grid_cells_path": "socio/grid.shp", "district_totals_csv": "socio/t.csv",
                                                       "district_grid_summary_csv": None, "mapping_cache_dir": "cache"}}
    (tmp_path / "cfg").mkdir()
    path = tmp_path / "cfg" / "c.json"
    path.write_text(json.dumps(example), encoding="utf-8")
    socio = load_driver_config(path, script_id="t").output.profile["socio_exposure"]
    assert Path(socio["grid_cells_path"]) == (tmp_path / "cfg" / "socio" / "grid.shp").resolve()
    assert Path(socio["district_totals_csv"]) == (tmp_path / "cfg" / "socio" / "t.csv").resolve()
    assert Path(socio["mapping_cache_dir"]) == (tmp_path / "cfg" / "cache").resolve()
    assert socio["district_grid_summary_csv"] is None


def test_in_repo_qingbei_case_configs_use_only_relative_paths():
    for path in sorted((ROOT / "cases" / "qingbei" / "configs").glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert ":/" not in text and ":\\\\" not in text, path.name
        config = load_driver_config(path, script_id="t")
        assert Path(config.mesh.path).is_relative_to(ROOT) and Path(config.forcing.path).is_relative_to(ROOT)


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c.pop("mesh"), "mesh: Field required"),
    (lambda c: c.pop("terrain"), "terrain: Field required"),
    (lambda c: c.pop("sections"), "sections: Field required"),
    (lambda c: c.pop("forcing"), "forcing: Field required"),
    (lambda c: c.pop("run"), "run: Field required"),
    (lambda c: c.pop("output"), "output: Field required"),
    (lambda c: c["terrain"].pop("method"), "terrain.method: Field required"),
    (lambda c: c["terrain"].pop("dem_sha256"), "terrain.dem_sha256: Field required"),
    (lambda c: c["sections"].pop("discharge_series_status"), "sections.discharge_series_status: Field required"),
    (lambda c: c["run"].pop("initial_depth_m"), "run.initial_depth_m: Field required"),
    (lambda c: c["run"].pop("manning_n"), "run.manning_n: Field required"),
    (lambda c: c["run"].pop("time_origin"), "run.time_origin: Field required"),
    (lambda c: c["run"].pop("coordinate_system"), "run.coordinate_system: Field required"),
    (lambda c: c["run"].update(hours=0, output_times_s=None), "greater than or equal to 1"),
    (lambda c: c["run"].update(hours=9000, output_times_s=None), "less than or equal to 8760"),
    (lambda c: c["run"].update(hours=1), "exactly one of hours or output_times_s"),
    (lambda c: c["run"].update(hours=None, output_times_s=None), "exactly one of hours or output_times_s"),
    (lambda c: c["run"].update(output_times_s=[5.0, 10.0]), "start at 0"),
    (lambda c: c["run"].update(initial_depth_m=-1.0), "greater than or equal to 0"),
    (lambda c: c["run"].update(manning_n=1.5), "less than or equal to 1"),
    (lambda c: c["run"].update(manning_n=0), "greater than 0"),
    (lambda c: c["run"].update(manning_n="0.03"), "number"),
    (lambda c: c["run"].update(checkpoint_interval_s=0), "greater than 0"),
    (lambda c: c["run"].update(time_origin="2023-08-01T02:00:00"), "UTC offset"),
    (lambda c: c["run"].update(run_id="bad id"), "match pattern"),
    (lambda c: c["run"].update(case_id="result_native"), "result_native"),
    (lambda c: c["run"]["long_run_options"].update(checkpoint_interval_s=10.0), "use run.checkpoint_interval_s"),
    (lambda c: c["run"]["long_run_options"].update(cfl_number=2.0), "long_run_options"),
    (lambda c: c["run"]["long_run_options"].update(manning_friction="none"), "manning_friction"),
    (lambda c: c["forcing"].update(rainfall={"column": "rainfall_mm_per_step"}), "forcing.rainfall"),
    (lambda c: c["sections"]["discharge_series_status"].update(west_in="GUESSED"), "discharge_series_status"),
    (lambda c: c["sections"].update(distribution={"west_in": "uniform"}), "distribution"),
    (lambda c: c["sections"].update(distribution={"a__b": "length"}), "without '__'"),
    (lambda c: c["terrain"].update(dem_sha256="abc"), "match pattern"),
    (lambda c: c["terrain"].update(method=""), "at least 1 character"),
    (lambda c: c["terrain"].update(vertical_unit="ft"), "vertical_unit"),
    (lambda c: c["mesh"].update(expected_sha256="xyz"), "match pattern"),
    (lambda c: c["output"].update(export="yes"), "boolean"),
    (lambda c: c.update(unknown_field=1), "Extra inputs"),
    (lambda c: c.update(format="large-run-driver-config-v2"), "format"),
    (lambda c: c["sections"].update(outflow_boundary={"east_out": {"boundary_type": "rating_curve"}}), "boundary_type"),
    (lambda c: c["sections"].update(outflow_boundary={"east_out": {"discharge_coefficient": 0.5}}), "only valid with boundary_type free_outflow"),
    (lambda c: c["sections"].update(outflow_boundary={"east_out": {"boundary_type": "free_outflow", "discharge_coefficient": 0}}), "greater than 0"),
    (lambda c: c["sections"].update(outflow_boundary={"east_out": {"boundary_type": "free_outflow", "discharge_coefficient": 1.6}}), "less than or equal to 1.5"),
    (lambda c: c["sections"].update(outflow_boundary={"east_out": {"boundary_type": "free_outflow", "crest_elevation_m": "1"}}), "crest_elevation_m"),
    (lambda c: c["sections"].update(outflow_boundary={"east_out": {"boundary_type": "free_outflow", "crest_elevation_m": 1e5}}), "less than or equal to 10000"),
    (lambda c: c["sections"].update(outflow_boundary={"east_out": {"boundary_type": "free_outflow", "weir": 1}}), "Extra inputs"),
    (lambda c: c["sections"].update(outflow_boundary={"a__b": {}}), "without '__'"),
])
def test_config_validation_errors(tmp_path, mutate, message):
    config = _config_dict(make_case(tmp_path))
    mutate(config)
    with pytest.raises(LargeRunDriverError, match=message):
        parse_driver_config(config)


def test_relative_paths_resolve_against_the_config_directory(tmp_path):
    path = make_case(tmp_path)
    config = load_driver_config(path)
    assert Path(config.mesh.path) == (tmp_path / "domain-mesh.json").resolve()
    assert Path(config.output.directory) == (tmp_path / "run").resolve()
    assert config.terrain.mapping_report_path is None


# ------------------------------------------------------------ input checks
def _prepare(path: Path):
    config = load_driver_config(path)
    return prepare_case(config, driver._sha_file(path), build_mesh=False)


def test_input_cross_checks(tmp_path):
    path = make_case(tmp_path)
    case = _prepare(path)
    assert case.mesh_info["n_face"] == 200 and case.mesh_info["n_exterior_edge"] == 40
    assert [g["name"] for g in case.groups] == ["west_in", "east_out"]
    assert [g["distribution"] for g in case.groups] == ["length", "conveyance_depth_5_3"]
    assert case.metadata.forcing.notice == RIVER_ASSUMED_SECTIONS_NOTICE and case.metadata.live_solver_ran
    assert case.metadata.vertical_datum is None and case.metadata.data_origin == "LIVE_SOLVER_RUN"
    assert case.metadata.solver["terrain"]["dem_opened_by_driver"] is False
    assert set(case.metadata.source_hashes) == {"driver_config", "mesh_artifact", "sections_audit", "forcing_csv", "dem_as_supplied", "terrain_npz"}

    terrain = np.load(tmp_path / "terrain.npz")
    ids, elev = terrain["face_id"], terrain["elevation_m"]
    np.savez(tmp_path / "terrain.npz", face_id=ids[1:], elevation_m=elev[1:])
    with pytest.raises(LargeRunDriverError, match="lacks elevation for 1 mesh faces"):
        _prepare(path)
    np.savez(tmp_path / "terrain.npz", face_id=np.concatenate([ids, ids[:1]]), elevation_m=np.concatenate([elev, elev[:1]]))
    with pytest.raises(LargeRunDriverError, match="duplicate"):
        _prepare(path)
    np.savez(tmp_path / "terrain.npz", face_id=np.concatenate([ids, [99]]), elevation_m=np.concatenate([elev, [1.0]]))
    with pytest.raises(LargeRunDriverError, match="not mesh faces"):
        _prepare(path)
    bad = elev.copy(); bad[3] = np.nan
    np.savez(tmp_path / "terrain.npz", face_id=ids, elevation_m=bad)
    with pytest.raises(LargeRunDriverError, match="non-finite"):
        _prepare(path)
    np.savez(tmp_path / "terrain.npz", face_id=ids, elevation_m=elev)
    _prepare(path)

    csv_path = tmp_path / "forcing.csv"
    original = csv_path.read_text(encoding="utf-8")
    csv_path.write_text("time_s,inflow_m3s,outflow_m3s\n0,0.5,0.2\n8,0.5,0.2\n", encoding="utf-8")
    with pytest.raises(LargeRunDriverError, match=r"covers \[0.0, 8.0\] s but the run needs \[0, 10.0\]"):
        _prepare(path)
    csv_path.write_text("time_s,inflow_m3s,outflow_m3s\n0,0.5,-0.2\n15,0.5,0.2\n", encoding="utf-8")
    with pytest.raises(LargeRunDriverError, match="negative"):
        _prepare(path)
    csv_path.write_text("time_s,inflow_m3s\n0,0.5\n15,0.5\n", encoding="utf-8")
    with pytest.raises(LargeRunDriverError, match=r"lacks series columns \['outflow_m3s'\]"):
        _prepare(path)
    csv_path.write_text("time_s,inflow_m3s,outflow_m3s\n0,0.5,0.2\n0,0.5,0.2\n15,0.5,0.2\n", encoding="utf-8")
    with pytest.raises(LargeRunDriverError, match="strictly increasing"):
        _prepare(path)
    csv_path.write_text(original, encoding="utf-8")

    audit_path = tmp_path / "sections_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    _rewrite(audit_path, {**audit, "mesh_id": "other-mesh"})
    with pytest.raises(LargeRunDriverError, match="mesh_id"):
        _prepare(path)
    interior = copy.deepcopy(audit); interior["sections"]["east_out"]["edge_indices"][0] = 2  # sorted pair (0, 12): the first cell diagonal
    _rewrite(audit_path, interior)
    with pytest.raises(LargeRunDriverError, match="interior"):
        _prepare(path)
    swapped = copy.deepcopy(audit); swapped["sections"]["west_in"]["edge_nodes"][0] = [0, 1]
    _rewrite(audit_path, swapped)
    with pytest.raises(LargeRunDriverError, match="edge_nodes do not match"):
        _prepare(path)
    _rewrite(audit_path, audit)

    config = _config_dict(path)
    config["run"]["coordinate_system"] = "EPSG:32650"
    with pytest.raises(LargeRunDriverError, match="coordinate_system"):
        _prepare(_rewrite(path, config))
    config["run"]["coordinate_system"] = "EPSG:4548"
    config["mesh"]["expected_sha256"] = "0" * 64
    with pytest.raises(LargeRunDriverError, match="sha256"):
        _prepare(_rewrite(path, config))
    config["mesh"]["expected_sha256"] = None
    config["sections"]["discharge_series_status"] = {"west_in": "RECONSTRUCTED_NOT_MEASURED"}
    with pytest.raises(LargeRunDriverError, match=r"missing \['east_out'\]"):
        _prepare(_rewrite(path, config))
    config["sections"]["discharge_series_status"] = {"west_in": "RECONSTRUCTED_NOT_MEASURED", "east_out": "COMPUTED_OUTFLOW", "ghost": "MEASURED"}
    with pytest.raises(LargeRunDriverError, match=r"unknown sections \['ghost'\]"):
        _prepare(_rewrite(path, config))


def test_check_reports_missing_terrain_as_the_only_missing_item(tmp_path):
    path = make_case(tmp_path)
    terrain = tmp_path / "terrain.npz"
    terrain.rename(tmp_path / "terrain.absent.npz")
    report = check_case(path)
    assert report["status"] == "missing_inputs" and report["errors"] == []
    assert report["missing"] == [str(terrain.resolve())]
    assert report["inputs"]["mesh"]["face_count"] == 200 and report["inputs"]["sections"]["edges_exterior_and_matching"] is True
    assert report["inputs"]["forcing"]["rows"] == 4 and report["run"]["frames"] == 3
    assert report["run"]["forcing_label"]["scope"] == "RIVER_ONLY"
    assert report["environment"]["crs"]["projected"] is True
    (tmp_path / "terrain.absent.npz").rename(terrain)
    report = check_case(path)
    assert report["status"] == "ok" and report["missing"] == [] and report["inputs"]["terrain"]["present"] is True
    assert driver.main([str(path), "--check"]) == 0
    (tmp_path / "run").mkdir()
    report = check_case(path)
    assert report["status"] == "invalid" and any("exists" in e for e in report["errors"])
    assert driver.main([str(path), "--check"]) == 1


# ---------------------------------------------------------------- full run
def test_full_run_streams_exports_and_verifies(tmp_path):
    import netCDF4
    path = make_case(tmp_path)
    report = run_large_case(path)
    run_dir = tmp_path / "run"; bundle = run_dir / "bundle"
    assert report["status"] == "complete" and report["export"]["status"] == "complete" and report["verify"]["status"] == "PASS"
    assert report["frames_expected"] == 3 and report["frames_written_this_process"] == 3
    on_disk = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "complete" and on_disk["checkpoints_kept"] == ["checkpoint_00000001.npz", "checkpoint_00000002.npz"]
    receipt = json.loads((run_dir / "run_receipt.json").read_text(encoding="utf-8"))
    assert receipt["solver"]["kernel"] == "numpy" and receipt["solver"]["accepted_step_count"] > 0
    assert receipt["versions"]["python"] and receipt["versions"]["numpy"] and "numba_kernel_status" in receipt["versions"]
    assert receipt["sink"]["frame_count"] == 3 and receipt["export"]["file_count"] > 20 and receipt["verify"]["status"] == "PASS"
    assert {"mesh_artifact", "terrain_npz", "sections_audit", "forcing_csv", "driver_config"} <= set(receipt["source_hashes"])
    assert receipt["timings_s"]["mesh_build_s"] >= 0 and receipt["timings_s"]["solver_s"] > 0
    rows = _ledger_rows(run_dir)
    assert [row["output_index"] for row in rows] == [0, 1, 2]
    assert rows[-1]["cumulative_inflow_volume_m3"] == pytest.approx(0.5 * 10.0, rel=1e-9)
    assert abs(rows[-1]["residual_m3"]) < 1e-9
    events = [json.loads(line)["event"] for line in (run_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0] == "start" and events[-1] == "complete" and events.count("frame_written") == 3 and "solver_progress" in events
    assert (run_dir / "config.resolved.json").is_file()
    with netCDF4.Dataset(str(bundle / "result_native.nc")) as ds:
        assert ds.status == "COMPLETE" and int(ds.frames_written) == 3 and int(ds.live_solver_ran) == 1 and ds.data_origin == "LIVE_SOLVER_RUN"
        assert ds.forcing_scope == "RIVER_ONLY" and ds.forcing_notice == RIVER_ASSUMED_SECTIONS_NOTICE
        assert ds.vertical_datum == "UNSPECIFIED_NATIVE_SOURCE_DATUM"
        assert "mass_budget_groups__west_in__cumulative_inflow_volume_m3" in ds.variables
        assert "mass_budget_interval_outflow_delivered_volume_m3" in ds.variables and "water_level" in ds.variables
        inflow_vol = np.asarray(ds["mass_budget_interval_inflow_volume_m3"][:])
        assert np.allclose(inflow_vol, [0.0, 2.5, 2.5])
        assert ds.getncattr("ledger_cumulative_inflow_volume_m3") == pytest.approx(5.0, rel=1e-9)
        assert json.loads(str(ds["mass_ledger_json"][...]))["kernel"] == "numpy"
        assert "solver_max_water_depth_m" in ds.variables
        meta = json.loads(ds.run_metadata_json)
        assert meta["solver"]["name"] == "hydrodynamics.triangular_long_run" and meta["solver"]["terrain"]["method"].startswith("synthetic")
    with netCDF4.Dataset(str(bundle / "tiny_case.nc")) as ds:
        assert np.allclose(np.asarray(ds["inflow"][:]), [0.0, 0.5, 0.5]) and np.allclose(np.asarray(ds["rainfall"][:]), 0.0)
        assert ds["inflow"].availability.endswith("ASSUMED_SECTIONS_RECONSTRUCTED_Q") and ds["rainfall"].availability == "ZERO_NO_RAINFALL_IN_RUN"
        assert ds["water_level"].availability == "NOT_AVAILABLE_DATUM_UNSPECIFIED"
    run = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "COMPLETE_NOT_PRODUCTION_APPROVED" and run["live_solver_ran"] is True
    assert run["forcing"]["boundary_groups"][0]["group_id"] == "west_in"
    with pytest.raises(LargeRunDriverError, match="exists"):
        run_large_case(path)
    with pytest.raises(LargeRunDriverError, match="already complete"):
        run_large_case(path, resume=True)


def _free_outflow_case(root: Path, *, name: str = "run", crest=None, coefficient=None, **kwargs) -> Path:
    path = make_case(root, name=name, **kwargs)
    config = _config_dict(path)
    settings = {"boundary_type": "free_outflow", "crest_elevation_m": crest}
    if coefficient is not None:
        settings["discharge_coefficient"] = coefficient
    config["sections"]["outflow_boundary"] = {"east_out": settings}
    return _rewrite(path, config)


def test_free_outflow_section_config_builds_reference_only_groups(tmp_path):
    path = _free_outflow_case(tmp_path, crest=0.3, coefficient=0.5)
    case = _prepare(path)
    inflow, free = case.groups
    assert inflow["role"] == "inflow" and inflow["time_s"] == [0.0, 5.0, 10.0, 15.0]
    assert free == {"name": "east_out", "role": "free_outflow", "edge_indices": free["edge_indices"],
                    "reference_time_s": [0.0, 5.0, 10.0, 15.0], "reference_discharge_m3_s": [0.2] * 4,
                    "discharge_coefficient": 0.5, "crest_elevation_m": 0.3}
    groups = case.metadata.solver["sections"]["groups"]
    assert groups["east_out"]["boundary_type"] == "free_outflow" and groups["east_out"]["discharge_series_use"] == "reference_only"
    assert groups["east_out"]["distribution"] is None and groups["east_out"]["crest_elevation_m"] == 0.3
    assert "boundary_type" not in groups["west_in"]  # prescribed metadata unchanged (keeps old partial results resumable)
    assert {g.group_id: g.role for g in case.metadata.forcing.boundary_groups} == {"west_in": "inflow", "east_out": "outflow"}
    report = check_case(path)
    assert report["status"] == "ok", report["errors"]
    section = report["inputs"]["sections"]["sections"]["east_out"]
    assert section["boundary_type"] == "free_outflow" and section["discharge_coefficient"] == 0.5 and section["crest"] == "fixed_elevation"
    assert report["inputs"]["sections"]["sections"]["west_in"]["boundary_type"] == "inflow"

    config = _config_dict(path)
    config["sections"]["distribution"] = {"east_out": "length"}
    with pytest.raises(LargeRunDriverError, match="distribution does not apply to a free_outflow section"):
        _prepare(_rewrite(path, config))
    assert any("distribution does not apply" in e for e in check_case(path)["errors"])
    config["sections"]["distribution"] = {}
    config["sections"]["outflow_boundary"] = {"west_in": {"boundary_type": "free_outflow"}}
    with pytest.raises(LargeRunDriverError, match="west_in is an inflow section"):
        _prepare(_rewrite(path, config))
    config["sections"]["outflow_boundary"] = {"ghost": {"boundary_type": "free_outflow"}}
    with pytest.raises(LargeRunDriverError, match=r"outflow_boundary names unknown sections \['ghost'\]"):
        _prepare(_rewrite(path, config))


def test_full_run_with_free_outflow_exports_and_verifies(tmp_path):
    import netCDF4
    path = _free_outflow_case(tmp_path)
    report = run_large_case(path)
    assert report["status"] == "complete" and report["verify"]["status"] == "PASS"
    rows = _ledger_rows(tmp_path / "run")
    final = rows[-1]
    group = final["groups"]["east_out"]
    assert group["cumulative_outflow_delivered_volume_m3"] > 0.0 and group["outflow_discharge_m3_s"] > 0.0
    assert group["cumulative_reference_outflow_volume_m3"] == pytest.approx(0.2 * 10.0, rel=1e-12)
    assert abs(final["residual_m3"]) < 1e-9
    with netCDF4.Dataset(str(tmp_path / "run" / "bundle" / "result_native.nc")) as ds:
        assert "mass_budget_groups__east_out__outflow_discharge_m3_s" in ds.variables
        assert ds["mass_budget_groups__east_out__outflow_discharge_m3_s"].units == "m3 s-1"
        close = json.loads(str(ds["mass_ledger_json"][...]))
        assert close["groups"]["east_out"]["role"] == "free_outflow"
        assert close["groups"]["east_out"]["cumulative_reference_outflow_volume_m3"] == pytest.approx(2.0, rel=1e-12)


def test_failed_input_run_writes_failed_report_and_keeps_nothing_else(tmp_path):
    path = make_case(tmp_path)
    (tmp_path / "forcing.csv").write_text("time_s,inflow_m3s,outflow_m3s\n0,0.5,0.2\n8,0.5,0.2\n", encoding="utf-8")
    with pytest.raises(LargeRunDriverError, match="covers"):
        run_large_case(path)
    report = json.loads((tmp_path / "run" / "run_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed" and report["error"]["type"] == "LargeRunDriverError" and "covers" in report["error"]["message"]
    assert not (tmp_path / "run" / "bundle" / "result_native.nc").exists()
    assert driver.main([str(path), "--resume", "--quiet"]) == 1  # no checkpoint: cannot resume


# ------------------------------------------------------------------ resume
def test_resume_after_crash_matches_uninterrupted_run(tmp_path, monkeypatch):
    import netCDF4
    reference = make_case(tmp_path / "ref", name="ref", export=False)
    run_large_case(reference)
    crashed = make_case(tmp_path / "crash", name="crash", export=True)
    original = NativeUgridNetcdfSink.write_frame

    def crashing_write(self, index, time_s, arrays, ledger=None):
        if int(index) == 2:
            raise RuntimeError("simulated crash inside the frame sink after frame 1")
        return original(self, index, time_s, arrays, ledger)

    monkeypatch.setattr(NativeUgridNetcdfSink, "write_frame", crashing_write)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_large_case(crashed)
    run_dir = tmp_path / "crash" / "crash"; bundle = run_dir / "bundle"
    failed = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed" and failed["error"]["type"] == "RuntimeError"
    assert failed["checkpoints_kept"] == ["checkpoint_00000001.npz"] and failed["frames_written_this_process"] == 2
    assert (bundle / "result_native.nc.failed").is_file() and not (bundle / "result_native.nc").exists()
    assert len(_ledger_rows(run_dir)) == 2
    monkeypatch.setattr(NativeUgridNetcdfSink, "write_frame", original)
    with pytest.raises(LargeRunDriverError, match="exists"):
        run_large_case(crashed)
    report = run_large_case(crashed, resume=True)
    assert report["status"] == "complete" and report["resume"] is True and report["resume_count"] == 1
    assert report["frames_present_before"] == 2 and report["frames_written_this_process"] == 1
    assert report["resumed_from_checkpoint"].endswith("checkpoint_00000001.npz") and report["verify"]["status"] == "PASS"
    receipt = json.loads((run_dir / "run_receipt.json").read_text(encoding="utf-8"))
    assert receipt["resume"]["frames_present_in_partial"] == 2 and receipt["solver"]["resumed_from"].endswith("checkpoint_00000001.npz")
    assert _ledger_rows(run_dir) == _ledger_rows(tmp_path / "ref" / "ref")
    with netCDF4.Dataset(str(bundle / "result_native.nc")) as resumed, netCDF4.Dataset(str(tmp_path / "ref" / "ref" / "bundle" / "result_native.nc")) as ref:
        assert resumed.status == "COMPLETE" and int(resumed.frames_written) == 3
        history = json.loads(resumed.resume_history_json)
        assert len(history) == 1 and history[0]["frames_present"] == 2 and history[0]["previous_status"] == "FAILED"
        for name in ("water_depth", "momentum_x", "momentum_y", "water_level", "time", "saved_frame_max_water_depth", "saved_frame_max_speed",
                     "saved_frame_arrival_time", "solver_max_water_depth_m"):
            assert np.array_equal(np.asarray(resumed[name][:]), np.asarray(ref[name][:]), equal_nan=True), name
        for name in ("mass_budget_interval_inflow_volume_m3", "mass_budget_groups__east_out__cumulative_outflow_delivered_volume_m3"):
            assert np.array_equal(np.asarray(resumed[name][:]), np.asarray(ref[name][:])), name
        assert json.loads(str(resumed["mass_ledger_json"][...])) == json.loads(str(ref["mass_ledger_json"][...]))
    with pytest.raises(LargeRunDriverError, match="already complete"):
        run_large_case(crashed, resume=True)


def test_resume_without_checkpoint_is_refused(tmp_path, monkeypatch):
    path = make_case(tmp_path, checkpoint_interval_s=None)
    original = NativeUgridNetcdfSink.write_frame

    def crashing_write(self, index, time_s, arrays, ledger=None):
        if int(index) == 1:
            raise RuntimeError("crash")
        return original(self, index, time_s, arrays, ledger)

    monkeypatch.setattr(NativeUgridNetcdfSink, "write_frame", crashing_write)
    with pytest.raises(RuntimeError):
        run_large_case(path)
    monkeypatch.setattr(NativeUgridNetcdfSink, "write_frame", original)
    with pytest.raises(LargeRunDriverError, match="requires run.checkpoint_interval_s"):
        run_large_case(path, resume=True)


# ---------------------------------------------------------------- branch from a parent checkpoint
def _forcing_after_branch(root: Path, rows) -> None:
    lines = ["time_s,inflow_m3s,outflow_m3s,extra"] + [f"{t},{q_in},{q_out},1" for t, q_in, q_out in rows]
    (root / "forcing.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


CHANGED_AFTER_5S = [(0.0, 0.5, 0.2), (5.0, 0.5, 0.2), (10.0, 0.5, 0.45), (15.0, 0.5, 0.45)]


def test_branch_from_checkpoint_matches_a_fresh_run_with_the_corrected_forcing(tmp_path):
    import netCDF4
    parent = make_case(tmp_path / "parent", name="parent", export=False)
    assert run_large_case(parent)["status"] == "complete"
    # reference: a fresh full run whose outflow changes only after t = 5 s
    reference = make_case(tmp_path / "ref", name="ref", export=False)
    _forcing_after_branch(tmp_path / "ref", CHANGED_AFTER_5S)
    run_large_case(reference)
    # branch: same corrected forcing, continued from the parent's t = 5 s checkpoint
    child = make_case(tmp_path / "child", name="child", export=True)
    _forcing_after_branch(tmp_path / "child", CHANGED_AFTER_5S)
    checkpoint = tmp_path / "parent" / "parent" / "checkpoints" / "checkpoint_00000001.npz"
    report = run_large_case(child, branch_from=checkpoint)
    assert report["status"] == "complete" and report["verify"]["status"] == "PASS"
    branch = report["branch"]
    assert branch["branch_time_s"] == 5.0 and branch["checkpoint_output_index"] == 1
    assert branch["changed_series"] == [{"group": "east_out", "series": "discharge_m3_s", "first_differing_sample_time_s": 10.0,
                                         "differing_sample_count": 2}]
    assert report["frames_present_before"] == 2 and report["frames_written_this_process"] == 1
    run_dir = tmp_path / "child" / "child"
    assert _ledger_rows(run_dir) == _ledger_rows(tmp_path / "ref" / "ref")
    with netCDF4.Dataset(str(run_dir / "bundle" / "result_native.nc")) as got, \
            netCDF4.Dataset(str(tmp_path / "ref" / "ref" / "bundle" / "result_native.nc")) as ref:
        for name in ("water_depth", "momentum_x", "momentum_y", "water_level", "time", "saved_frame_max_water_depth",
                     "saved_frame_max_speed", "saved_frame_arrival_time", "solver_max_water_depth_m",
                     "mass_budget_groups__east_out__cumulative_outflow_delivered_volume_m3"):
            assert np.array_equal(np.asarray(got[name][:]), np.asarray(ref[name][:]), equal_nan=True), name
        assert json.loads(str(got["mass_ledger_json"][...])) == json.loads(str(ref["mass_ledger_json"][...]))
    with netCDF4.Dataset(str(tmp_path / "parent" / "parent" / "bundle" / "result_native.nc")) as par, \
            netCDF4.Dataset(str(tmp_path / "ref" / "ref" / "bundle" / "result_native.nc")) as ref:
        assert not np.array_equal(np.asarray(par["water_depth"][2]), np.asarray(ref["water_depth"][2]))  # the change mattered


def test_branch_refuses_changes_before_the_branch_time_and_non_series_changes(tmp_path):
    parent = make_case(tmp_path / "parent", name="parent", export=False)
    run_large_case(parent)
    checkpoint = tmp_path / "parent" / "parent" / "checkpoints" / "checkpoint_00000001.npz"
    early = make_case(tmp_path / "early", name="early", export=False)
    _forcing_after_branch(tmp_path / "early", [(0.0, 0.5, 0.2), (5.0, 0.5, 0.3), (10.0, 0.5, 0.3), (15.0, 0.5, 0.3)])
    with pytest.raises(LargeRunDriverError, match="differs before the branch time"):
        run_large_case(early, branch_from=checkpoint)
    same = make_case(tmp_path / "same", name="same", export=False)
    with pytest.raises(LargeRunDriverError, match="identical to the parent"):
        run_large_case(same, branch_from=checkpoint)
    manning = make_case(tmp_path / "manning", name="manning", export=False)
    _forcing_after_branch(tmp_path / "manning", CHANGED_AFTER_5S)
    data = _config_dict(manning); data["run"]["manning_n"] = 0.04
    _rewrite(manning, data)
    with pytest.raises(LargeRunDriverError, match="only boundary discharge series"):
        run_large_case(manning, branch_from=checkpoint)
    with pytest.raises(LargeRunDriverError, match="cannot be combined with --resume"):
        run_large_case(same, branch_from=checkpoint, resume=True)


def test_branch_needs_a_complete_parent(tmp_path, monkeypatch):
    parent = make_case(tmp_path / "parent", name="parent", export=False)
    original = NativeUgridNetcdfSink.write_frame

    def crashing_write(self, index, time_s, arrays, ledger=None):
        if int(index) == 2:
            raise RuntimeError("crash")
        return original(self, index, time_s, arrays, ledger)

    monkeypatch.setattr(NativeUgridNetcdfSink, "write_frame", crashing_write)
    with pytest.raises(RuntimeError):
        run_large_case(parent)
    monkeypatch.setattr(NativeUgridNetcdfSink, "write_frame", original)
    child = make_case(tmp_path / "child", name="child", export=False)
    _forcing_after_branch(tmp_path / "child", CHANGED_AFTER_5S)
    with pytest.raises(LargeRunDriverError, match="COMPLETE parent"):
        run_large_case(child, branch_from=tmp_path / "parent" / "parent" / "checkpoints" / "checkpoint_00000001.npz")
