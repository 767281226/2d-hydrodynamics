"""Create a small synthetic D/S source set and dispatch the restricted mesh-only route."""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import json
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import types
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


_RESTRICTED_WORKER_CLI_BOOTSTRAP = r"""
import importlib.machinery, os, sys, types
root = os.path.abspath(sys.argv[1])
package = types.ModuleType("hydrodynamics")
package.__path__ = [os.path.join(root, "hydrodynamics")]
package.__package__ = "hydrodynamics"
package.__spec__ = importlib.machinery.ModuleSpec("hydrodynamics", loader=None, is_package=True)
package.__spec__.submodule_search_locations = package.__path__
sys.modules["hydrodynamics"] = package
from hydrodynamics.restricted_worker import main
raise SystemExit(main(sys.argv[2:]))
"""


def _install_namespace_package(root: Path) -> None:
    if "hydrodynamics" in sys.modules:
        return
    package = types.ModuleType("hydrodynamics")
    package.__path__ = [str(root / "hydrodynamics")]
    package.__package__ = "hydrodynamics"
    package.__spec__ = importlib.machinery.ModuleSpec("hydrodynamics", loader=None, is_package=True)
    package.__spec__.submodule_search_locations = package.__path__
    sys.modules["hydrodynamics"] = package


RESOURCE_LIMITS = {
    "max_workers": 1,
    "job_memory_limit_mib": 2048,
    "wall_clock_limit_s": 900.0,
    "output_budget_bytes": 1_073_741_824,
    "scratch_budget_bytes": 16_777_216,
    "child_process_policy": "job_object_inheritance_without_breakaway",
}
SEED = 20261027
OUTER = [[473506.0, 4335888.0], [473606.0, 4335888.0],
         [473606.0, 4335988.0], [473506.0, 4335988.0]]
SECURITY = [[473546.0, 4335928.0], [473556.0, 4335928.0],
            [473556.0, 4335938.0], [473546.0, 4335938.0]]
CONSTRAINT = [[473516.0, 4335908.0], [473526.0, 4335908.0], [473526.0, 4335918.0]]
DOMAIN_BOUNDARY_LINE = OUTER + [OUTER[0]]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> bytes:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _file_identity(path: Path, *, base: Path) -> dict[str, Any]:
    from hydrodynamics.restricted_worker import _stat_identity

    return {
        "path": path.relative_to(base).as_posix(),
        "sha256": _sha256(path),
        "expected_stat_identity": _stat_identity(path),
    }


def _write_domain_shapefile(path: Path, polygons: list[list[list[list[float]]]], *, prj_crs: str = "EPSG:4548") -> dict[str, Path]:
    import shapefile
    from pyproj import CRS

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = shapefile.Writer(str(path), shapeType=shapefile.POLYGON, encoding="utf-8")
    writer.field("source", "C", size=32)
    try:
        for rings in polygons:
            writer.poly(rings)
            writer.record("synthetic")
    finally:
        writer.close()
    sidecars = {suffix: path.with_suffix(f".{suffix}") for suffix in ("shp", "shx", "dbf")}
    sidecars["prj"] = path.with_suffix(".prj")
    sidecars["cpg"] = path.with_suffix(".cpg")
    sidecars["prj"].write_text(CRS.from_user_input(prj_crs).to_wkt(version="WKT1_GDAL") + "\n", encoding="utf-8")
    sidecars["cpg"].write_text("UTF-8\n", encoding="ascii")
    return sidecars


def _polygon_geojson(path: Path, rings: list[list[list[float]]]) -> Path:
    payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4548"}},
        "features": [{"type": "Feature", "properties": {"synthetic": True},
                      "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]] for ring in polygon]}}
                     for polygon in rings],
    }
    _write_json(path, payload)
    return path


def _line_geojson(path: Path, points: list[list[float]]) -> Path:
    payload = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4548"}},
        "features": [{"type": "Feature", "properties": {"synthetic": True},
                      "geometry": {"type": "LineString", "coordinates": points}}],
    }
    _write_json(path, payload)
    return path


def create_synthetic_domain_mesh_case(
    project_root: str | Path,
    experiment_dir: str | Path,
    *,
    run_id: str = "domain-mesh-only-synthetic-normal",
    d_polygons: list[list[list[list[float]]]] | None = None,
    s_polygons: list[list[list[list[float]]]] | None = None,
    d_prj_crs: str = "EPSG:4548",
    d_source_crs: str = "EPSG:4548",
    s_source_crs: str = "EPSG:4548",
    constraint_points: list[list[float]] | None = None,
    boundary_points: list[list[float]] | None = None,
    include_constraint: bool = True,
    derived_domain_exterior: bool = False,
    maximum_faces: int = 500_000,
    minimum_face_quality: float = 0.1,
    mesh_algorithm_plan: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any], Path, dict[str, Any]]:
    """Create a fresh experiment tree. Inputs contain only small synthetic vectors."""
    root = Path(project_root).resolve()
    _install_namespace_package(root)
    from hydrodynamics.domain_mesh_only import (
        DOMAIN_MESH_ONLY_FORMAT, DOMAIN_MESH_ONLY_VERSION,
        DOMAIN_MESH_ONLY_ALGORITHM_PLAN_VERSION,
        DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION, DOMAIN_SOURCE_MANIFEST_FORMAT, DOMAIN_SOURCE_MANIFEST_VERSION,
        RESOURCE_LIMITS as MESH_ONLY_RESOURCE_LIMITS,
        validate_domain_mesh_only_config_shape, validate_source_manifest_shape,
    )
    from hydrodynamics.mesh_algorithm_plan import validate_mesh_algorithm_plan_shape
    from hydrodynamics.restricted_worker import _stat_identity
    from hydrodynamics.b1_synthetic_pilot import _gmsh_runtime_identity

    experiment = Path(experiment_dir)
    if Path(experiment).is_absolute() or PureWindowsPath(str(experiment)).is_absolute() or PureWindowsPath(str(experiment)).drive:
        raise ValueError("experiment_dir must be project-relative")
    experiment_path = (root / experiment).resolve()
    if not experiment_path.is_relative_to(root):
        raise ValueError("experiment_dir must remain inside project_root")
    if experiment_path.exists():
        raise FileExistsError(f"experiment directory already exists; no-clobber refused: {experiment_path}")
    if (not isinstance(run_id, str) or not 1 <= len(run_id) <= 64
            or not run_id[0].isascii() or not run_id[0].isalnum()
            or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for ch in run_id)):
        raise ValueError("run_id must use 1–64 safe ASCII identifier characters")
    experiment_path.mkdir(parents=True)
    input_root = experiment_path / "inputs"
    input_root.mkdir()
    d_polygons = d_polygons or [[OUTER]]
    s_polygons = s_polygons or [[SECURITY]]
    if derived_domain_exterior and boundary_points is not None:
        raise ValueError("derived_domain_exterior does not accept an external boundary_points file")
    if derived_domain_exterior:
        manifest_root = input_root / "staged"
        originals_root = manifest_root / "originals"
        files_root = manifest_root / "files"
        originals_root.mkdir(parents=True)
        files_root.mkdir()
        d_originals = _write_domain_shapefile(originals_root / "domain" / "domain_D.shp", d_polygons, prj_crs=d_prj_crs)
        d_sidecars: dict[str, Path] = {}
        for role, source_path in d_originals.items():
            staged_path = files_root / "domain" / source_path.name
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, staged_path)
            d_sidecars[role] = staged_path
        security_original = _polygon_geojson(originals_root / "security" / "security_S.geojson", s_polygons)
        security_path = files_root / "security" / security_original.name
        security_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(security_original, security_path)
        if include_constraint:
            constraint_points = constraint_points or CONSTRAINT
            constraint_original = _line_geojson(originals_root / "lines" / "constraint_01.geojson", constraint_points)
            constraint_path = files_root / "lines" / constraint_original.name
            constraint_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(constraint_original, constraint_path)
        else:
            constraint_path = None

        def copied_identity(staged: Path, original: Path) -> dict[str, Any]:
            value = _file_identity(staged, base=manifest_root)
            value["origin"] = _file_identity(original, base=manifest_root)
            return value

        components = [{
            "component_id": "D", "source_kind": "file", "role": "domain_D", "format": "polygon_shapefile",
            "source_crs": d_source_crs, "source_version": "synthetic-D-v1",
            "group_name": None, "closed": None,
            "files": {role: copied_identity(path, d_originals[role]) for role, path in d_sidecars.items()},
        }, {
            "component_id": "S", "source_kind": "file", "role": "security_S", "format": "polygon_geojson",
            "source_crs": s_source_crs, "source_version": "synthetic-S-v1",
            "group_name": None, "closed": None,
            "files": {"geometry": copied_identity(security_path, security_original)},
        }, {
            "component_id": "D-exterior", "source_kind": "derived", "role": "boundary_line",
            "format": "derived_domain_D_exterior_line_v1", "parent_component_id": "D",
            "group_name": "synthetic_D_exterior", "closed": True,
            "geometry_only": True, "q_binding": "NONE",
        }]
        if constraint_path is not None:
            components.append({
                "component_id": "constraint_01", "source_kind": "file", "role": "constraint_line",
                "format": "line_geojson", "source_crs": "EPSG:4548", "source_version": "synthetic-constraint-v1",
                "group_name": "synthetic_constraint", "closed": False,
                "files": {"geometry": copied_identity(constraint_path, constraint_original)},
            })
        manifest_version = DOMAIN_SOURCE_MANIFEST_DERIVED_VERSION
    else:
        manifest_root = input_root
        constraint_points = constraint_points or CONSTRAINT
        boundary_points = boundary_points or DOMAIN_BOUNDARY_LINE
        d_sidecars = _write_domain_shapefile(input_root / "domain" / "domain_D.shp", d_polygons, prj_crs=d_prj_crs)
        security_path = _polygon_geojson(input_root / "security" / "security_S.geojson", s_polygons)
        boundary_path = _line_geojson(input_root / "lines" / "domain_boundary.geojson", boundary_points)
        constraint_path = _line_geojson(input_root / "lines" / "constraint_01.geojson", constraint_points)
        components = [{
            "component_id": "domain_D", "role": "domain_D", "format": "polygon_shapefile",
            "source_crs": d_source_crs, "group_name": None, "closed": None,
            "files": {role: _file_identity(path, base=input_root) for role, path in d_sidecars.items()},
        }, {
            "component_id": "security_S", "role": "security_S", "format": "polygon_geojson",
            "source_crs": s_source_crs, "group_name": None, "closed": None,
            "files": {"geometry": _file_identity(security_path, base=input_root)},
        }, {
            "component_id": "domain_boundary_01", "role": "boundary_line", "format": "line_geojson",
            "source_crs": "EPSG:4548", "group_name": "synthetic_domain_boundary", "closed": True,
            "files": {"geometry": _file_identity(boundary_path, base=input_root)},
        }, {
            "component_id": "constraint_01", "role": "constraint_line", "format": "line_geojson",
            "source_crs": "EPSG:4548", "group_name": "synthetic_constraint", "closed": False,
            "files": {"geometry": _file_identity(constraint_path, base=input_root)},
        }]
        manifest_version = DOMAIN_SOURCE_MANIFEST_VERSION
    source_manifest = {
        "format": DOMAIN_SOURCE_MANIFEST_FORMAT, "schema_version": manifest_version,
        "source_set_id": f"{run_id[:55]}-sources", "target_coordinate_system": "EPSG:4548",
        "components": components,
    }
    validate_source_manifest_shape(source_manifest)
    manifest_path = manifest_root / "source_manifest.json"
    manifest_bytes = _write_json(manifest_path, source_manifest)
    gmsh_identity = _gmsh_runtime_identity()
    config = {
        "format": DOMAIN_MESH_ONLY_FORMAT,
        "schema_version": (DOMAIN_MESH_ONLY_ALGORITHM_PLAN_VERSION
                           if mesh_algorithm_plan is not None else DOMAIN_MESH_ONLY_VERSION),
        "run_id": run_id, "output_root": (experiment_path / "output").relative_to(root).as_posix(),
        "resources": dict(MESH_ONLY_RESOURCE_LIMITS),
        "source_manifest": {
            "path": manifest_path.relative_to(root).as_posix(), "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "expected_stat_identity": _stat_identity(manifest_path),
        },
        "target_coordinate_system": "EPSG:4548",
        "mesh_generation": {
            "maximum_faces": maximum_faces, "maximum_nodes": 2_000_000,
            "minimum_face_quality": minimum_face_quality, "geometry_tolerance_m": 1e-8,
            "minimum_face_area_m2": 1e-12, "default_size_m": 50.0, "minimum_size_m": 1.0,
            "area_abs_tolerance_m2": 1e-7, "area_rel_tolerance": 1e-9,
        },
        "expected_gmsh_version": gmsh_identity["python_binding_version"],
        "expected_gmsh_library_sha256": gmsh_identity["native_library_sha256"],
    }
    if mesh_algorithm_plan is not None:
        config["mesh_algorithm_plan"] = validate_mesh_algorithm_plan_shape(mesh_algorithm_plan)
    validate_domain_mesh_only_config_shape(config)
    config_path = experiment_path / "configs" / f"{run_id}.json"
    _write_json(config_path, config)
    return config_path, config, manifest_path, source_manifest


def dispatch_restricted_worker_cli(config_path: str | Path, project_root: str | Path) -> subprocess.CompletedProcess[str]:
    root = Path(project_root).resolve()
    config = Path(config_path).resolve()
    if not config.is_relative_to(root):
        raise ValueError("config_path must remain inside project_root")
    command = [
        sys.executable, "-c", _RESTRICTED_WORKER_CLI_BOOTSTRAP, str(root),
        "--run-config", config.relative_to(root).as_posix(), "--project-root", str(root),
    ]
    return subprocess.run(command, cwd=root, check=False, text=True, capture_output=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--experiment-dir", required=True)
    parser.add_argument("--algorithm-plan", help="Project-relative v1 algorithm plan; selects mesh-only config schema 1.1")
    parser.add_argument("--derived-domain-exterior", action="store_true",
                        help="use schema 1.1 and derive a geometry-only D exterior inside the worker Job")
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    _install_namespace_package(root)
    algorithm_plan = None
    if args.algorithm_plan is not None:
        plan_path = Path(args.algorithm_plan)
        if (plan_path.is_absolute() or PureWindowsPath(args.algorithm_plan).is_absolute()
                or PureWindowsPath(args.algorithm_plan).drive or ".." in plan_path.parts):
            raise ValueError("--algorithm-plan must be a project-relative path without parent traversal")
        plan_file = (root / plan_path).resolve()
        if (not plan_file.is_relative_to(root) or plan_file.is_symlink() or not plan_file.is_file()
                or plan_file.stat().st_size > 64 * 1024):
            raise ValueError("--algorithm-plan must be a bounded regular project file")
        from hydrodynamics.mesh_algorithm_plan import validate_mesh_algorithm_plan_shape
        algorithm_plan = validate_mesh_algorithm_plan_shape(json.loads(plan_file.read_text(encoding="utf-8")))
    config_path, _config, _manifest_path, _manifest = create_synthetic_domain_mesh_case(
        root, args.experiment_dir, derived_domain_exterior=args.derived_domain_exterior,
        include_constraint=not args.derived_domain_exterior, mesh_algorithm_plan=algorithm_plan)
    completed = dispatch_restricted_worker_cli(config_path, root)
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
