from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import netCDF4
import pytest
import yaml

from hydrodynamics.gate_dispatch import (
    GateDispatchInput,
    build_gate_dispatch_log,
    export_gate_dispatch_for_netcdf,
    gate_dispatch_paths,
    write_gate_dispatch_log,
)
from hydrodynamics.mesh_preparation import write_ugrid_preparation
from hydrodynamics.rehearsal_contract import (
    ProductCoverage,
    ResultProductSelector,
    ResultSetManifest,
    ScopeDescriptor,
    validate_selector_against_manifest,
)
from hydrodynamics.rehearsal_package import (
    PackageFileRecord,
    RehearsalPackageError,
    R2AExportRequest,
    _load_r2a_profile,
    create_local_execution_envelope,
    export_result_package,
    main,
    validate_result_package,
    validate_result_package,
)
from hydrodynamics.triangular_business import (
    TriangularBusinessPackage,
    TriangularInitialConditionField,
    TriangularRoughnessField,
    write_triangular_business_package,
)
from hydrodynamics.triangular_mesh import FaceTerrainField, TriangularMesh
from hydrodynamics.triangular_solver import run_triangular_solver
from hydrodynamics.triangular_solver_contract import validate_triangular_run_request


def _json_write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_case(root: Path, *, with_g1: bool = False, request_products: list[dict] | None = None, request_name: str = "request.yaml") -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    profile = _load_r2a_profile().profile
    mesh = TriangularMesh(
        node_xy=profile.mesh.node_xy_m, face_nodes=profile.mesh.face_nodes,
        coordinate_system=profile.mesh.coordinate_system, mesh_id=profile.mesh.mesh_id,
        face_id=profile.mesh.face_id, boundary_groups=profile.mesh.boundary_groups,
        constraint_groups=profile.mesh.constraint_groups,
    )
    terrain = FaceTerrainField(
        mesh_id=mesh.mesh_id or "", elevation_m=tuple(profile.terrain.elevation_m),
        valid_mask=(True,) * mesh.n_face, covered_area_m2=tuple(mesh.face_area),
        valid_area_m2=tuple(mesh.face_area), coverage_ratio=(1.0,) * mesh.n_face,
        coordinate_system=mesh.coordinate_system, vertical_datum=profile.terrain.vertical_datum,
        vertical_datum_source=profile.terrain.vertical_datum_source,
    )
    mesh_path = root / "mesh.nc"
    write_ugrid_preparation(mesh_path, mesh, terrain)
    initial = TriangularInitialConditionField.from_assignments(
        mesh, terrain, "depth", tuple(zip(mesh.face_id, profile.initial_condition.values_m)),
        source="R2A test fixture static reference",
    )
    business = TriangularBusinessPackage(
        mesh, terrain,
        TriangularRoughnessField.from_constant(mesh, float(profile.roughness_manning_n), source="test fixture provenance only"),
        initial, (),
    )
    business_path = root / "business.json"
    write_triangular_business_package(business_path, business, mesh_artifact=mesh_path)
    config_path = root / "run.yaml"
    config_path.write_text(yaml.safe_dump({
        "format": "triangular-solver-run-v1", "schema_version": "1.0",
        "case_id": profile.model_case_id, "business_file": business_path.name,
        "result_file": "result.nc", "time_origin": None, "output_times_s": [0.0, 0.5, 1.0],
        "boundary_policy": "closed_wall", "initial_momentum": "zero", "sources": "none",
        "maximum_face_count": 64, "maximum_time_step_s": 0.1,
    }, sort_keys=False), encoding="utf-8")
    assignment = {
        "format": "rehearsal-local-dispatch-assignment-v1", "assignment_basis": "LOCAL_SYNTHETIC_TEST_CONTEXT",
        "tenant_id": "tenant-local-r2a", "project_id": "project-local-r2a",
        "scenario_id": "scenario-local-r2a", "scenario_version_id": "scenario-version-local-r2a",
        "platform_run_id": "platform-run-local-r2a", "attempt_id": "attempt-local-r2a-1", "attempt_no": 1,
        "script_id": "20261019-1792400000000000000", "local_run_id": "20261019-1792400000000000000",
        "model_case_id": profile.model_case_id, "scope_id": profile.scope_id,
    }
    assignment_path = root / "assignment.json"
    _json_write(assignment_path, assignment)
    request_path = root / request_name
    request_path.write_text(yaml.safe_dump({
        "format": "rehearsal-result-package-export-request-v1", "schema_version": "1.0",
        "run_config_file": config_path.name, "dispatch_assignment_file": assignment_path.name,
        "execution_envelope_file": "envelope.json", "output_directory": "package",
        "scope_id": profile.scope_id, "metric_policy_version": "r2a-static-flat-bed-v1",
        "requested_products": request_products or [
            {"product_code": "water_depth", "required": True, "scope_id": profile.scope_id, "aggregation": "FRAME"},
            {"product_code": "storage_volume", "required": True, "scope_id": profile.scope_id, "aggregation": "FRAME"},
            {"product_code": "maximum_depth", "required": True, "scope_id": profile.scope_id, "aggregation": "MAX_OVER_WINDOW"},
            {"product_code": "gate_audit", "required": False, "scope_id": "AUDIT_RUN_GLOBAL", "aggregation": "EVENT_TIMELINE"},
        ],
        "require_complete": False,
    }, sort_keys=False), encoding="utf-8")
    envelope_path = root / "envelope.json"
    package_path = root / "package"
    result_path = root / "result.nc"
    create_local_execution_envelope(request_path)
    run_triangular_solver(config_path)
    if with_g1:
        export_gate_dispatch_for_netcdf(result_path, GateDispatchInput(), script_id=assignment["script_id"])
    return {
        "root": root, "mesh": mesh_path, "business": business_path, "config": config_path,
        "assignment": assignment_path, "envelope": envelope_path, "request": request_path,
        "result": result_path, "package": package_path,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_export_without_g1_is_valid_but_incomplete_and_require_complete_exits_3(tmp_path, capsys):
    case = _make_case(tmp_path / "incomplete")
    source_hashes = {key: _sha(case[key]) for key in ("config", "business", "mesh", "assignment", "envelope", "result")}
    report = export_result_package(case["request"])
    assert report.valid and not report.complete
    assert report.package_status == "INCOMPLETE_REVIEW"
    manifest = json.loads((case["package"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["can_publish"] is False
    assert manifest["engineering_validated"] is False
    assert manifest["identity"]["identity_authentication_status"] == "NOT_ASSESSED"
    assert manifest["identity"]["native_identity_basis"] == "OUTER_EXECUTION_CONTEXT_ONLY"
    assert manifest["gate_audit"]["status"] == "MISSING_INPUT"
    assert manifest["source_graph"]["validation_basis"] == "LOCAL_SOURCE_GRAPH_V1"
    frames = json.loads((case["package"] / "frames.json").read_text(encoding="utf-8"))
    summary = json.loads((case["package"] / "summary.json").read_text(encoding="utf-8"))
    assert frames["frame_times_s"] == [0.0, 0.5, 1.0]
    assert frames["overall_maximum_depth_m"] == 0.2
    assert frames["overall_maximum_depth_time_s"] == 0.0
    assert frames["overall_maximum_depth_face_id"] == 92001
    assert all(row["maximum_depth_m"] == 0.2 for row in frames["frames"])
    assert all(row["water_volume_m3"] == pytest.approx(0.4, abs=1e-12) for row in frames["frames"])
    assert summary["total_face_area_m2"] == 2.0
    assert summary["maximum_depth_tie_policy"] == "EARLIEST_TIME_THEN_LOWEST_FACE_ID"
    assert summary["maximum_water_volume_m3"] == pytest.approx(0.4, abs=1e-12)
    assert summary["maximum_water_volume_time_s"] == 0.0
    assert validate_result_package(case["request"]).valid
    assert main(["validate", "--request", str(case["request"]), "--require-complete"]) == 3
    capsys.readouterr()
    assert {key: _sha(case[key]) for key in source_hashes} == source_hashes
    cli_request = yaml.safe_load(case["request"].read_text(encoding="utf-8"))
    cli_request["output_directory"] = "package_cli"
    cli_request["require_complete"] = True
    cli_request_path = case["root"] / "request_cli.yaml"
    cli_request_path.write_text(yaml.safe_dump(cli_request, sort_keys=False), encoding="utf-8")
    assert main(["export", "--request", str(cli_request_path)]) == 3
    capsys.readouterr()
    assert (case["root"] / "package_cli" / "manifest.json").is_file()
    with pytest.raises(RehearsalPackageError, match="refusing to overwrite"):
        export_result_package(case["request"])


def test_complete_g1_pair_is_preserved_byte_for_byte_and_export_validates(tmp_path):
    case = _make_case(tmp_path / "complete", with_g1=True)
    csv_path, json_path = gate_dispatch_paths(case["result"])
    csv_before, json_before = csv_path.read_bytes(), json_path.read_bytes()
    payload_before = json.loads(json_before)
    report = export_result_package(case["request"])
    assert report.valid and report.complete
    assert (case["package"] / "audit/gate_dispatch.csv").read_bytes() == csv_before
    assert (case["package"] / "audit/gate_dispatch.json").read_bytes() == json_before
    packaged_payload = json.loads((case["package"] / "audit/gate_dispatch.json").read_text(encoding="utf-8"))
    assert packaged_payload["result_file"] == payload_before["result_file"] == str(case["result"].resolve())
    assert validate_result_package(case["request"]).complete


def test_final_post_rename_validation_failure_rolls_back_success_manifest(tmp_path, monkeypatch):
    case = _make_case(tmp_path / "final-reader-failure", with_g1=True)
    destination = case["package"]
    actual_validate = validate_result_package

    def fail_only_after_publish(request_path, package_directory=None):
        if package_directory is not None and Path(package_directory).resolve() == destination.resolve():
            raise RehearsalPackageError("injected post-rename fresh validation failure")
        return actual_validate(request_path, package_directory)

    monkeypatch.setattr("hydrodynamics.rehearsal_package.validate_result_package", fail_only_after_publish)
    with pytest.raises(RehearsalPackageError, match="post-rename fresh validation failure"):
        export_result_package(case["request"])
    assert not (destination / "manifest.json").exists()
    assert not destination.exists()


def test_post_rename_source_graph_race_rolls_back_export_files(tmp_path, monkeypatch):
    import hydrodynamics.rehearsal_package as package_module

    case = _make_case(tmp_path / "source-race", with_g1=True)
    destination = case["package"]
    actual_rename = package_module.os.rename

    def rename_then_change_request(source, target):
        result = actual_rename(source, target)
        if Path(target).resolve() == destination.resolve():
            case["request"].write_bytes(case["request"].read_bytes() + b"\n# changed after publication\n")
        return result

    monkeypatch.setattr(package_module.os, "rename", rename_then_change_request)
    with pytest.raises(RehearsalPackageError):
        package_module.export_result_package(case["request"])
    assert case["request"].read_bytes().endswith(b"# changed after publication\n")
    assert not (destination / "manifest.json").exists()
    assert not destination.exists()


def test_post_rename_rollback_preserves_concurrent_foreign_file(tmp_path, monkeypatch):
    import hydrodynamics.rehearsal_package as package_module

    case = _make_case(tmp_path / "foreign-content", with_g1=True)
    destination = case["package"]
    actual_rename = package_module.os.rename
    actual_validate = package_module.validate_result_package

    def rename_then_add_foreign_file(source, target):
        result = actual_rename(source, target)
        if Path(target).resolve() == destination.resolve():
            (destination / "foreign.keep").write_text("concurrent content", encoding="utf-8")
        return result

    def fail_fresh_validation(request_path, package_directory=None):
        if package_directory is not None and Path(package_directory).resolve() == destination.resolve():
            raise RehearsalPackageError("injected validation failure after concurrent write")
        return actual_validate(request_path, package_directory)

    monkeypatch.setattr(package_module.os, "rename", rename_then_add_foreign_file)
    monkeypatch.setattr(package_module, "validate_result_package", fail_fresh_validation)
    with pytest.raises(RehearsalPackageError, match="validation failure after concurrent write"):
        package_module.export_result_package(case["request"])
    assert (destination / "foreign.keep").read_text(encoding="utf-8") == "concurrent content"
    assert not (destination / "manifest.json").exists()


def test_rollback_checks_parent_identity_before_removing_same_identity_child(tmp_path, monkeypatch):
    import hydrodynamics.rehearsal_package as package_module

    case = _make_case(tmp_path / "parent-replaced", with_g1=True)
    destination = case["package"]
    moved_native = case["root"] / "moved-original-native"
    original_rename = package_module.os.rename
    actual_capture = package_module._capture_export_tree
    actual_identity = package_module._filesystem_identity
    expected_bytes: dict[str, bytes] = {}
    original_leaf_identity: dict[str, tuple[int, int]] = {}

    def capture_then_remember_leaf(stage, expected_files):
        files, directories = actual_capture(stage, expected_files)
        original_leaf_identity["native/result.nc"] = files["native/result.nc"]
        return files, directories

    def report_same_leaf_identity(path, *, directory):
        if (not directory and Path(path).resolve() == (destination / "native/result.nc").resolve()
                and "native/result.nc" in original_leaf_identity):
            # Simulate a same-volume hardlink when this Windows sandbox denies
            # hardlink creation; the frozen independent probe uses a real one.
            return original_leaf_identity["native/result.nc"]
        return actual_identity(path, directory=directory)

    def rename_then_replace_native_parent(source, target):
        result = original_rename(source, target)
        if Path(target).resolve() == destination.resolve():
            original_rename(destination / "native", moved_native)
            (destination / "native").mkdir()
            (destination / "native/result.nc").write_bytes((moved_native / "result.nc").read_bytes())
            expected_bytes["native"] = (destination / "native/result.nc").read_bytes()
            case["request"].write_bytes(case["request"].read_bytes() + b"\n# force source graph revalidation failure\n")
        return result

    monkeypatch.setattr(package_module.os, "rename", rename_then_replace_native_parent)
    monkeypatch.setattr(package_module, "_capture_export_tree", capture_then_remember_leaf)
    monkeypatch.setattr(package_module, "_filesystem_identity", report_same_leaf_identity)
    with pytest.raises(RehearsalPackageError) as exc_info:
        package_module.export_result_package(case["request"])
    foreign_link = destination / "native/result.nc"
    assert foreign_link.exists(), str(exc_info.value)
    assert foreign_link.read_bytes() == expected_bytes["native"]
    assert (moved_native / "result.nc").read_bytes() == expected_bytes["native"]
    assert not (destination / "manifest.json").exists()
    assert "native" in str(exc_info.value)


def test_rollback_recovers_concurrently_modified_manifest_bytes(tmp_path, monkeypatch):
    import hydrodynamics.rehearsal_package as package_module

    case = _make_case(tmp_path / "modified-manifest", with_g1=True)
    destination = case["package"]
    original_rename = package_module.os.rename
    concurrent_bytes: dict[str, bytes] = {}

    def rename_then_modify_manifest(source, target):
        result = original_rename(source, target)
        if Path(target).resolve() == destination.resolve():
            path = destination / "manifest.json"
            payload = json.loads(path.read_bytes())
            payload["concurrent_user_note"] = "preserve this exact failure evidence"
            concurrent_bytes["manifest"] = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
            path.write_bytes(concurrent_bytes["manifest"])
            case["request"].write_bytes(case["request"].read_bytes() + b"\n# force fresh source revalidation failure\n")
        return result

    monkeypatch.setattr(package_module.os, "rename", rename_then_modify_manifest)
    with pytest.raises(RehearsalPackageError) as exc_info:
        package_module.export_result_package(case["request"])
    recovery_files = [path for path in destination.glob("manifest.recovered.*.json") if path.is_file()]
    assert not (destination / "manifest.json").exists()
    assert len(recovery_files) == 1
    assert recovery_files[0].read_bytes() == concurrent_bytes["manifest"]
    assert str(recovery_files[0]) in str(exc_info.value)


def test_replaced_manifest_inode_is_archived_without_overwriting_concurrent_file(tmp_path, monkeypatch):
    import hydrodynamics.rehearsal_package as package_module

    case = _make_case(tmp_path / "replaced-manifest", with_g1=True)
    destination = case["package"]
    original_rename = package_module.os.rename
    concurrent_manifest: dict[str, bytes] = {}

    def rename_then_replace_manifest(source, target):
        result = original_rename(source, target)
        if Path(target).resolve() == destination.resolve():
            original = destination / "manifest.json"
            concurrent_manifest["bytes"] = original.read_bytes()
            original_rename(original, destination / "manifest.original.json")
            original.write_bytes(concurrent_manifest["bytes"])
            case["request"].write_bytes(case["request"].read_bytes() + b"\n# force fresh source revalidation failure\n")
        return result

    monkeypatch.setattr(package_module.os, "rename", rename_then_replace_manifest)
    with pytest.raises(RehearsalPackageError) as exc_info:
        package_module.export_result_package(case["request"])
    recovery_files = [path for path in destination.glob("manifest.recovered.*.json") if path.is_file()]
    assert not (destination / "manifest.json").exists()
    assert (destination / "manifest.original.json").read_bytes() == concurrent_manifest["bytes"]
    assert len(recovery_files) == 1
    assert recovery_files[0].read_bytes() == concurrent_manifest["bytes"]
    assert str(recovery_files[0]) in str(exc_info.value)


def test_replaced_package_root_is_preserved_and_reported(tmp_path, monkeypatch):
    import hydrodynamics.rehearsal_package as package_module

    case = _make_case(tmp_path / "root-replaced", with_g1=True)
    destination = case["package"]
    detached = case["root"] / "detached-package-root"
    original_rename = package_module.os.rename
    original_manifest: dict[str, bytes] = {}

    def rename_then_replace_package_root(source, target):
        result = original_rename(source, target)
        if Path(target).resolve() == destination.resolve():
            original_manifest["bytes"] = (destination / "manifest.json").read_bytes()
            original_rename(destination, detached)
            destination.mkdir()
            (destination / "foreign.keep").write_bytes(b"replacement root content")
            case["request"].write_bytes(case["request"].read_bytes() + b"\n# force source revalidation failure\n")
        return result

    monkeypatch.setattr(package_module.os, "rename", rename_then_replace_package_root)
    with pytest.raises(RehearsalPackageError) as exc_info:
        package_module.export_result_package(case["request"])
    assert (destination / "foreign.keep").read_bytes() == b"replacement root content"
    assert (detached / "manifest.json").read_bytes() == original_manifest["bytes"]
    assert str(destination) in str(exc_info.value)


def test_output_directory_rejects_reparse_alias_before_external_write(tmp_path, monkeypatch):
    import hydrodynamics.rehearsal_package as package_module

    case = _make_case(tmp_path / "reparse-case")
    outside = tmp_path / "outside-reparse-case"
    outside.mkdir()
    guard = outside / "keep.txt"
    guard.write_bytes(b"foreign target must be preserved")
    junction = case["root"] / "output_alias"
    is_reparse = package_module._is_reparse_point

    def simulate_directory_reparse(path):
        if Path(path) == junction:
            return True
        return is_reparse(path)

    monkeypatch.setattr(package_module, "_is_reparse_point", simulate_directory_reparse)
    request = yaml.safe_load(case["request"].read_text(encoding="utf-8"))
    request["output_directory"] = "output_alias/package"
    case["request"].write_text(yaml.safe_dump(request, sort_keys=False), encoding="utf-8")
    with pytest.raises(RehearsalPackageError, match="reparse point"):
        export_result_package(case["request"])
    assert not (outside / "package").exists()
    assert guard.read_bytes() == b"foreign target must be preserved"


def test_package_validator_detects_native_array_mutation(tmp_path):
    case = _make_case(tmp_path / "native-tamper")
    export_result_package(case["request"])
    with netCDF4.Dataset(case["result"], "r+") as dataset:
        depth = dataset.variables["water_depth"][:]
        depth[1, 0] = 0.25
        dataset.variables["water_depth"][:] = depth
    with pytest.raises(RehearsalPackageError):
        validate_result_package(case["request"])


@pytest.mark.parametrize("tamper", ["fake_digest", "missing_file", "extra_file", "duplicate_key", "duplicate_record", "unknown_field"])
def test_package_validator_rejects_manifest_and_artifact_tampering(tmp_path, tamper):
    case = _make_case(tmp_path / tamper)
    export_result_package(case["request"])
    manifest_path = case["package"] / "manifest.json"
    if tamper == "fake_digest":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = "0" * 64
        _json_write(manifest_path, manifest)
    elif tamper == "missing_file":
        (case["package"] / "summary.json").unlink()
    elif tamper == "extra_file":
        (case["package"] / "unexpected.txt").write_text("extra", encoding="utf-8")
    elif tamper == "duplicate_key":
        manifest_path.write_text('{"format":"rehearsal-result-package-v1","format":"rehearsal-result-package-v1"}', encoding="utf-8")
    elif tamper == "unknown_field":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["unexpected"] = True
        _json_write(manifest_path, manifest)
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append(dict(manifest["files"][0]))
        _json_write(manifest_path, manifest)
    with pytest.raises(RehearsalPackageError):
        validate_result_package(case["request"])


def test_package_keys_and_output_path_traversal_are_rejected(tmp_path):
    for key in ("../escape", "C:/escape", "/rooted", "a//b"):
        with pytest.raises(Exception):
            PackageFileRecord(key=key, sha256="0" * 64, byte_count=0, media_type="application/octet-stream")
    case = _make_case(tmp_path / "path")
    request = yaml.safe_load(case["request"].read_text(encoding="utf-8"))
    request["output_directory"] = "../escape"
    with pytest.raises(Exception):
        R2AExportRequest.model_validate(request)


def test_checked_in_example_requests_match_the_package_schema():
    example_dir = Path(__file__).resolve().parents[1] / "examples" / "rehearsal_package"
    schema = R2AExportRequest.model_json_schema()
    assert schema["properties"]["format"]["const"] == "rehearsal-result-package-export-request-v1"
    for name in ("package_request.yaml", "package_request_with_g1.yaml"):
        value = yaml.safe_load((example_dir / name).read_text(encoding="utf-8"))
        assert R2AExportRequest.model_validate(value).format == "rehearsal-result-package-export-request-v1"


def test_selector_validation_rejects_wrong_scope_policy_and_frame(tmp_path):
    case = _make_case(tmp_path / "selectors")
    export_result_package(case["request"])
    selectors = json.loads((case["package"] / "selectors.json").read_text(encoding="utf-8"))
    result_manifest = ResultSetManifest.model_validate(selectors["result_set_manifest"])
    valid_selector = ResultProductSelector.model_validate(selectors["selectors"][0])
    assert validate_selector_against_manifest(valid_selector, result_manifest).available
    mutations = [
        valid_selector.model_copy(update={"scope_hash": "0" * 64}),
        valid_selector.model_copy(update={"metric_policy_version": "wrong-policy"}),
        valid_selector.model_copy(update={"frame_index": 2, "time_s": 0.0}),
    ]
    assert all(not validate_selector_against_manifest(item, result_manifest).available for item in mutations)


@pytest.mark.parametrize("field,value", [("attempt_id", "attempt-other"), ("script_id", "20261019-1792400000000000001")])
def test_envelope_attempt_and_script_are_compared_with_separate_assignment(tmp_path, field, value):
    case = _make_case(tmp_path / field)
    envelope = json.loads(case["envelope"].read_text(encoding="utf-8"))
    envelope[field] = value
    if field == "script_id":
        envelope["local_run_id"] = value
    _json_write(case["envelope"], envelope)
    with pytest.raises(RehearsalPackageError, match="execution envelope"):
        validate_result_package(case["request"])


def test_assignment_case_and_source_graph_are_rechecked(tmp_path):
    case = _make_case(tmp_path / "source-graph")
    assignment = json.loads(case["assignment"].read_text(encoding="utf-8"))
    assignment["model_case_id"] = "another-case"
    _json_write(case["assignment"], assignment)
    with pytest.raises(RehearsalPackageError, match="model_case_id"):
        validate_result_package(case["request"])


def test_missing_original_source_graph_refuses_validation(tmp_path):
    case = _make_case(tmp_path / "source-missing")
    export_result_package(case["request"])
    moved = case["business"].with_suffix(".moved")
    case["business"].rename(moved)
    try:
        with pytest.raises(RehearsalPackageError):
            validate_result_package(case["request"])
    finally:
        moved.rename(case["business"])


@pytest.mark.parametrize("g1_error", ["wrong_time", "wrong_case", "missing_member"])
def test_g1_source_time_case_and_missing_member_are_not_trusted(tmp_path, g1_error):
    case = _make_case(tmp_path / g1_error)
    if g1_error == "missing_member":
        csv_path, json_path = gate_dispatch_paths(case["result"])
        export_gate_dispatch_for_netcdf(case["result"], GateDispatchInput(), script_id="20261019-1792400000000000000")
        json_path.unlink()
        report = export_result_package(case["request"])
        assert not report.complete
        manifest = json.loads((case["package"] / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["gate_audit"]["status"] == "MISSING_INPUT"
        assert (case["package"] / "audit/gate_dispatch.csv").is_file()
        assert not (case["package"] / "audit/gate_dispatch.json").exists()
        return
    assignment = json.loads(case["assignment"].read_text(encoding="utf-8"))
    context = {"format": "triangular-solver-result-v1", "script_id": assignment["script_id"]}
    times = [0.0, 0.4, 1.0] if g1_error == "wrong_time" else [0.0, 0.5, 1.0]
    if g1_error == "wrong_case":
        context["case_id"] = "another-native-case"
    log = build_gate_dispatch_log(times, GateDispatchInput(), result_context=context)
    write_gate_dispatch_log(case["result"], log)
    with pytest.raises(RehearsalPackageError, match="G1"):
        export_result_package(case["request"])


def test_required_unsupported_product_stays_explicit_and_blocks_completeness(tmp_path):
    profile = _load_r2a_profile().profile
    products = [
        {"product_code": "water_depth", "required": True, "scope_id": profile.scope_id, "aggregation": "FRAME"},
        {"product_code": "wet_duration", "required": True, "scope_id": profile.scope_id, "aggregation": "WET_DURATION"},
    ]
    case = _make_case(tmp_path / "unsupported", with_g1=True, request_products=products)
    report = export_result_package(case["request"])
    assert not report.complete
    manifest = json.loads((case["package"] / "manifest.json").read_text(encoding="utf-8"))
    status = next(item for item in manifest["products"] if item["product_code"] == "wet_duration")
    assert status["required"] is True
    assert status["status"] == "UNSUPPORTED"


def test_packaged_profile_resource_is_in_a_built_wheel(tmp_path):
    from setuptools.build_meta import build_wheel

    wheel_name = build_wheel(str(tmp_path))
    wheel_path = tmp_path / wheel_name
    with zipfile.ZipFile(wheel_path) as archive:
        assert "hydrodynamics/rehearsal_data/r2a_flat_bed_closed_v1.json" in archive.namelist()
        raw = archive.read("hydrodynamics/rehearsal_data/r2a_flat_bed_closed_v1.json")
    assert hashlib.sha256(raw).hexdigest() == _load_r2a_profile().profile_sha256
