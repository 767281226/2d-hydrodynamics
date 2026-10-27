"""Create a bounded 256x256 synthetic input case for the M2-B0 worker.

The generated files stay in the caller-selected case directory.  This helper
does not start a worker; use ``python -m hydrodynamics.restricted_worker``
after reviewing the generated config.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
from typing import Any

import numpy as np
import rasterio
from rasterio.env import Env
from rasterio.transform import from_origin


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_identity(path: Path) -> dict[str, int]:
    value = path.stat()
    return {"size_bytes": int(value.st_size), "mtime_ns": int(value.st_mtime_ns),
            "ctime_ns": int(value.st_ctime_ns), "device": int(value.st_dev),
            "file_id": int(value.st_ino)}


def _write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def create_case(project_root: Path, case_directory: Path) -> Path:
    project_root = project_root.resolve()
    if case_directory.is_absolute() or PureWindowsPath(str(case_directory)).is_absolute() or ".." in case_directory.parts:
        raise ValueError("case_directory must be relative to project_root and may not use parent traversal")
    case_root = (project_root / case_directory).resolve(strict=False)
    if not case_root.is_relative_to(project_root):
        raise ValueError("case_directory escapes project_root")
    case_root.mkdir(parents=True, exist_ok=False)
    raster_path = case_root / "source_256.tif"
    mesh_path = case_root / "mesh.json"
    source_manifest_path = case_root / "source_manifest.json"
    config_path = case_root / "worker_config.json"

    xmin = 473506.78215896204
    ymax = 4335988.239580579
    with Env(GDAL_PAM_ENABLED="NO"):
        with rasterio.open(
            raster_path, "w", driver="GTiff", width=256, height=256, count=1,
            dtype="float32", nodata=-32767.0,
            transform=from_origin(xmin, ymax, 1.0, 1.0), crs="EPSG:4548",
        ) as dataset:
            values = np.full((256, 256), 3.25, dtype=np.float32)
            dataset.write(values, 1)
            dataset.set_band_unit(1, "metre")

    x0, y0 = xmin, ymax - 1.0
    mesh = {
        "format": "triangle-mesh-xy-json-v1",
        "mesh_id": "b0-synthetic-256x256",
        "coordinate_system": "EPSG:4548",
        "node_xy": [[x0, y0], [x0 + 1.0, y0], [x0 + 1.0, y0 + 1.0], [x0, y0 + 1.0]],
        "face_nodes": [[0, 1, 2], [0, 2, 3]],
        "face_id": [1, 2],
        "geometry_tolerance_m": 1e-8,
        "minimum_face_area_m2": 1e-12,
    }
    _write_json(mesh_path, mesh)
    raster_sha = _sha256(raster_path)
    mesh_sha = _sha256(mesh_path)
    source_manifest = {
        "format": "b0-synthetic-source-manifest-v1",
        "synthetic_only": True,
        "raster_path": raster_path.relative_to(project_root).as_posix(),
        "raster_sha256": raster_sha,
        "mesh_path": mesh_path.relative_to(project_root).as_posix(),
        "mesh_sha256": mesh_sha,
        "source_shape_yx": [256, 256],
        "source_pixel_count": 65_536,
    }
    _write_json(source_manifest_path, source_manifest)
    source_manifest_sha = _sha256(source_manifest_path)

    with Env(GDAL_PAM_ENABLED="NO", GDAL_GEOREF_SOURCES="INTERNAL"):
        with rasterio.open(raster_path, sharing=False) as dataset:
            source_wkt = dataset.crs.to_wkt(version="WKT1_GDAL")

    config = {
        "format": "triangle-terrain-preparation-worker-config-v1",
        "schema_version": "1.0",
        "run_id": "b0-smallcase-001",
        "output_root": (case_directory / "runs").as_posix(),
        "resources": {
            "max_workers": 1,
            "job_memory_limit_mib": 2048,
            "wall_clock_limit_s": 900.0,
            "output_budget_bytes": 1_073_741_824,
            "scratch_budget_bytes": 16_777_216,
            "child_process_policy": "job_object_inheritance_without_breakaway",
        },
        "source": {
            "raster_path": raster_path.relative_to(project_root).as_posix(),
            "sha256": raster_sha,
            "source_manifest_path": source_manifest_path.relative_to(project_root).as_posix(),
            "source_manifest_sha256": source_manifest_sha,
            "expected_stat_identity": _stat_identity(raster_path),
            "components": [],
        },
        "mesh": {"path": mesh_path.relative_to(project_root).as_posix(), "sha256": mesh_sha},
        "mapping_config": {
            "format": "triangle-terrain-windowed-v1",
            "max_source_pixels": 65_536,
            "tile_rows": 256,
            "tile_cols": 256,
            "max_inflight_tiles": 1,
            "gdal_cache_mib": 64,
            "max_faces": 500_000,
            "max_nodes": 2_000_000,
            "max_candidate_overlap_checks": 1_000_000_000,
            "coverage_policy": "diagnostic_only",
            "minimum_valid_coverage": None,
            "coverage_policy_evidence_path": None,
            "coverage_policy_evidence_sha256": None,
            "source_vertical_unit_declaration": None,
            "source_vertical_unit_evidence_path": None,
            "source_vertical_unit_evidence_sha256": None,
            "source_vertical_datum_declaration": None,
            "source_vertical_datum_evidence_path": None,
            "source_vertical_datum_evidence_sha256": None,
            "area_abs_tolerance_m2": 1e-8,
            "area_rel_tolerance": 1e-10,
            "elevation_abs_tolerance_m": 1e-8,
            "elevation_rel_tolerance": 1e-10,
        },
        "crs_alias": {
            "mode": "verify_embedded_epsg4548_xy",
            "target_crs": "EPSG:4548",
            "expected_source_wkt_sha256": hashlib.sha256(source_wkt.encode("utf-8")).hexdigest(),
        },
    }
    _write_json(config_path, config)
    return config_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_directory", type=Path, help="New directory relative to project_root")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    path = create_case(args.project_root, args.case_directory)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
