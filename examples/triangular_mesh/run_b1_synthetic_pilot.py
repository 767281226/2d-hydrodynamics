"""Generate and run the synthetic P0/G0 restricted preparation pilot.

All generated rasters, meshes, configs, bundles and measurements stay under
the supplied work directory. The worker CLI is invoked with an explicit
project root so route/source code hashes are bound to this checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SEED = 20261027
RESOURCE_LIMITS = {
    "max_workers": 1,
    "job_memory_limit_mib": 2048,
    "wall_clock_limit_s": 900.0,
    "output_budget_bytes": 1_073_741_824,
    "scratch_budget_bytes": 16_777_216,
    "child_process_policy": "job_object_inheritance_without_breakaway",
}
P0_MAPPING_LIMITS = {
    "max_source_pixels": 262_144,
    "tile_rows": 256,
    "tile_cols": 256,
    "max_inflight_tiles": 1,
    "gdal_cache_mib": 64,
    "max_faces": 500_000,
    "max_nodes": 2_000_000,
    "max_candidate_overlap_checks": 1_000_000,
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
}
GOLDEN_TOLERANCES = {
    "area_abs_m2": 5e-7,
    "area_rel": 5e-10,
    "elevation_abs_m": 1e-7,
    "coverage_abs": 5e-10,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stat_identity(path: Path) -> dict[str, int]:
    info = path.stat()
    return {"size_bytes": int(info.st_size), "mtime_ns": int(info.st_mtime_ns),
            "ctime_ns": int(info.st_ctime_ns), "device": int(info.st_dev), "file_id": int(info.st_ino)}


def _write_json(path: Path, value: Any) -> None:
    if path.exists():
        raise FileExistsError(f"no-clobber pilot input exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def _make_mesh(face_count: int, *, seed: int) -> dict[str, Any]:
    if face_count not in {2, 8, 32}:
        raise ValueError("P0 mesh face_count must be 2, 8 or 32")
    square_count = face_count // 2
    grid_side = int(math.sqrt(square_count))
    if grid_side * grid_side != square_count:
        raise ValueError("P0 face counts must form a complete square grid of 50 m cells")
    origin_col, origin_row = 230, 230
    xmin, ymax = 473_506.0, 4_335_988.0
    nodes: list[list[float]] = []
    node_index: dict[tuple[int, int], int] = {}
    faces: list[list[int]] = []
    rng = random.Random(seed)

    def node(col: int, row: int) -> int:
        key = (col, row)
        if key not in node_index:
            node_index[key] = len(nodes)
            nodes.append([xmin + col, ymax - row])
        return node_index[key]

    for y in range(grid_side):
        for x in range(grid_side):
            row, col = origin_row + 50 * y, origin_col + 50 * x
            nw, ne = node(col, row), node(col + 50, row)
            sw, se = node(col, row + 50), node(col + 50, row + 50)
            if rng.randrange(2) == 0:
                faces.extend(([nw, sw, se], [nw, se, ne]))
            else:
                faces.extend(([nw, sw, ne], [sw, se, ne]))
    return {
        "format": "triangle-mesh-xy-json-v1",
        "mesh_id": f"p0-{face_count}-seed-{seed}",
        "coordinate_system": "EPSG:4548",
        "node_xy": nodes,
        "face_nodes": faces,
        "face_id": list(range(10_000, 10_000 + face_count)),
        "geometry_tolerance_m": 1e-8,
        "minimum_face_area_m2": 1e-12,
    }


def _write_source_raster(path: Path, *, masked: bool) -> None:
    import numpy as np
    import rasterio
    from rasterio.env import Env
    from rasterio.transform import from_origin

    rows, cols = np.indices((512, 512), dtype=np.float64)
    values = (100.0 + 0.001 * (cols + 0.5) + 0.002 * (511.5 - rows)).astype(np.float32)
    mask = np.full((512, 512), 255, dtype=np.uint8)
    if masked:
        values[250, 250] = -32767.0
        values[260, 262] = np.nan
        mask[270, 275] = 0
    with Env(GDAL_PAM_ENABLED="NO", GDAL_TIFF_INTERNAL_MASK=True):
        with rasterio.open(
            path, "w", driver="GTiff", width=512, height=512, count=1,
            dtype="float32", nodata=-32767.0,
            transform=from_origin(473_506.0, 4_335_988.0, 1.0, 1.0),
            crs="EPSG:4548", compress="NONE", tiled=False,
        ) as dataset:
            dataset.write(values, 1)
            dataset.set_band_unit(1, "metre")
            dataset.update_tags(AREA_OR_POINT="Area")
            dataset.write_mask(mask)


def _dependency_identity() -> dict[str, str]:
    from hydrodynamics.mesh_generation import _gmsh_native_library_path

    if "gmsh" in sys.modules:
        raise RuntimeError("the pilot supervisor imported the Gmsh Python binding before Job launch")
    native = _gmsh_native_library_path()
    if native is None:
        raise RuntimeError("Gmsh native SDK DLL is unavailable; install the declared .[gmsh] extra")
    return {"expected_gmsh_version": importlib.metadata.version("gmsh"),
            "expected_gmsh_library_sha256": _sha256(native),
            "native_library_path_for_diagnostics": str(native.resolve())}


def _synthetic_source_manifest(root: Path, path: Path, mesh_path: Path) -> tuple[Path, str, str]:
    raster_sha, mesh_sha = _sha256(path), _sha256(mesh_path)
    raster_stat, mesh_stat = _stat_identity(path), _stat_identity(mesh_path)
    record = {
        "format": "b1-synthetic-source-manifest-v1", "synthetic_only": True,
        "raster_path": path.relative_to(root).as_posix(), "raster_sha256": raster_sha,
        "raster_bytes": raster_stat["size_bytes"], "raster_mtime_ns": raster_stat["mtime_ns"],
        "mesh_path": mesh_path.relative_to(root).as_posix(), "mesh_sha256": mesh_sha,
        "input_files": [
            {"path": path.relative_to(root).as_posix(), "sha256": raster_sha,
             "bytes": raster_stat["size_bytes"], "mtime_ns": raster_stat["mtime_ns"]},
            {"path": mesh_path.relative_to(root).as_posix(), "sha256": mesh_sha,
             "bytes": mesh_stat["size_bytes"], "mtime_ns": mesh_stat["mtime_ns"]},
        ],
    }
    manifest_path = path.parent / f"source_manifest_{path.stem}_{mesh_path.stem}.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing != record:
            raise FileExistsError(f"existing synthetic source manifest has a different identity: {manifest_path}")
    else:
        _write_json(manifest_path, record)
    return manifest_path, raster_sha, mesh_sha


def _worker_config(root: Path, experiment: Path, *, run_id: str, action: str,
                   raster_path: Path, mesh_path: Path, mesh_generation: dict[str, Any] | None,
                   pilot_case: dict[str, Any] | None) -> Path:
    from hydrodynamics.restricted_worker import _stat_identity
    import rasterio
    from rasterio.env import Env

    manifest_path, raster_sha, mesh_sha = _synthetic_source_manifest(root, raster_path, mesh_path)
    base_example = json.loads((root / "examples/triangular_mesh/restricted_worker_config.example.json").read_text(encoding="utf-8"))
    mapping = dict(base_example["mapping_config"])
    mapping.update(P0_MAPPING_LIMITS)
    with Env(GDAL_PAM_ENABLED="NO", GDAL_GEOREF_SOURCES="INTERNAL"):
        with rasterio.open(raster_path, sharing=False) as dataset:
            source_wkt = dataset.crs.to_wkt(version="WKT1_GDAL")
    config = {
        "format": "triangle-b1-synthetic-pilot-config-v1", "schema_version": "1.0",
        "run_id": run_id,
        "output_root": (experiment / "prepared" / action).relative_to(root).as_posix(),
        "resources": dict(RESOURCE_LIMITS),
        "source": {
            "raster_path": raster_path.relative_to(root).as_posix(), "sha256": raster_sha,
            "source_manifest_path": manifest_path.relative_to(root).as_posix(),
            "source_manifest_sha256": _sha256(manifest_path),
            "expected_stat_identity": _stat_identity(raster_path), "components": [],
        },
        "mesh": {"path": mesh_path.relative_to(root).as_posix(), "sha256": mesh_sha},
        "mapping_config": mapping,
        "crs_alias": {"mode": "verify_embedded_epsg4548_xy", "target_crs": "EPSG:4548",
                       "expected_source_wkt_sha256": hashlib.sha256(source_wkt.encode("utf-8")).hexdigest()},
        "action": action,
        "pilot_case": pilot_case,
        "mesh_generation": mesh_generation,
    }
    from hydrodynamics.restricted_worker import validate_route_config_shape
    validate_route_config_shape(config)
    path=experiment/"configs"/f"{run_id}.json"
    _write_json(path,config)
    return path


def _write_face_meshes(root: Path, inputs: Path) -> dict[int, Path]:
    paths: dict[int, Path] = {}
    for count in (2, 8, 32):
        path = inputs / f"mesh_{count}.json"
        _write_json(path, _make_mesh(count, seed=SEED))
        paths[count] = path
    return paths


def _oracle_rows(raster_path: Path, mesh_path: Path) -> dict[int, dict[str, Any]]:
    """Independent synthetic-only pixel/triangle oracle using Shapely intersections."""
    import numpy as np
    import rasterio
    from shapely.geometry import Polygon, box

    mesh=json.loads(mesh_path.read_text(encoding="utf-8"))
    with rasterio.Env(GDAL_PAM_ENABLED="NO",GDAL_GEOREF_SOURCES="INTERNAL"):
        with rasterio.open(raster_path,sharing=False) as dataset:
            raw=dataset.read(1,masked=False)
            source_mask=dataset.read_masks(1)
            nodata=dataset.nodata
            left=dataset.transform.c
            top=dataset.transform.f
            transform=dataset.transform
            result={}
            for face_id,indices in zip(mesh["face_id"],mesh["face_nodes"]):
                coords=[tuple(mesh["node_xy"][index]) for index in indices]
                polygon=Polygon(coords)
                face_area=float(polygon.area)
                minx,miny,maxx,maxy=polygon.bounds
                col0=max(0,math.floor((minx-left)/transform.a))
                col1=min(dataset.width,math.ceil((maxx-left)/transform.a))
                row0=max(0,math.floor((top-maxy)/-transform.e))
                row1=min(dataset.height,math.ceil((top-miny)/-transform.e))
                valid=nodata_area=nonfinite=masked=covered=weighted=0.0
                tiny=max(float(mesh["geometry_tolerance_m"])**2,face_area*1e-14)
                for row in range(row0,row1):
                    for col in range(col0,col1):
                        pixel=box(left+col,top-row-1,left+col+1,top-row)
                        overlap=float(polygon.intersection(pixel).area)
                        if overlap<=tiny: continue
                        covered+=overlap
                        value=float(raw[row,col])
                        is_nonfinite=not math.isfinite(value)
                        is_nodata=(not is_nonfinite and ((source_mask[row,col]==0) or (nodata is not None and value==float(nodata))))
                        is_valid=not is_nonfinite and not is_nodata
                        if is_nonfinite: nonfinite+=overlap
                        elif is_nodata: nodata_area+=overlap
                        elif is_valid:
                            valid+=overlap;weighted+=overlap*value
                        else: masked+=overlap
                result[int(face_id)]={
                    "face_area_m2":face_area,"covered_area_m2":covered,"valid_area_m2":valid,
                    "nodata_area_m2":nodata_area,"nonfinite_area_m2":nonfinite,
                    "masked_other_area_m2":masked,"coverage_ratio":min(1.0,valid/face_area),
                    "elevation_m":None if valid==0 else weighted/valid,
                }
    return result


def _compare_golden(raster_path: Path, mesh_path: Path, bundle_path: Path) -> dict[str, Any]:
    from hydrodynamics.prepared_terrain import read_prepared_terrain

    expected=_oracle_rows(raster_path,mesh_path)
    reader=read_prepared_terrain(bundle_path)
    observed={int(row["face_id"]):row for row in reader.iter_faces()}
    if set(expected)!=set(observed): raise AssertionError("synthetic golden face IDs differ from prepared reader")
    errors={"area_abs_m2":0.0,"coverage_abs":0.0,"elevation_abs_m":0.0}
    for face_id,golden in expected.items():
        row=observed[face_id]
        for field in ("face_area_m2","covered_area_m2","valid_area_m2","nodata_area_m2","nonfinite_area_m2","masked_other_area_m2"):
            errors["area_abs_m2"]=max(errors["area_abs_m2"],abs(float(row[field])-float(golden[field])))
        errors["coverage_abs"]=max(errors["coverage_abs"],abs(float(row["coverage_ratio"])-float(golden["coverage_ratio"])))
        if golden["elevation_m"] is not None and row["elevation_m"] is not None:
            errors["elevation_abs_m"]=max(errors["elevation_abs_m"],abs(float(row["elevation_m"])-float(golden["elevation_m"])))
    if (errors["area_abs_m2"]>GOLDEN_TOLERANCES["area_abs_m2"]
            or errors["coverage_abs"]>GOLDEN_TOLERANCES["coverage_abs"]
            or errors["elevation_abs_m"]>GOLDEN_TOLERANCES["elevation_abs_m"]):
        raise AssertionError(f"window mapper differs from independent synthetic oracle: {errors}")
    return {"oracle":"Shapely per-face/per-pixel polygon intersection; synthetic input only",
            "tolerances":dict(GOLDEN_TOLERANCES),"maximum_absolute_errors":errors,
            "face_count":len(expected)}


def _run_cli(root: Path, config_path: Path) -> tuple[dict[str, Any], float, list[str]]:
    command=[sys.executable,"-m","hydrodynamics.restricted_worker","--run-config",
             config_path.relative_to(root).as_posix(),"--project-root",str(root)]
    started=time.perf_counter()
    completed=subprocess.run(command,cwd=root,capture_output=True,text=True,timeout=960)
    elapsed=time.perf_counter()-started
    lines=[line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"pilot CLI produced no JSON result; exit={completed.returncode}; stderr={completed.stderr[-2000:]}")
    try: result=json.loads(lines[-1])
    except json.JSONDecodeError as exc: raise RuntimeError(f"pilot CLI result is not JSON: {lines[-1]!r}") from exc
    result["subprocess_exit_code"]=completed.returncode
    result["subprocess_stderr_tail"]=completed.stderr[-2000:]
    return result,elapsed,command


def run_experiment(project_root: Path, experiment_root: Path, *, p0_repeats: int = 2) -> dict[str, Any]:
    if p0_repeats != 2:
        raise ValueError("the frozen P0 protocol uses exactly two independent worker launches per face count")
    if "gmsh" in sys.modules:
        raise RuntimeError("supervisor process already imported gmsh before the Job-bound G0 run")
    experiment_root=experiment_root.resolve()
    experiment_root.relative_to(project_root)
    if experiment_root.exists(): raise FileExistsError(f"no-clobber experiment directory exists: {experiment_root}")
    experiment_root.mkdir(parents=True,exist_ok=False)
    inputs=experiment_root/"inputs";inputs.mkdir()
    configs=experiment_root/"configs";configs.mkdir()
    inputs_rasters={}
    for pattern in ("planar_all_valid","planar_nodata_nonfinite"):
        raster_path=inputs/f"source_{pattern}.tif"
        _write_source_raster(raster_path,masked=pattern!="planar_all_valid")
        inputs_rasters[pattern]=raster_path
    mesh_paths=_write_face_meshes(project_root,inputs)

    p0_runs=[]
    for pattern,counts in (("planar_all_valid",(2,8,32)),("planar_nodata_nonfinite",(32,))):
        for count in counts:
            for repeat in range(1,p0_repeats+1):
                run_id=f"p0-{pattern.replace('_','-')}-f{count}-r{repeat}"
                case={"case_id":run_id,"seed":SEED,"face_count":count,"face_size_m":50.0,
                      "repeat_index":repeat,"source_pattern":pattern}
                config_path=_worker_config(project_root,experiment_root,run_id=run_id,
                    action="p0_windowed_mapping",raster_path=inputs_rasters[pattern],
                    mesh_path=mesh_paths[count],mesh_generation=None,pilot_case=case)
                result,cli_elapsed,command=_run_cli(project_root,config_path)
                if result.get("subprocess_exit_code")!=0 or result.get("status")!="preparation_diagnostic_complete":
                    raise RuntimeError(f"P0 run failed: {run_id}: {result}")
                bundle=Path(result["bundle_path"])
                manifest=json.loads((bundle/"manifest.json").read_text(encoding="utf-8"))
                status=json.loads((bundle/"status.json").read_text(encoding="utf-8"))
                golden=_compare_golden(inputs_rasters[pattern],mesh_paths[count],bundle)
                receipt=manifest["worker"]
                pilot=manifest["synthetic_pilot"]
                run_record={
                    "run_id":run_id,"action":"p0_windowed_mapping","source_pattern":pattern,
                    "face_count":count,"face_size_m":50.0,"repeat_index":repeat,"seed":SEED,
                    "config_path":config_path.relative_to(project_root).as_posix(),
                    "config_sha256":_sha256(config_path),"bundle_path":str(bundle),
                    "subprocess_command":command,"subprocess_exit_code":result["subprocess_exit_code"],
                    "cli_elapsed_seconds":cli_elapsed,"status":result["status"],"exit_code":result["exit_code"],
                    "worker_started_return_status":result["worker_started"],"worker_started_status_file":status["worker_started"],
                    "pilot_measurements":pilot,"prepared_writer_seconds":json.loads((bundle/"artifacts/worker-result.json").read_text(encoding="utf-8"))["prepared_writer_seconds"],
                    "readback_seconds":status.get("pilot_readback_seconds"),"job_receipt":receipt,
                    "golden_comparison":golden,
                }
                p0_runs.append(run_record)
    p0_summary={"format":"b1-p0-results-v1","synthetic_only":True,"source_shape_yx":[512,512],
                "source_resolution_m":1.0,"tile_rows":256,"tile_cols":256,"gdal_cache_mib":64,
                "max_inflight_tiles":1,"max_candidate_overlap_checks":1_000_000,
                "resources":dict(RESOURCE_LIMITS),"runs":p0_runs,
                "timing_protocol":{"worker_process":"fresh process for each trial; no warm worker reuse",
                                    "filesystem_cache":"host filesystem cache state is uncontrolled; trial index is recorded, not claimed cold/warm",
                                    "mapper_components":"metadata IO, raw-window IO, candidate search, clip execution and residual",
                                    "golden_error_baseline":dict(GOLDEN_TOLERANCES)}}
    _write_json(experiment_root/"p0_results.json",p0_summary)

    gmsh_identity=_dependency_identity()
    g0_config_path=inputs/"gmsh_recipe.json"
    recipe_example=json.loads((project_root/"examples/triangular_mesh/b1_synthetic_gmsh_recipe.example.json").read_text(encoding="utf-8"))
    _write_json(g0_config_path,recipe_example)
    g0_run_id="g0-gmsh-synthetic-normal"
    g0_generation={"format":"triangle-b1-gmsh-generation-v1","maximum_faces":500_000,
        "maximum_nodes":2_000_000,"minimum_face_quality":0.1,"area_abs_tolerance_m2":1e-7,
        "area_rel_tolerance":1e-9,"expected_gmsh_version":gmsh_identity["expected_gmsh_version"],
        "expected_gmsh_library_sha256":gmsh_identity["expected_gmsh_library_sha256"],
        "area_roles":{"outer_boundary":"synthetic_D",
                       "holes":{"hole_0":"synthetic_safety_area_exclusion"},
                       "constraints":{"synthetic_constraint":"synthetic_constraint_line"}}}
    g0_config_path=_worker_config(project_root,experiment_root,run_id=g0_run_id,
        action="g0_gmsh_windowed_mapping",raster_path=inputs_rasters["planar_all_valid"],
        mesh_path=g0_config_path,mesh_generation=g0_generation,pilot_case=None)
    result,cli_elapsed,command=_run_cli(project_root,g0_config_path)
    if result.get("subprocess_exit_code")!=0 or result.get("status")!="preparation_diagnostic_complete":
        raise RuntimeError(f"G0 normal run failed: {result}")
    bundle=Path(result["bundle_path"])
    manifest=json.loads((bundle/"manifest.json").read_text(encoding="utf-8"))
    status=json.loads((bundle/"status.json").read_text(encoding="utf-8"))
    g0_worker_result=json.loads((bundle/"artifacts/worker-result.json").read_text(encoding="utf-8"))
    g0_summary={"format":"b1-g0-results-v1","synthetic_only":True,"config_path":g0_config_path.relative_to(project_root).as_posix(),
        "config_sha256":_sha256(g0_config_path),"bundle_path":str(bundle),"subprocess_command":command,
        "subprocess_exit_code":result["subprocess_exit_code"],"cli_elapsed_seconds":cli_elapsed,
        "status":result["status"],"exit_code":result["exit_code"],
        "worker_started_return_status":result["worker_started"],"worker_started_status_file":status["worker_started"],
        "worker_receipt":manifest["worker"],"pilot_evidence":manifest["synthetic_pilot"],
        "prepared_writer_seconds":g0_worker_result["prepared_writer_seconds"],
        "readback_seconds":status.get("pilot_readback_seconds"),
        "output_artifacts":{name:{"sha256":_sha256(bundle/"artifacts"/name),"bytes":(bundle/"artifacts"/name).stat().st_size}
                            for name in ("gmsh_mesh.json","gmsh_mapping_mesh.json","g0_source_manifest.json")}}
    _write_json(experiment_root/"g0_results.json",g0_summary)
    runtime={"python":sys.version.splitlines()[0],"installed_gmsh":gmsh_identity,
             "supervisor_gmsh_module_loaded":"gmsh" in sys.modules}
    summary={"format":"m2-b1-p0-g0-synthetic-summary-v1","base_commit":"73da7c24591423d2b6b3cede8eed2496baca295b",
             "project_root":str(project_root),"package_version":importlib.metadata.version("two-d-hydrodynamics"),
             "runtime_preflight":runtime,"resources":dict(RESOURCE_LIMITS),"p0":p0_summary,"g0":g0_summary,
             "scope":{"real_dem_pixels_read":False,"real_dem_full_hash_recomputed":False,
                      "real_D_S_geometry_recomputed":False,"gmsh_real_source_geometry":False,
                      "b1_started":False,"solver_run":False,"database_or_social_economics_used":False}}
    _write_json(experiment_root/"pilot_summary.json",summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root",type=Path,default=PROJECT_ROOT)
    parser.add_argument("--experiment-dir",type=Path,default=Path("work/full_dem_m2_b1_p0g0_20261027/experiment_001"))
    parser.add_argument("--p0-repeats",type=int,choices=(2,),default=2)
    args=parser.parse_args(argv)
    project_root=args.project_root.resolve()
    experiment=args.experiment_dir if args.experiment_dir.is_absolute() else project_root/args.experiment_dir
    summary=run_experiment(project_root,experiment,p0_repeats=args.p0_repeats)
    print(json.dumps({"experiment_dir":str(experiment.resolve()),"summary":str(experiment.resolve()/"pilot_summary.json"),
                      "p0_runs":len(summary["p0"]["runs"]),"g0_status":summary["g0"]["status"],
                      "g0_exit_code":summary["g0"]["exit_code"],"gmsh_loaded_in_supervisor":summary["runtime_preflight"]["supervisor_gmsh_module_loaded"]},
                     ensure_ascii=False,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
