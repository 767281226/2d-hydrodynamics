import hashlib
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import textwrap

import jsonschema
import pytest

from hydrodynamics import domain_mesh_only, restricted_worker
from hydrodynamics.triangular_mesh import TriangularMesh


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "hydrodynamics" / "schemas" / "domain_mesh_quality_failure_diagnostic_v1.schema.json"
EXAMPLE_PATH = ROOT / "examples" / "triangular_mesh" / "domain_mesh_quality_failure.synthetic.example.json"
RUN_ID = "synthetic-quality-failure"
SOURCE_SHA256 = "b" * 64
CODE_HASHES = {
    "hydrodynamics/domain_mesh_only.py": "c" * 64,
    "hydrodynamics/restricted_worker.py": "d" * 64,
}


def _synthetic_config_bytes(run_id: str) -> bytes:
    return json.dumps({
        "run_id": run_id,
        "source_manifest": {"sha256": SOURCE_SHA256},
        "mesh_generation": {
            "minimum_face_quality": 0.1,
            "maximum_nodes": 2_000_000,
            "maximum_faces": 500_000,
        },
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _synthetic_config_sha256(run_id: str) -> str:
    return hashlib.sha256(_synthetic_config_bytes(run_id)).hexdigest()


CONFIG_SHA256 = _synthetic_config_sha256(RUN_ID)


def _mesh(face_count: int = 20) -> TriangularMesh:
    heights = [
        0.02, 0.02, 0.03, 0.03, 0.04, 0.04, 0.05, 0.05, 0.06, 0.06,
        0.07, 0.07, 0.08, 0.08, 0.09, 0.09, 0.10, 0.10, 0.11, 0.11,
    ][:face_count]
    node_xy = []
    face_nodes = []
    for row, height in enumerate(heights):
        x = float(row * 4)
        start = len(node_xy)
        node_xy.extend(((x, 0.0), (x + 2.0, 0.0), (x, height)))
        face_nodes.append((start, start + 1, start + 2))
    face_ids = [2**53 + 100 - row for row in range(face_count)]
    return TriangularMesh(
        node_xy=node_xy,
        face_nodes=face_nodes,
        coordinate_system="EPSG:4548",
        mesh_id="synthetic-quality-mesh",
        face_id=face_ids,
        boundary_groups={"D_exterior_geometry": [(0, 1)]},
        constraint_groups={"synthetic_constraint": [(4, 5)]} if face_count > 1 else {},
        geometry_tolerance_m=1e-8,
        minimum_face_area_m2=1e-12,
    )


def _mesh_bytes(mesh: TriangularMesh) -> bytes:
    return domain_mesh_only._canonical_json_bytes({
        "format": "synthetic-canonical-mesh-fixture-v1",
        "mesh_id": mesh.mesh_id,
        "coordinate_system": mesh.coordinate_system,
        "node_xy": mesh.node_xy,
        "face_nodes": mesh.face_nodes,
        "face_id": mesh.face_id,
    })


def _quality_case(
    mesh: TriangularMesh,
    *,
    run_id: str = RUN_ID,
    config_sha256: str | None = None,
):
    quality = mesh.quality_report(minimum_quality=0.1)
    mesh_bytes = _mesh_bytes(mesh)
    primary = domain_mesh_only._quality_failure_primary_record(
        mesh,
        quality,
        run_id=run_id,
        config_sha256=config_sha256 or _synthetic_config_sha256(run_id),
        source_manifest_sha256=SOURCE_SHA256,
        route_code_sha256=domain_mesh_only._quality_failure_code_identity_sha256(CODE_HASHES),
        mesh_sha256=hashlib.sha256(mesh_bytes).hexdigest(),
        canonical_mesh_bytes=len(mesh_bytes),
    )
    diagnostic = domain_mesh_only._build_quality_failure_diagnostic(mesh, quality, primary)
    return quality, primary, diagnostic


def _binding_files(
    stage: Path,
    *,
    run_id: str = RUN_ID,
    code_hashes: dict[str, str] = CODE_HASHES,
) -> str:
    scratch = stage / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    config_bytes = _synthetic_config_bytes(run_id)
    config_sha256 = hashlib.sha256(config_bytes).hexdigest()
    (scratch / "worker-control.json").write_text(json.dumps({
        "format": "triangle-preparation-worker-control-v1",
        "config_sha256": config_sha256,
        "code_hashes": code_hashes,
    }), encoding="utf-8")
    (scratch / "worker-config.json").write_bytes(config_bytes)
    return config_sha256


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate_definition(schema: dict, definition: str, value: object) -> None:
    wrapper = {
        "$schema": schema["$schema"],
        "$id": schema["$id"],
        "$defs": schema["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    jsonschema.validate(value, wrapper)


def test_diagnostic_schema_and_synthetic_example_are_valid() -> None:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    example = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(example, schema)
    assert example["diagnostic_only"] is True
    assert example["mesh_ready"] is False
    assert example["worker_exit_code"] == 53
    assert example["worst_face_selection"]["faces"][0]["face_id_decimal"] == "9007199254740993"
    assert len(example["quality_summary"]["histogram"]["counts"]) == 1024
    _validate_definition(schema, "qualityFailurePrimary", {
        "format": "triangle-mesh-quality-failure-primary-v1",
        "attempt_id": RUN_ID,
        "run_id": RUN_ID,
        "worker_exit_code": 53,
        "failure_code": "MINIMUM_FACE_QUALITY_FAILED",
        "message": "synthetic quality failure",
        "diagnostic_only": True,
        "config_sha256": CONFIG_SHA256,
        "source_manifest_sha256": SOURCE_SHA256,
        "route_code_sha256": "c" * 64,
        "metric_id": "triangle-face-quality-q4sqrt3-area-over-edge-square-sum-v1",
        "quality_threshold": 0.1,
        "comparison": "strict_lt",
        "node_count": 3,
        "face_count": 1,
        "bad_face_count": 1,
        "mesh_sha256": "d" * 64,
        "canonical_mesh_bytes": 128,
    })


def test_histogram_quantile_intervals_worst_top16_and_exact_decimal_face_ids() -> None:
    mesh = _mesh()
    report, primary, diagnostic = _quality_case(mesh)
    summary = diagnostic["quality_summary"]
    histogram = summary["histogram"]
    assert primary["bad_face_count"] == len(report["faces_below_quality_threshold"])
    assert primary["bad_face_count"] == sum(value < 0.1 for value in mesh.face_quality)
    assert sum(histogram["counts"]) + histogram["underflow_count"] + histogram["overflow_count"] == mesh.n_face
    assert len(histogram["counts"]) == 1024
    assert summary["minimum_quality_exact"] == min(mesh.face_quality)
    assert summary["maximum_quality_exact"] == max(mesh.face_quality)
    ordered = sorted(float(value) for value in mesh.face_quality)
    assert len(summary["quantile_intervals"]) == 6
    for item in summary["quantile_intervals"]:
        rank = math.ceil(item["percentile"] * mesh.n_face)
        assert item["rank_one_based"] == rank
        assert item["lower_bound"] <= ordered[rank - 1] <= item["upper_bound"]
        assert item["exact_value_reported"] is False

    worst = diagnostic["worst_face_selection"]
    assert worst["limit"] == 16 and worst["selected_count"] == 16 and worst["exact"] is True
    faces = worst["faces"]
    keys = [(item["quality"], int(item["face_id_decimal"]), item["face_row_zero_based"]) for item in faces]
    assert keys == sorted(keys)
    assert int(faces[0]["face_id_decimal"]) > 2**53
    assert mesh.face_quality[0] == mesh.face_quality[1]
    assert [faces[0]["face_row_zero_based"], faces[1]["face_row_zero_based"]] == [1, 0]
    assert next(item for item in faces if item["face_row_zero_based"] == 0)["boundary_group_names"] == [
        "D_exterior_geometry"
    ]
    assert next(item for item in faces if item["face_row_zero_based"] == 1)["constraint_group_names"] == [
        "synthetic_constraint"
    ]
    assert "node_xy" not in diagnostic and "face_nodes" not in diagnostic
    serialized = domain_mesh_only._canonical_json_bytes(diagnostic)
    assert len(serialized) <= domain_mesh_only.QUALITY_FAILURE_DIAGNOSTIC_MAX_BYTES
    jsonschema.validate(diagnostic, _schema())


def test_strict_threshold_excludes_equal_q_and_nextafter_above() -> None:
    mesh = _mesh(3)
    object.__setattr__(mesh, "face_quality", (
        math.nextafter(0.1, 0.0),
        0.1,
        math.nextafter(0.1, 1.0),
    ))
    report, primary, diagnostic = _quality_case(mesh)
    assert report["faces_below_quality_threshold"] == [0]
    assert primary["bad_face_count"] == 1
    assert diagnostic["quality_summary"]["bad_face_count_exact"] == 1
    assert diagnostic["quality_summary"]["minimum_quality_exact"] == math.nextafter(0.1, 0.0)


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("cap", "DIAGNOSTIC_BYTE_CAP_EXCEEDED"),
        ("budget", "DIAGNOSTIC_BUDGET_REJECTED"),
        ("no_clobber", "DIAGNOSTIC_NO_CLOBBER_CONFLICT"),
        ("interrupted", "DIAGNOSTIC_INTERRUPTED"),
    ],
)
def test_diagnostic_write_error_preserves_quality_primary_and_no_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_code: str,
) -> None:
    mesh = _mesh(3)
    _report, primary, diagnostic = _quality_case(mesh)
    stage = tmp_path / mode
    (stage / "scratch").mkdir(parents=True)
    (stage / "artifacts").mkdir()
    target = stage / domain_mesh_only.QUALITY_FAILURE_DIAGNOSTIC_RELATIVE_PATH
    if mode == "budget":
        (stage / "existing-output.bin").write_bytes(b"x" * 200_000)
    elif mode == "no_clobber":
        target.parent.mkdir(parents=True)
        target.write_bytes(b"preserve-existing")
    elif mode == "interrupted":
        monkeypatch.setattr(
            restricted_worker,
            "_write_no_clobber_bytes",
            lambda *args, **kwargs: (_ for _ in ()).throw(InterruptedError("synthetic cancellation")),
        )
    elif mode == "cap":
        monkeypatch.setattr(
            domain_mesh_only,
            "_canonical_json_bytes",
            lambda _value: b"x" * (domain_mesh_only.QUALITY_FAILURE_DIAGNOSTIC_MAX_BYTES + 1),
        )

    reference, error = domain_mesh_only._persist_quality_failure_diagnostic(
        stage,
        diagnostic,
        output_budget_bytes=128 * 1024 if mode == "budget" else 1024 * 1024,
    )
    quality_error = domain_mesh_only._make_quality_failure_exception(primary, reference, [error])
    assert reference is None
    assert error is not None and error["code"] == expected_code
    assert str(quality_error) == primary["message"]
    assert quality_error.quality_failure["worker_exit_code"] == 53
    assert quality_error.failure_diagnostic_error["errors"][0]["code"] == expected_code
    assert not any((stage / marker).exists() for marker in ("MESH_READY", "READY", "latest"))
    if mode == "no_clobber":
        assert target.read_bytes() == b"preserve-existing"


def _parent_binding_files(
    stage: Path,
    *,
    run_id: str,
    code_hashes: dict[str, str] = CODE_HASHES,
) -> str:
    scratch = stage / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    config_bytes = _synthetic_config_bytes(run_id)
    config_sha256 = hashlib.sha256(config_bytes).hexdigest()
    (scratch / "worker-control.json").write_text(json.dumps({
        "format": "triangle-preparation-worker-control-v1",
        "config_sha256": config_sha256,
        "code_hashes": code_hashes,
    }), encoding="utf-8")
    (scratch / "worker-config.json").write_bytes(config_bytes)
    return config_sha256


def test_parent_failed_bundle_references_diagnostic_and_cancel_keeps_primary(tmp_path: Path) -> None:
    mesh = _mesh(3)
    _report, primary, diagnostic = _quality_case(mesh)
    code_hashes = CODE_HASHES

    output = tmp_path / "out"
    stage = output / ".synthetic.pending"
    (stage / "scratch").mkdir(parents=True)
    (stage / "artifacts").mkdir()
    _parent_binding_files(stage, run_id=RUN_ID, code_hashes=code_hashes)
    assert domain_mesh_only._persist_quality_failure_primary(
        stage, primary, scratch_budget_bytes=16 * 1024 * 1024,
    ) is None
    reference, error = domain_mesh_only._persist_quality_failure_diagnostic(
        stage, diagnostic, output_budget_bytes=1024 * 1024,
    )
    assert reference is not None and error is None
    (stage / "MESH_READY").write_text("must be removed")
    state = restricted_worker._WorkerStartState(ever_started=True, process_id=1101, mesh_only_route=True)
    finished = restricted_worker._finish_failure(
        stage,
        output / "synthetic.failed",
        run_id=RUN_ID,
        status="worker_failed",
        exit_code=53,
        message=primary["message"],
        worker_start_state=state,
        config_sha256=CONFIG_SHA256,
        worker_receipt={"job_active_processes_at_exit": 0},
        output_budget=1024 * 1024,
        worker_result={
            "quality_failure": primary,
            "failure_diagnostic": reference,
            "failure_diagnostic_error": None,
        },
    )
    failed = output / "synthetic.failed"
    status = json.loads((failed / "status.json").read_text(encoding="utf-8"))
    manifest = json.loads((failed / "manifest.json").read_text(encoding="utf-8"))
    schema = _schema()
    _validate_definition(schema, "qualityFailurePrimary", status["quality_failure"])
    _validate_definition(schema, "failureDiagnosticReference", status["failure_diagnostic"])
    assert finished.exit_code == 53
    assert status["failure_diagnostic_error"] is None
    assert manifest["failure_diagnostic"] == reference
    assert not (failed / "MESH_READY").exists() and not (failed / "READY").exists()

    cancel_output = tmp_path / "cancel-out"
    cancel_stage = cancel_output / ".cancelled.pending"
    (cancel_stage / "scratch").mkdir(parents=True)
    (cancel_stage / "artifacts").mkdir()
    _parent_binding_files(cancel_stage, run_id=RUN_ID, code_hashes=code_hashes)
    assert domain_mesh_only._persist_quality_failure_primary(
        cancel_stage, primary, scratch_budget_bytes=16 * 1024 * 1024,
    ) is None
    cancelled = restricted_worker._finish_failure(
        cancel_stage,
        cancel_output / "synthetic.cancelled",
        run_id=RUN_ID,
        status="cancelled",
        exit_code=44,
        message="cancelled while creating quality diagnostics",
        worker_start_state=restricted_worker._WorkerStartState(
            ever_started=True, process_id=1102, mesh_only_route=True,
        ),
        config_sha256=CONFIG_SHA256,
        worker_receipt={"job_active_processes_at_exit": 0},
        output_budget=1024 * 1024,
    )
    cancelled_status = json.loads((cancel_output / "synthetic.cancelled" / "status.json").read_text(encoding="utf-8"))
    assert cancelled.exit_code == 44 and cancelled_status["exit_code"] == 44
    assert cancelled_status["quality_failure"]["worker_exit_code"] == 53
    assert cancelled_status["failure_diagnostic"] is None
    assert cancelled_status["failure_diagnostic_error"]["errors"][0]["code"] == "DIAGNOSTIC_INTERRUPTED"
    _validate_definition(_schema(), "qualityFailurePrimary", cancelled_status["quality_failure"])
    _validate_definition(_schema(), "failureDiagnosticError", cancelled_status["failure_diagnostic_error"])
    assert not (cancel_output / "synthetic.cancelled" / "MESH_READY").exists()


@pytest.mark.skipif(os.name != "nt", reason="restricted diagnostic execution requires Windows Job Objects")
def test_synthetic_quality_diagnostic_is_built_inside_one_job_and_stays_failed(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "out"
    stage = output / ".quality-job.pending"
    scratch = stage / "scratch"
    (stage / "artifacts").mkdir(parents=True)
    scratch.mkdir(parents=True)
    code_hashes = restricted_worker._module_code_hashes(
        root, pilot_route=False, domain_mesh_only_route=True,
    )
    job_run_id = "synthetic-quality-job"
    bindings = {
        "run_id": job_run_id,
        "config_sha256": _synthetic_config_sha256(job_run_id),
        "source_manifest_sha256": SOURCE_SHA256,
        "code_hashes": code_hashes,
    }
    assert _parent_binding_files(stage, run_id=bindings["run_id"], code_hashes=code_hashes) == bindings["config_sha256"]
    script = tmp_path / "synthetic_quality_failure_worker.py"
    script.write_text(textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import sys
        import traceback
        stage = Path(sys.argv[1])
        def record_exception(kind, value, tb):
            (stage / "scratch" / "synthetic-worker-traceback.txt").write_text(
                "".join(traceback.format_exception(kind, value, tb)), encoding="utf-8"
            )
            sys.__excepthook__(kind, value, tb)
        sys.excepthook = record_exception
        sys.stderr = (stage / "scratch" / "synthetic-worker-stderr.txt").open("w", encoding="utf-8")
        from hydrodynamics import domain_mesh_only as dm
        from hydrodynamics.restricted_worker import _json_bytes, _write_no_clobber_bytes
        from hydrodynamics.triangular_mesh import TriangularMesh
        import hashlib
        bindings = json.loads(sys.argv[2])
        assert dm._current_process_in_job()
        mesh = TriangularMesh(
            node_xy=[(0.0, 0.0), (2.0, 0.0), (1.0, 0.02)],
            face_nodes=[(0, 1, 2)],
            coordinate_system="EPSG:4548",
            mesh_id="synthetic-quality-job-mesh",
            face_id=[9007199254740993],
            boundary_groups={"synthetic_boundary": [(0, 1)]},
            geometry_tolerance_m=1e-8,
            minimum_face_area_m2=1e-12,
        )
        quality = mesh.quality_report(minimum_quality=0.1)
        mesh_bytes = dm._canonical_json_bytes({
            "format": "synthetic-mesh-payload-v1",
            "node_xy": mesh.node_xy,
            "face_nodes": mesh.face_nodes,
            "face_id": mesh.face_id,
        })
        primary = dm._quality_failure_primary_record(
            mesh, quality,
            run_id=bindings["run_id"],
            config_sha256=bindings["config_sha256"],
            source_manifest_sha256=bindings["source_manifest_sha256"],
            route_code_sha256=dm._quality_failure_code_identity_sha256(bindings["code_hashes"]),
            mesh_sha256=hashlib.sha256(mesh_bytes).hexdigest(),
            canonical_mesh_bytes=len(mesh_bytes),
        )
        scratch_error = dm._persist_quality_failure_primary(
            stage, primary, scratch_budget_bytes=16 * 1024 * 1024,
        )
        diagnostic = dm._build_quality_failure_diagnostic(mesh, quality, primary)
        reference, diagnostic_error = dm._persist_quality_failure_diagnostic(
            stage, diagnostic, output_budget_bytes=1024 * 1024 * 1024,
        )
        errors = [item for item in (scratch_error, diagnostic_error) if item is not None]
        failure = dm._make_quality_failure_exception(primary, reference, errors)
        result = {
            "format": "triangle-preparation-worker-result-v1",
            "status": "failed",
            "category": "worker_failed",
            "message": str(failure),
            "worker_exit_code": 53,
            "process_id": os.getpid(),
            "quality_failure": failure.quality_failure,
            "failure_diagnostic": failure.failure_diagnostic,
            "failure_diagnostic_error": failure.failure_diagnostic_error,
        }
        _write_no_clobber_bytes(
            stage / "artifacts" / "worker-result.json",
            _json_bytes(result),
            output_budget=1024 * 1024 * 1024,
            root=stage,
            reserve_bytes=96 * 1024,
        )
        raise SystemExit(53)
    """), encoding="utf-8")
    environment = os.environ.copy()
    previous = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(root) + (os.pathsep + previous if previous else "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    state = restricted_worker._WorkerStartState(mesh_only_route=True)
    receipt = restricted_worker._run_contained_command(
        [sys.executable, "-B", str(script), str(stage), json.dumps(bindings, separators=(",", ":"))],
        cwd=tmp_path,
        environment=environment,
        memory_limit_mib=2048,
        wall_clock_limit_s=900,
        scratch_directory=scratch,
        output_directory=output,
        scratch_budget_bytes=16 * 1024 * 1024,
        output_budget_bytes=1024 * 1024 * 1024,
        worker_start_state=state,
    )
    if receipt["root_exit_code"] != 53:
        error_path = scratch / "synthetic-worker-traceback.txt"
        detail = error_path.read_text(encoding="utf-8") if error_path.exists() else "no child traceback file"
        raise AssertionError(f"synthetic Job root exit={receipt['root_exit_code']}: {detail}")
    assert receipt["job_list_assigned_at_create"] is True
    assert receipt["process_in_job_before_resume"] is True
    assert receipt["job_active_processes_at_exit"] == 0
    assert state.ever_started is True
    worker_result = json.loads((stage / "artifacts" / "worker-result.json").read_text(encoding="utf-8"))
    assert worker_result["quality_failure"]["failure_code"] == "MINIMUM_FACE_QUALITY_FAILED"
    failed = output / "synthetic-quality-job.failed"
    finished = restricted_worker._finish_failure(
        stage,
        failed,
        run_id=bindings["run_id"],
        status="worker_failed",
        exit_code=53,
        message=worker_result["message"],
        worker_start_state=state,
        config_sha256=bindings["config_sha256"],
        worker_receipt=receipt,
        output_budget=1024 * 1024 * 1024,
        worker_result=worker_result,
    )
    status = json.loads((failed / "status.json").read_text(encoding="utf-8"))
    diagnostic_path = failed / status["failure_diagnostic"]["path"]
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert finished.exit_code == 53
    assert status["mesh_ready"] is False and diagnostic["diagnostic_only"] is True
    assert diagnostic["worst_face_selection"]["faces"][0]["face_id_decimal"] == "9007199254740993"
    assert not any((failed / marker).exists() for marker in ("MESH_READY", "READY", "latest"))
    (tmp_path / "synthetic_job_receipt.json").write_text(json.dumps({
        "format": "synthetic-mesh-quality-diagnostic-job-receipt-v1",
        "worker_exit_code": receipt["root_exit_code"],
        "job_active_processes_at_exit": receipt["job_active_processes_at_exit"],
        "job_list_assigned_at_create": receipt["job_list_assigned_at_create"],
        "process_in_job_before_resume": receipt["process_in_job_before_resume"],
        "limit_installed_before_resume": receipt["limit_installed_before_resume"],
        "breakaway_allowed": receipt["breakaway_allowed"],
        "kill_on_job_close": receipt["kill_on_job_close"],
        "job_memory_limit_bytes": receipt["job_memory_limit_bytes"],
        "wall_clock_limit_s": receipt["wall_clock_limit_s"],
        "output_budget_bytes": receipt["output_budget_bytes"],
        "scratch_budget_bytes": receipt["scratch_budget_bytes"],
        "worker_elapsed_seconds": receipt["elapsed_seconds"],
        "job_peak_commit_bytes": receipt["job_peak_commit_bytes"],
        "output_peak_bytes": receipt["output_peak_bytes"],
        "scratch_peak_bytes": receipt["scratch_peak_bytes"],
        "diagnostic_only": diagnostic["diagnostic_only"],
        "mesh_ready": status["mesh_ready"],
        "diagnostic_path": status["failure_diagnostic"]["path"],
        "diagnostic_sha256": status["failure_diagnostic"]["sha256"],
        "face_count": diagnostic["mesh"]["face_count"],
        "node_count": diagnostic["mesh"]["node_count"],
    }, sort_keys=True), encoding="utf-8")
    with pytest.raises(domain_mesh_only.DomainMeshOnlyError, match="marker, manifest and status"):
        domain_mesh_only.read_domain_mesh_bundle_identity(
            failed,
            expected_config_sha256=bindings["config_sha256"],
            expected_source_manifest_sha256=bindings["source_manifest_sha256"],
        )
