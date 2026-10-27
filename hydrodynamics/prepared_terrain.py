"""Versioned preparation-only face terrain writer and identity reader.

This format is deliberately separate from native simulation-result bundles.
The reader verifies only the stored manifest and face rows; it never opens a
DEM, reads a source window, or reruns terrain mapping.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import struct
from typing import Any, Callable, Iterable, Iterator, Mapping
import uuid


class PreparedTerrainError(ValueError):
    """A prepared terrain artifact is malformed or has the wrong identity."""


class PreparedOutputBudgetExceeded(PreparedTerrainError):
    """A prepared artifact would exceed its explicit output-byte budget."""


class PreparedTerrainWriteError(PreparedTerrainError):
    """A no-clobber or filesystem write operation failed."""


class PreparedTerrainStopped(PreparedTerrainError):
    """Readback was cancelled or reached its caller's total wall-clock limit."""

    def __init__(self, status: str) -> None:
        if status not in {"cancelled", "timed_out"}:
            raise ValueError("prepared terrain stop status must be cancelled or timed_out")
        self.status = status
        super().__init__(f"prepared readback stopped: {status}")


_ROW_KEYS = {
    "face_id", "face_area_m2", "covered_area_m2", "valid_area_m2", "nodata_area_m2",
    "nonfinite_area_m2", "masked_other_area_m2", "ignored_in_raster_area_m2",
    "ignored_valid_area_m2", "outside_raster_area_m2", "coverage_ratio", "elevation_m",
    "valid",
}
_MANIFEST_FORMAT = "triangle-prepared-terrain-b0-v1"
_MAX_MANIFEST_BYTES = 64 * 1024


def face_id_sequence_sha256(face_ids: Iterable[int]) -> str:
    digest = sha256()
    count = 0
    for value in face_ids:
        if type(value) is not int or value < 0 or value > (1 << 63) - 1:
            raise PreparedTerrainError("face IDs must be non-negative strict int64 values")
        digest.update(struct.pack(">q", value))
        count += 1
    if count == 0:
        raise PreparedTerrainError("at least one face ID is required")
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PreparedTerrainError(f"prepared JSON contains an unsupported or non-finite value: {exc}") from exc


def _validate_face_row(row: Mapping[str, Any], line_number: int, *,
                       area_abs_tolerance_m2: float, area_rel_tolerance: float) -> None:
    if set(row) != _ROW_KEYS:
        raise PreparedTerrainError(f"prepared face row {line_number} keys differ from the frozen format")
    if type(row.get("face_id")) is not int or row["face_id"] < 0 or row["face_id"] > (1 << 63) - 1:
        raise PreparedTerrainError(f"prepared face row {line_number} has an invalid strict int64 face_id")
    if type(row.get("valid")) is not bool:
        raise PreparedTerrainError(f"prepared face row {line_number} valid field must be boolean")
    for key in _ROW_KEYS - {"face_id", "valid"}:
        value = row[key]
        if key == "elevation_m" and value is None:
            if row["valid"]:
                raise PreparedTerrainError(f"prepared face row {line_number} is valid but has no elevation")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise PreparedTerrainError(f"prepared face row {line_number} {key} must be finite numeric data")
    if row["valid"] != (row["elevation_m"] is not None):
        raise PreparedTerrainError(f"prepared face row {line_number} valid state differs from elevation presence")
    face_area = float(row["face_area_m2"])
    area_tolerance = max(area_abs_tolerance_m2, face_area * area_rel_tolerance)
    if face_area <= 0.0 or not 0.0 <= float(row["coverage_ratio"]) <= 1.0 + 1e-9:
        raise PreparedTerrainError(f"prepared face row {line_number} has invalid area or coverage ratio")
    area_fields = ("covered_area_m2", "valid_area_m2", "nodata_area_m2", "nonfinite_area_m2",
                   "masked_other_area_m2", "ignored_in_raster_area_m2", "ignored_valid_area_m2",
                   "outside_raster_area_m2")
    if any(float(row[name]) < -1e-9 for name in area_fields):
        raise PreparedTerrainError(f"prepared face row {line_number} has a negative area")
    covered = float(row["covered_area_m2"])
    valid = float(row["valid_area_m2"])
    nodata = float(row["nodata_area_m2"])
    nonfinite = float(row["nonfinite_area_m2"])
    masked_other = float(row["masked_other_area_m2"])
    ignored = float(row["ignored_in_raster_area_m2"])
    ignored_valid = float(row["ignored_valid_area_m2"])
    outside = float(row["outside_raster_area_m2"])
    if covered > face_area + area_tolerance:
        raise PreparedTerrainError(f"prepared face row {line_number} covered area exceeds face area")
    if valid > covered + area_tolerance:
        raise PreparedTerrainError(f"prepared face row {line_number} valid area exceeds covered area")
    classified = valid + nodata + nonfinite + masked_other
    if abs(classified - covered) > area_tolerance:
        raise PreparedTerrainError(f"prepared face row {line_number} coverage classes do not partition covered area")
    if abs((covered + ignored + outside) - face_area) > area_tolerance:
        raise PreparedTerrainError(f"prepared face row {line_number} raster/ignored/outside areas do not partition face area")
    if ignored_valid > ignored + area_tolerance:
        raise PreparedTerrainError(f"prepared face row {line_number} ignored valid area exceeds ignored overlap")
    expected_ratio = min(1.0, valid / face_area)
    ratio_tolerance = max(1e-9, area_tolerance / face_area)
    if abs(float(row["coverage_ratio"]) - expected_ratio) > ratio_tolerance:
        raise PreparedTerrainError(f"prepared face row {line_number} coverage ratio differs from valid/native area")
    if row["valid"] != (valid > 0.0):
        raise PreparedTerrainError(f"prepared face row {line_number} valid state differs from valid area")


def _abort_status(abort_check: Callable[[], str | None] | None) -> None:
    if abort_check is None:
        return
    status = abort_check()
    if status is not None:
        raise PreparedTerrainStopped(status)


def _validate_area_totals(observed: Mapping[str, float], expected: Mapping[str, Any], *,
                          face_count: int, absolute_tolerance: float,
                          relative_tolerance: float) -> None:
    for observed_name, expected_name in (("native", "native"), ("covered", "covered"), ("valid", "valid")):
        actual = observed[observed_name]
        wanted = expected.get(expected_name)
        if isinstance(wanted, bool) or not isinstance(wanted, (int, float)) or not math.isfinite(float(wanted)):
            raise PreparedTerrainError(f"prepared aggregate {expected_name} area is missing or non-finite")
        if not math.isclose(actual, float(wanted), rel_tol=relative_tolerance * 4,
                            abs_tol=absolute_tolerance * max(face_count, 1)):
            raise PreparedTerrainError(f"prepared face rows do not sum to stored {expected_name} area")
    if observed["valid"] > observed["covered"] + absolute_tolerance * max(face_count, 1):
        raise PreparedTerrainError("prepared aggregate valid area exceeds covered area")
    if observed["covered"] > observed["native"] + absolute_tolerance * max(face_count, 1):
        raise PreparedTerrainError("prepared aggregate covered area exceeds native mesh area")


def _sha256_file(path: Path, *, abort_check: Callable[[], str | None] | None = None) -> tuple[str, int]:
    digest = sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            _abort_status(abort_check)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _write_no_clobber(path: Path, chunks: Iterable[bytes], *, budget_limit: int,
                      used_bytes: int, reserve_bytes: int) -> tuple[int, str, int]:
    if path.exists():
        raise PreparedTerrainWriteError(f"prepared output target already exists: {path.name}")
    temporary = path.with_name(f".p-{uuid.uuid4().hex[:12]}.tmp")
    digest = sha256()
    written = 0
    try:
        with temporary.open("xb") as stream:
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("prepared writer chunks must be bytes")
                if used_bytes + written + len(chunk) + reserve_bytes > budget_limit:
                    raise PreparedOutputBudgetExceeded(
                        f"output budget would be exceeded before writing {path.name}"
                    )
                stream.write(chunk)
                digest.update(chunk)
                written += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link creation is atomic and fails if another writer created the
        # destination.  The temporary and final names refer to one file until
        # unlink, so this does not duplicate allocated file data.
        os.link(temporary, path)
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return used_bytes + written, digest.hexdigest(), written


def write_prepared_face_artifacts(
    output_directory: str | Path,
    *,
    body: Mapping[str, Any],
    face_rows: Iterable[Mapping[str, Any]],
    output_budget_bytes: int,
    reserved_bytes: int = _MAX_MANIFEST_BYTES + 16 * 1024,
    starting_output_bytes: int = 0,
) -> dict[str, Any]:
    """Write bounded JSONL face terrain rows plus their immutable body.

    The caller reserves space for the supervisor-owned manifest/status/READY
    files.  Every row is serialized and charged before its bytes are written.
    """
    if type(output_budget_bytes) is not int or output_budget_bytes < 1:
        raise PreparedOutputBudgetExceeded("output_budget_bytes must be a strict positive integer")
    if type(reserved_bytes) is not int or reserved_bytes < 0 or reserved_bytes >= output_budget_bytes:
        raise PreparedOutputBudgetExceeded("output manifest reserve is invalid for the configured output budget")
    if type(starting_output_bytes) is not int or starting_output_bytes < 0:
        raise PreparedOutputBudgetExceeded("starting_output_bytes must be a strict non-negative integer")
    root = Path(output_directory)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise PreparedTerrainError("prepared artifacts path must be a regular directory")

    body_value = dict(body)
    if body_value.get("format") != _MANIFEST_FORMAT:
        raise PreparedTerrainError("prepared terrain body format is unsupported")
    if (body_value.get("preparation_only") is not True or body_value.get("simulation_result") is not False
            or body_value.get("production_approved") is not False or body_value.get("engineering_ready") is not False):
        raise PreparedTerrainError("prepared terrain must remain preparation-only and non-production")
    face_count = body_value.get("face_count")
    if type(face_count) is not int or face_count <= 0 or face_count > 500_000:
        raise PreparedTerrainError("prepared terrain face_count must be a strict positive integer")
    mesh_identity = body_value.get("mesh")
    if (not isinstance(mesh_identity, dict) or mesh_identity.get("face_count") != face_count
            or type(mesh_identity.get("face_id_sequence_sha256")) is not str
            or len(mesh_identity["face_id_sequence_sha256"]) != 64):
        raise PreparedTerrainError("prepared body mesh face count/sequence identity is missing or inconsistent")
    mapping = body_value.get("mapping")
    mapping_config = mapping.get("config") if isinstance(mapping, dict) else None
    if not isinstance(mapping_config, dict):
        raise PreparedTerrainError("prepared body mapping configuration is missing")
    area_abs_tolerance = mapping_config.get("area_abs_tolerance_m2")
    area_rel_tolerance = mapping_config.get("area_rel_tolerance")
    for value, name in ((area_abs_tolerance, "area_abs_tolerance_m2"), (area_rel_tolerance, "area_rel_tolerance")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
            raise PreparedTerrainError(f"prepared mapping {name} must be finite and positive")
    area_abs_tolerance = float(area_abs_tolerance)
    area_rel_tolerance = float(area_rel_tolerance)

    id_digest = sha256()
    seen_ids: set[int] = set()
    row_count = 0
    observed_totals = {"native": 0.0, "covered": 0.0, "valid": 0.0}

    def encoded_rows() -> Iterator[bytes]:
        nonlocal row_count
        for row in face_rows:
            _validate_face_row(row, row_count + 1, area_abs_tolerance_m2=area_abs_tolerance,
                               area_rel_tolerance=area_rel_tolerance)
            if row["face_id"] in seen_ids:
                raise PreparedTerrainError(f"prepared face row {row_count + 1} duplicates face_id={row['face_id']}")
            seen_ids.add(row["face_id"])
            observed_totals["native"] += float(row["face_area_m2"])
            observed_totals["covered"] += float(row["covered_area_m2"])
            observed_totals["valid"] += float(row["valid_area_m2"])
            payload = _canonical_bytes(dict(row))
            id_digest.update(struct.pack(">q", row["face_id"]))
            row_count += 1
            yield payload

    used, rows_sha, rows_bytes = _write_no_clobber(
        root / "prepared_faces.jsonl", encoded_rows(), budget_limit=output_budget_bytes,
        used_bytes=starting_output_bytes, reserve_bytes=reserved_bytes,
    )
    if row_count != face_count:
        raise PreparedTerrainError(f"prepared row count {row_count} differs from face_count={face_count}")
    if id_digest.hexdigest() != mesh_identity["face_id_sequence_sha256"]:
        raise PreparedTerrainError("prepared face ID sequence differs from the bound mesh sequence")
    mapping_totals = mapping.get("area_totals_m2")
    if not isinstance(mapping_totals, Mapping):
        raise PreparedTerrainError("prepared body mapping area totals are missing")
    _validate_area_totals(observed_totals, mapping_totals, face_count=face_count,
                          absolute_tolerance=area_abs_tolerance, relative_tolerance=area_rel_tolerance)
    file_record = {
        "name": "prepared_faces.jsonl", "sha256": rows_sha, "bytes": rows_bytes,
        "face_count": row_count, "face_id_sequence_sha256": id_digest.hexdigest(),
    }
    body_value["prepared_face_file"] = file_record
    bindings = body_value.setdefault("bindings", {})
    if not isinstance(bindings, dict):
        raise PreparedTerrainError("prepared bindings must be an object")
    bindings["terrain_sha256"] = rows_sha
    body_payload = _canonical_bytes(body_value)
    _, body_sha, body_bytes = _write_no_clobber(
        root / "prepared_body.json", (body_payload,), budget_limit=output_budget_bytes,
        used_bytes=used, reserve_bytes=reserved_bytes,
    )
    return {
        "prepared_face_file": file_record,
        "prepared_body_file": {"name": "prepared_body.json", "sha256": body_sha, "bytes": body_bytes},
        "output_bytes_written": used + body_bytes,
    }


def build_prepared_manifest(*, run_id: str, prepared_body: Mapping[str, Any],
                            files: Mapping[str, Any], worker_receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Build the supervisor-owned manifest after a worker has fully exited."""
    body = dict(prepared_body)
    if body.get("format") != _MANIFEST_FORMAT or body.get("run_id") != run_id:
        raise PreparedTerrainError("prepared body run identity differs from the supervisor request")
    if (body.get("preparation_only") is not True or body.get("simulation_result") is not False
            or body.get("production_approved") is not False or body.get("engineering_ready") is not False):
        raise PreparedTerrainError("prepared body contains a success/production readiness claim")
    if (worker_receipt.get("os_limit_enforced") is not True
            or worker_receipt.get("max_workers") != 1
            or worker_receipt.get("limit_installed_before_resume") is not True
            or worker_receipt.get("job_list_assigned_at_create") is not True
            or worker_receipt.get("post_create_assignment_window") is not False
            or worker_receipt.get("process_in_job_before_resume") is not True
            or worker_receipt.get("creation_suspended") is not True
            or worker_receipt.get("breakaway_allowed") is not False
            or worker_receipt.get("kill_on_job_close") is not True
            or worker_receipt.get("worker_result_write_failed") is True
            or worker_receipt.get("job_active_processes_at_exit") != 0):
        raise PreparedTerrainError("supervisor did not verify an enforced limit and an empty job at exit")
    if (type(worker_receipt.get("root_exit_code")) is not int or worker_receipt["root_exit_code"] != 0
            or worker_receipt.get("termination_reason") is not None):
        raise PreparedTerrainError("worker exit/termination receipt does not prove successful completion")
    memory_limit = worker_receipt.get("job_memory_limit_bytes")
    process_limit = worker_receipt.get("process_memory_limit_bytes")
    if (type(memory_limit) is not int or memory_limit <= 0 or process_limit != memory_limit
            or not isinstance(worker_receipt.get("memory_limit_messages"), list)
            or any(message in {9, 10} for message in worker_receipt["memory_limit_messages"])):
        raise PreparedTerrainError("worker memory-limit receipt is invalid or records a limit violation")
    process_ids = worker_receipt.get("job_process_ids_observed")
    if (not isinstance(process_ids, list) or not process_ids
            or any(type(pid) is not int or pid <= 0 for pid in process_ids)
            or len(set(process_ids)) != len(process_ids)
            or worker_receipt.get("process_id") not in process_ids):
        raise PreparedTerrainError("worker process tree receipt lacks unique observed process IDs")
    for observed, budget in (("job_peak_commit_bytes", "job_memory_limit_bytes"),
                             ("scratch_peak_bytes", "scratch_budget_bytes"),
                             ("output_peak_bytes", "output_budget_bytes")):
        value, limit = worker_receipt.get(observed), worker_receipt.get(budget)
        if type(value) is not int or type(limit) is not int or value < 0 or value > limit:
            raise PreparedTerrainError(f"supervisor {observed} is missing or exceeds its enforced/configured limit")
    wall_limit = worker_receipt.get("wall_clock_limit_s")
    elapsed = worker_receipt.get("elapsed_seconds")
    if (isinstance(wall_limit, bool) or not isinstance(wall_limit, (int, float))
            or not math.isfinite(float(wall_limit)) or wall_limit <= 0
            or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
            or not math.isfinite(float(elapsed)) or elapsed < 0 or elapsed > wall_limit):
        raise PreparedTerrainError("supervisor worker duration exceeds wall_clock_limit_s")
    if (not isinstance(files, Mapping) or set(files) != {"prepared_face_file", "prepared_body_file", "worker_result_file"}
            or not isinstance(files.get("prepared_face_file"), Mapping)
            or not isinstance(files.get("prepared_body_file"), Mapping)
            or not isinstance(files.get("worker_result_file"), Mapping)):
        raise PreparedTerrainError("prepared manifest file identities are incomplete")
    face_file = files["prepared_face_file"]
    mesh = body.get("mesh")
    body_bindings = body.get("bindings")
    if (face_file.get("face_count") != body.get("face_count")
            or not isinstance(mesh, Mapping)
            or face_file.get("face_count") != mesh.get("face_count")
            or face_file.get("face_id_sequence_sha256") != mesh.get("face_id_sequence_sha256")
            or not isinstance(body_bindings, Mapping)
            or body_bindings.get("mesh_face_id_sequence_sha256") != mesh.get("face_id_sequence_sha256")
            or body.get("prepared_face_file") != face_file):
        raise PreparedTerrainError("prepared face count/sequence differs from mesh/body identity")
    return {
        **body,
        "prepared_body_file": dict(files.get("prepared_body_file", {})),
        "manifest_format": _MANIFEST_FORMAT,
        "manifest_status": "preparation_diagnostic_complete",
        "worker": dict(worker_receipt),
        "files": dict(files),
        "ready": True,
        "latest_published": False,
    }


@dataclass(frozen=True)
class PreparedTerrainReader:
    """A verified, lazy reader over an already prepared face JSONL file."""

    bundle_directory: Path
    manifest: Mapping[str, Any]
    abort_check: Callable[[], str | None] | None = None
    area_abs_tolerance_m2: float = 1e-8
    area_rel_tolerance: float = 1e-10

    @property
    def data_directory(self) -> Path:
        artifacts = self.bundle_directory / "artifacts"
        if artifacts.exists():
            if artifacts.is_symlink() or not artifacts.is_dir():
                raise PreparedTerrainError("prepared artifacts path must be a non-symlink directory")
            return artifacts
        return self.bundle_directory

    def iter_faces(self) -> Iterator[dict[str, Any]]:
        path = self.data_directory / "prepared_faces.jsonl"
        with path.open("rb") as stream:
            for line_number, raw in enumerate(stream, start=1):
                if line_number % 128 == 1:
                    _abort_status(self.abort_check)
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PreparedTerrainError(f"prepared face row {line_number} is not valid JSON") from exc
                if not isinstance(value, dict):
                    raise PreparedTerrainError(f"prepared face row {line_number} has an unsupported shape")
                _validate_face_row(value, line_number,
                                   area_abs_tolerance_m2=self.area_abs_tolerance_m2,
                                   area_rel_tolerance=self.area_rel_tolerance)
                yield value


def read_prepared_terrain(bundle_directory: str | Path, *,
                          expected_bindings: Mapping[str, str] | None = None,
                          allow_pending_marker: bool = False,
                          abort_check: Callable[[], str | None] | None = None) -> PreparedTerrainReader:
    """Verify only stored identities/fields; no DEM or mapping dependency is used."""
    _abort_status(abort_check)
    root = Path(bundle_directory)
    if root.is_symlink() or not root.is_dir():
        raise PreparedTerrainError("prepared bundle path must be a non-symlink directory")
    ready_path = root / "READY"
    pending_ready_path = root / "READY.pending"
    if ready_path.is_file() and not ready_path.is_symlink():
        marker_path = ready_path
        expected_ready_format = "triangle-prepared-ready-v1"
    elif allow_pending_marker and pending_ready_path.is_file() and not pending_ready_path.is_symlink():
        marker_path = pending_ready_path
        expected_ready_format = "triangle-prepared-ready-pending-v1"
    else:
        raise PreparedTerrainError("prepared bundle has no completed supervisor READY marker")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise PreparedTerrainError("prepared manifest must be a regular file no larger than 64 KiB")
    try:
        _abort_status(abort_check)
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedTerrainError(f"prepared manifest could not be read: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("manifest_format") != _MANIFEST_FORMAT:
        raise PreparedTerrainError("prepared manifest format is unsupported")
    _abort_status(abort_check)
    if not allow_pending_marker and root.name != manifest.get("run_id"):
        raise PreparedTerrainError("private pending attempt directories are not consumable by the prepared reader")
    if allow_pending_marker and not root.name.endswith(".pending"):
        raise PreparedTerrainError("pending-marker readback is restricted to private pending directories")
    if manifest.get("manifest_status") != "preparation_diagnostic_complete" or manifest.get("ready") is not True:
        raise PreparedTerrainError("prepared manifest is not a completed preparation diagnostic")
    if (manifest.get("preparation_only") is not True or manifest.get("simulation_result") is not False
            or manifest.get("production_approved") is not False or manifest.get("engineering_ready") is not False
            or manifest.get("latest_published") is not False):
        raise PreparedTerrainError("prepared manifest contains an invalid readiness or result claim")
    worker = manifest.get("worker")
    if (not isinstance(worker, dict) or worker.get("os_limit_enforced") is not True
            or worker.get("max_workers") != 1
            or worker.get("limit_installed_before_resume") is not True
            or worker.get("job_list_assigned_at_create") is not True
            or worker.get("post_create_assignment_window") is not False
            or worker.get("process_in_job_before_resume") is not True
            or worker.get("creation_suspended") is not True or worker.get("breakaway_allowed") is not False
            or worker.get("kill_on_job_close") is not True or worker.get("worker_result_write_failed") is True
            or worker.get("job_active_processes_at_exit") != 0):
        raise PreparedTerrainError("prepared manifest lacks verified worker enforcement/exit evidence")
    if (type(worker.get("root_exit_code")) is not int or worker["root_exit_code"] != 0
            or worker.get("termination_reason") is not None
            or worker.get("process_memory_limit_bytes") != worker.get("job_memory_limit_bytes")
            or not isinstance(worker.get("memory_limit_messages"), list)
            or any(message in {9, 10} for message in worker["memory_limit_messages"])):
        raise PreparedTerrainError("prepared manifest worker receipt records a failed exit or limit violation")
    worker_wall = worker.get("wall_clock_limit_s")
    worker_elapsed = worker.get("elapsed_seconds")
    if (isinstance(worker_wall, bool) or not isinstance(worker_wall, (int, float))
            or not math.isfinite(float(worker_wall)) or worker_wall <= 0
            or isinstance(worker_elapsed, bool) or not isinstance(worker_elapsed, (int, float))
            or not math.isfinite(float(worker_elapsed)) or worker_elapsed < 0 or worker_elapsed > worker_wall):
        raise PreparedTerrainError("prepared worker duration exceeds its recorded wall_clock_limit_s")
    for observed, budget in (("job_peak_commit_bytes", "job_memory_limit_bytes"),
                             ("scratch_peak_bytes", "scratch_budget_bytes"),
                             ("output_peak_bytes", "output_budget_bytes")):
        value, limit = worker.get(observed), worker.get(budget)
        if type(value) is not int or type(limit) is not int or value < 0 or value > limit:
            raise PreparedTerrainError(f"prepared manifest {observed} is missing or exceeds its limit")
    process_ids = worker.get("job_process_ids_observed")
    if (not isinstance(process_ids, list) or not process_ids
            or any(type(pid) is not int or pid <= 0 for pid in process_ids)
            or len(set(process_ids)) != len(process_ids)
            or worker.get("process_id") not in process_ids):
        raise PreparedTerrainError("prepared manifest process-tree receipt is incomplete")
    try:
        ready = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedTerrainError(f"READY marker is malformed: {exc}") from exc
    if (ready.get("format") != expected_ready_format
            or ready.get("manifest_sha256") != sha256(manifest_raw).hexdigest()
            or ready.get("run_id") != manifest.get("run_id")
            or ready.get("simulation_result") is not False
            or ready.get("production_approved") is not False):
        raise PreparedTerrainError("READY marker does not bind the stored preparation manifest")
    status_path = root / "status.json"
    if status_path.is_symlink() or not status_path.is_file() or status_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise PreparedTerrainError("prepared status must be a regular file no larger than 64 KiB")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedTerrainError(f"prepared status JSON is malformed: {exc}") from exc
    pending_status = expected_ready_format == "triangle-prepared-ready-pending-v1"
    expected_status = "prepared_validation_pending_atomic_publish" if pending_status else "preparation_diagnostic_complete"
    expected_status_ready = False if pending_status else True
    if (not isinstance(status, dict) or status.get("status") != expected_status
            or status.get("exit_code") != 0 or status.get("run_id") != manifest.get("run_id")
            or status.get("ready") is not expected_status_ready or status.get("latest_published") is not False
            or status.get("preparation_only") is not True or status.get("simulation_result") is not False
            or status.get("production_approved") is not False or status.get("worker") != worker):
        raise PreparedTerrainError("prepared status does not match the completed non-simulation manifest")
    operation_wall = status.get("operation_wallclock_limit_s")
    operation_elapsed = status.get("operation_elapsed_seconds")
    if (isinstance(operation_wall, bool) or not isinstance(operation_wall, (int, float))
            or not math.isfinite(float(operation_wall)) or operation_wall <= 0
            or isinstance(operation_elapsed, bool) or not isinstance(operation_elapsed, (int, float))
            or not math.isfinite(float(operation_elapsed)) or operation_elapsed < 0
            or operation_elapsed > operation_wall):
        raise PreparedTerrainError("prepared status operation duration is missing or exceeds wall_clock_limit_s")
    if expected_bindings:
        bindings = manifest.get("bindings")
        if not isinstance(bindings, dict):
            raise PreparedTerrainError("prepared manifest bindings are missing")
        for name, digest in expected_bindings.items():
            if bindings.get(name) != digest:
                raise PreparedTerrainError(f"prepared {name} identity differs from the expected binding")

    face_info = manifest.get("prepared_face_file")
    if not isinstance(face_info, dict) or face_info.get("name") != "prepared_faces.jsonl":
        raise PreparedTerrainError("prepared face file record is missing or unsupported")
    file_records = manifest.get("files")
    if not isinstance(file_records, dict) or set(file_records) != {"prepared_face_file", "prepared_body_file", "worker_result_file"}:
        raise PreparedTerrainError("prepared manifest file records are missing or unsupported")
    if file_records.get("prepared_face_file") != face_info or file_records.get("prepared_body_file") != manifest.get("prepared_body_file"):
        raise PreparedTerrainError("prepared manifest file records disagree with their top-level identities")
    data_directory = root / "artifacts" if (root / "artifacts").exists() else root
    if data_directory.is_symlink() or not data_directory.is_dir():
        raise PreparedTerrainError("prepared artifacts path must be a non-symlink directory")
    faces_path = data_directory / "prepared_faces.jsonl"
    if faces_path.is_symlink() or not faces_path.is_file():
        raise PreparedTerrainError("prepared face data file is missing or is a symlink")
    digest, size = _sha256_file(faces_path, abort_check=abort_check)
    if digest != face_info.get("sha256") or size != face_info.get("bytes"):
        raise PreparedTerrainError("prepared face data bytes differ from the stored identity")
    expected_count = face_info.get("face_count")
    if type(expected_count) is not int or expected_count <= 0:
        raise PreparedTerrainError("prepared face count must be a strict positive integer")
    count = 0
    mesh = manifest.get("mesh")
    if (not isinstance(mesh, dict) or type(mesh.get("face_count")) is not int
            or not (1 <= mesh["face_count"] <= 500_000)
            or not isinstance(mesh.get("face_id_sequence_sha256"), str)):
        raise PreparedTerrainError("prepared manifest mesh face identity is missing or outside the frozen cap")
    if expected_count != mesh["face_count"] or expected_count != manifest.get("face_count"):
        raise PreparedTerrainError("prepared face count differs from manifest/mesh face_count")
    if face_info.get("face_id_sequence_sha256") != mesh["face_id_sequence_sha256"]:
        raise PreparedTerrainError("prepared face ID sequence differs from the bound mesh sequence")
    bindings = manifest.get("bindings")
    if (not isinstance(bindings, dict)
            or bindings.get("mesh_face_id_sequence_sha256") != mesh["face_id_sequence_sha256"]
            or bindings.get("mesh_sha256") != mesh.get("sha256")
            or bindings.get("terrain_sha256") != face_info.get("sha256")
            or bindings.get("mesh_fingerprint_sha256") != mesh.get("mesh_fingerprint_sha256")):
        raise PreparedTerrainError("prepared manifest mesh/terrain identity differs from its bindings")
    mapping = manifest.get("mapping")
    mapping_config = mapping.get("config") if isinstance(mapping, dict) else None
    if not isinstance(mapping_config, dict):
        raise PreparedTerrainError("prepared manifest mapping config is missing")
    area_abs_tolerance = mapping_config.get("area_abs_tolerance_m2", 1e-8)
    area_rel_tolerance = mapping_config.get("area_rel_tolerance", 1e-10)
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)) or x <= 0
           for x in (area_abs_tolerance, area_rel_tolerance)):
        raise PreparedTerrainError("prepared area tolerances must be finite and positive")
    _abort_status(abort_check)
    id_digest = sha256()
    seen_ids: set[int] = set()
    observed_totals = {"native": 0.0, "covered": 0.0, "valid": 0.0}
    for row in PreparedTerrainReader(root, manifest, abort_check,
                                     float(area_abs_tolerance), float(area_rel_tolerance)).iter_faces():
        face_id = row.get("face_id")
        if type(face_id) is not int or face_id < 0 or face_id > (1 << 63) - 1:
            raise PreparedTerrainError(f"prepared face row {count + 1} has an invalid face_id")
        if face_id in seen_ids:
            raise PreparedTerrainError(f"prepared face row {count + 1} duplicates face_id={face_id}")
        seen_ids.add(face_id)
        observed_totals["native"] += float(row["face_area_m2"])
        observed_totals["covered"] += float(row["covered_area_m2"])
        observed_totals["valid"] += float(row["valid_area_m2"])
        id_digest.update(struct.pack(">q", face_id))
        count += 1
    if count != expected_count:
        raise PreparedTerrainError("prepared face count differs from stored record")
    if id_digest.hexdigest() != face_info.get("face_id_sequence_sha256"):
        raise PreparedTerrainError("prepared face ID sequence differs from its stored identity")
    body_info = manifest.get("prepared_body_file")
    body_path = data_directory / "prepared_body.json"
    if (not isinstance(body_info, dict) or body_info.get("name") != "prepared_body.json"
            or body_path.is_symlink() or not body_path.is_file()):
        raise PreparedTerrainError("prepared body file record is missing or unsupported")
    body_sha, body_bytes = _sha256_file(body_path, abort_check=abort_check)
    if body_sha != body_info.get("sha256") or body_bytes != body_info.get("bytes"):
        raise PreparedTerrainError("prepared body bytes differ from the stored identity")
    worker_info = file_records.get("worker_result_file")
    worker_path = data_directory / "worker-result.json"
    if (not isinstance(worker_info, dict) or worker_info.get("name") != "worker-result.json"
            or worker_path.is_symlink() or not worker_path.is_file()):
        raise PreparedTerrainError("worker result receipt file record is missing")
    worker_sha, worker_bytes = _sha256_file(worker_path, abort_check=abort_check)
    if worker_sha != worker_info.get("sha256") or worker_bytes != worker_info.get("bytes"):
        raise PreparedTerrainError("worker result receipt bytes differ from the stored identity")
    _abort_status(abort_check)
    try:
        worker_result = json.loads(worker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedTerrainError(f"worker result receipt JSON is malformed: {exc}") from exc
    if (not isinstance(worker_result, dict) or worker_result.get("format") != "triangle-preparation-worker-result-v1"
            or worker_result.get("status") != "success"
            or worker_result.get("category") != "preparation_diagnostic_complete"
            or worker_result.get("worker_exit_code") != 0):
        raise PreparedTerrainError("worker result receipt is not a successful preparation diagnostic")
    prepared_receipt_files = worker_result.get("prepared_files")
    if (not isinstance(prepared_receipt_files, dict)
            or prepared_receipt_files.get("prepared_face_file") != file_records.get("prepared_face_file")
            or prepared_receipt_files.get("prepared_body_file") != file_records.get("prepared_body_file")
            or type(prepared_receipt_files.get("output_bytes_written")) is not int
            or prepared_receipt_files["output_bytes_written"] > worker.get("output_budget_bytes", -1)):
        raise PreparedTerrainError("worker result file/byte receipt differs from the supervisor manifest")
    _abort_status(abort_check)
    try:
        body = json.loads(body_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedTerrainError(f"prepared body JSON is malformed: {exc}") from exc
    if (not isinstance(body, dict) or body.get("format") != _MANIFEST_FORMAT
            or body.get("run_id") != manifest.get("run_id")
            or body.get("bindings") != manifest.get("bindings")
            or body.get("mesh") != manifest.get("mesh")
            or body.get("face_count") != manifest.get("face_count")
            or body.get("prepared_face_file") != manifest.get("prepared_face_file")
            or body.get("preparation_only") is not True or body.get("simulation_result") is not False
            or body.get("production_approved") is not False or body.get("engineering_ready") is not False):
        raise PreparedTerrainError("prepared body fields differ from the supervisor manifest")
    body_mapping = body.get("mapping")
    body_totals = body_mapping.get("area_totals_m2") if isinstance(body_mapping, dict) else None
    if not isinstance(body_totals, dict):
        raise PreparedTerrainError("prepared body aggregate area totals are missing")
    _validate_area_totals(observed_totals, body_totals, face_count=expected_count,
                          absolute_tolerance=float(area_abs_tolerance), relative_tolerance=float(area_rel_tolerance))
    _abort_status(abort_check)
    source_record = body.get("source")
    source_metadata = source_record.get("metadata") if isinstance(source_record, dict) else None
    unit_source = source_metadata.get("vertical_unit_source") if isinstance(source_metadata, dict) else None
    raw_unit = source_metadata.get("vertical_unit") if isinstance(source_metadata, dict) else None
    unit_declaration = mapping_config.get("source_vertical_unit_declaration")
    unit_evidence = source_record.get("source_vertical_unit_evidence") if isinstance(source_record, dict) else None
    if unit_source == "dem_metadata":
        if (raw_unit is None or str(raw_unit).strip().casefold() not in {"m", "meter", "meters", "metre", "metres"}
                or source_record.get("terrain_vertical_unit_status") != "source_metadata_metre"):
            raise PreparedTerrainError("source metadata unit provenance is missing, non-metric, or mislabeled")
    elif unit_source == "config_declaration":
        if (raw_unit is not None or unit_declaration != "m"
                or not isinstance(unit_evidence, dict)
                or unit_evidence.get("sha256") != mapping_config.get("source_vertical_unit_evidence_sha256")
                or source_record.get("terrain_vertical_unit_status") != "source_declared_metre_with_verified_evidence"):
            raise PreparedTerrainError("declared metre unit is not bound to its verified evidence")
    else:
        raise PreparedTerrainError("prepared terrain lacks an accurate vertical-unit source")

    datum_source = source_metadata.get("vertical_datum_source") if isinstance(source_metadata, dict) else None
    raw_datum = source_metadata.get("vertical_datum") if isinstance(source_metadata, dict) else None
    datum_declaration = mapping_config.get("source_vertical_datum_declaration")
    datum_evidence = source_record.get("source_vertical_datum_evidence") if isinstance(source_record, dict) else None
    datum_status = source_record.get("terrain_source_vertical_datum_status") if isinstance(source_record, dict) else None
    if raw_datum is not None:
        if datum_source != "dem_metadata" or datum_status != "source_metadata_datum":
            raise PreparedTerrainError("non-null source datum is not identified as raster metadata")
    elif datum_declaration is not None:
        if (datum_source != "config_declaration" or not isinstance(datum_evidence, dict)
                or datum_evidence.get("sha256") != mapping_config.get("source_vertical_datum_evidence_sha256")
                or datum_status != "source_declared_datum_with_verified_evidence"):
            raise PreparedTerrainError("declared source datum is not bound to its verified evidence")
    elif datum_source is not None or datum_status != "unknown_source_datum_unattested":
        raise PreparedTerrainError("unknown raw source datum is mislabeled as attested")
    relation = source_record.get("vertical_relation_context") if isinstance(source_record, dict) else None
    if (not isinstance(relation, dict) or relation.get("applied") is not False
            or relation.get("proves_dem_source_datum") is not False
            or source_record.get("terrain_elevation_adjustment_m") != 0.0):
        raise PreparedTerrainError("scoped vertical relation was applied to or used as proof of DEM datum")
    return PreparedTerrainReader(root, manifest)


__all__ = [
    "PreparedOutputBudgetExceeded", "PreparedTerrainError", "PreparedTerrainReader",
    "PreparedTerrainWriteError", "build_prepared_manifest", "face_id_sequence_sha256", "read_prepared_terrain",
    "write_prepared_face_artifacts",
]
