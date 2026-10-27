from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import tempfile
from typing import Any

import pytest

from hydrodynamics.prepared_terrain import (
    PreparedTerrainError,
    build_prepared_manifest,
    face_id_sequence_sha256,
    read_prepared_terrain,
    write_prepared_face_artifacts,
)
from hydrodynamics.restricted_worker import run_restricted_preparation


ROOT = Path(__file__).resolve().parents[1]


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    raw = path.read_bytes()
    return _hash_bytes(raw), len(raw)


def _make_real_synthetic_bundle(temp_root: Path) -> Path:
    generator = runpy.run_path(str(ROOT / "examples/triangular_mesh/create_restricted_worker_smallcase.py"))
    config_path = generator["create_case"](ROOT, temp_root.relative_to(ROOT) / "case")
    result = run_restricted_preparation(config_path, project_root=ROOT)
    assert result.status == "preparation_diagnostic_complete", result
    assert result.bundle_path is not None
    return result.bundle_path


def _copy_bundle(bundle: Path, temp_root: Path, label: str) -> Path:
    target_parent = temp_root / label
    target_parent.mkdir()
    target = target_parent / bundle.name
    shutil.copytree(bundle, target)
    return target


def _reseal_codec_copy(bundle: Path, *, rows: list[dict[str, Any]] | None = None,
                       update_mesh_sequence: bool = False,
                       worker_receipt_update: dict[str, Any] | None = None,
                       manifest_mesh_sequence: str | None = None) -> None:
    artifacts = bundle / "artifacts"
    body_path = artifacts / "prepared_body.json"
    faces_path = artifacts / "prepared_faces.jsonl"
    worker_path = artifacts / "worker-result.json"
    body = json.loads(body_path.read_text(encoding="utf-8"))
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    worker = json.loads(worker_path.read_text(encoding="utf-8"))

    if rows is not None:
        row_bytes = b"".join(_canonical(row) for row in rows)
        faces_path.write_bytes(row_bytes)
        sequence_sha = face_id_sequence_sha256(row["face_id"] for row in rows)
        face_info = {"name": "prepared_faces.jsonl", "sha256": _hash_bytes(row_bytes),
                     "bytes": len(row_bytes), "face_count": len(rows),
                     "face_id_sequence_sha256": sequence_sha}
        body["prepared_face_file"] = face_info
        body["bindings"]["terrain_sha256"] = face_info["sha256"]
        if update_mesh_sequence:
            body["mesh"]["face_id_sequence_sha256"] = sequence_sha
            body["bindings"]["mesh_face_id_sequence_sha256"] = sequence_sha
        body_bytes = _canonical(body)
        body_path.write_bytes(body_bytes)
        body_info = {"name": "prepared_body.json", "sha256": _hash_bytes(body_bytes), "bytes": len(body_bytes)}
        worker["prepared_files"] = {"prepared_face_file": face_info, "prepared_body_file": body_info,
                                    "output_bytes_written": face_info["bytes"] + body_info["bytes"]}
        worker_path.write_bytes(_canonical(worker))
        manifest["prepared_face_file"] = face_info
        manifest["prepared_body_file"] = body_info
        manifest["files"] = {
            "prepared_face_file": face_info,
            "prepared_body_file": body_info,
            "worker_result_file": {"name": "worker-result.json", **dict(zip(("sha256", "bytes"), _hash_file(worker_path)))},
        }
        manifest["bindings"] = body["bindings"]
        manifest["mesh"] = body["mesh"]

    if worker_receipt_update is not None:
        manifest["worker"].update(worker_receipt_update)
        status_path = bundle / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["worker"] = manifest["worker"]
        status_path.write_bytes(_canonical(status))
    if manifest_mesh_sequence is not None:
        manifest["mesh"]["face_id_sequence_sha256"] = manifest_mesh_sequence

    manifest_bytes = _canonical(manifest)
    manifest_path.write_bytes(manifest_bytes)
    ready = {"format": "triangle-prepared-ready-v1", "manifest_sha256": _hash_bytes(manifest_bytes),
             "run_id": manifest["run_id"], "simulation_result": False, "production_approved": False}
    (bundle / "READY").write_bytes(_canonical(ready))


@pytest.mark.skipif(os.name != "nt", reason="The source bundle is produced by the Windows Job Object worker")
def test_prepared_writer_and_reader_share_face_cross_field_contract() -> None:
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prepared-codec-r1-", dir=work) as name:
        temp_root = Path(name)
        bundle = _make_real_synthetic_bundle(temp_root)
        body = json.loads((bundle / "artifacts/prepared_body.json").read_text(encoding="utf-8"))
        source_rows = [json.loads(line) for line in (bundle / "artifacts/prepared_faces.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(source_rows) == 2

        ratio_rows = [dict(row) for row in source_rows]
        ratio_rows[0]["coverage_ratio"] = 0.125
        partition_rows = [dict(row) for row in source_rows]
        partition_rows[0]["nodata_area_m2"] += 0.25
        duplicate_rows = [dict(row) for row in source_rows]
        duplicate_rows[1]["face_id"] = duplicate_rows[0]["face_id"]

        for label, rows in (("ratio", ratio_rows), ("partition", partition_rows), ("duplicate", duplicate_rows)):
            writer_dir = temp_root / f"writer-{label}"
            with pytest.raises(PreparedTerrainError):
                write_prepared_face_artifacts(writer_dir, body=body, face_rows=rows,
                                              output_budget_bytes=1_073_741_824)

        for label, rows, update_sequence in (("ratio", ratio_rows, False),
                                             ("partition", partition_rows, False),
                                             ("duplicate", duplicate_rows, True)):
            copied = _copy_bundle(bundle, temp_root, f"reader-{label}")
            _reseal_codec_copy(copied, rows=rows, update_mesh_sequence=update_sequence)
            with pytest.raises(PreparedTerrainError):
                read_prepared_terrain(copied)

        mismatch = _copy_bundle(bundle, temp_root, "manifest-sequence")
        _reseal_codec_copy(mismatch, manifest_mesh_sequence="0" * 64)
        with pytest.raises(PreparedTerrainError):
            read_prepared_terrain(mismatch)


@pytest.mark.skipif(os.name != "nt", reason="The source bundle is produced by the Windows Job Object worker")
def test_prepared_builder_and_reader_reject_failed_worker_receipt() -> None:
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prepared-exit-r1-", dir=work) as name:
        temp_root = Path(name)
        bundle = _make_real_synthetic_bundle(temp_root)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        body = json.loads((bundle / "artifacts/prepared_body.json").read_text(encoding="utf-8"))
        bad_receipt = dict(manifest["worker"], termination_reason="timed_out", root_exit_code=43)
        with pytest.raises(PreparedTerrainError):
            build_prepared_manifest(run_id=manifest["run_id"], prepared_body=body,
                                    files=manifest["files"], worker_receipt=bad_receipt)
        copied = _copy_bundle(bundle, temp_root, "timed-out-worker")
        _reseal_codec_copy(copied, worker_receipt_update={"termination_reason": "timed_out", "root_exit_code": 43})
        with pytest.raises(PreparedTerrainError):
            read_prepared_terrain(copied)
