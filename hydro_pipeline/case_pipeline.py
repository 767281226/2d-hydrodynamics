"""One-config case pipeline: any projected DEM + domain polygon + sections + discharge CSV -> run bundle.

Stages (each writes into ``work_dir`` and is skipped when its inputs are unchanged):

``geometry``  domain polygon (coordinates / GeoJSON / Shapefile) -> simplified outer ring, holes, constraint lines
``mesh``      Gmsh constrained triangle mesh -> ``triangle-domain-mesh-only-artifact-v1`` JSON
``terrain``   exact area-weighted DEM mapping per face (raw values; NoData is never filled) -> ``terrain_faces.npz``
``sections``  inflow/outflow sections on the mesh exterior by the DEM main-channel rule -> sections audit JSON
``config``    ``large-run-driver-config-v1`` JSON for :mod:`large_run_driver`
``run``       solver -> native NetCDF -> bundle export -> verification (viewer: ``start_viewer.bat <run id>``)

The pipeline performs no CRS conversion, resampling or NoData filling: the domain, section
points and DEM must already share one projected metric CRS (the DEM CRS may be an equivalent
WKT of ``dem.crs``; equivalence is checked with pyproj, axis order ignored).

CLI::

    python -m hydro_pipeline.case_pipeline case.yaml [--stage STAGE] [--check] [--force] [--resume] [--id RUN_ID]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Mapping, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError, field_validator, model_validator

CASE_FORMAT = "case-pipeline-config-v1"
CASE_VERSION = "1.0"
STAGES = ("geometry", "mesh", "terrain", "sections", "config", "run")
MESH_ARTIFACT_FORMAT = "triangle-domain-mesh-only-artifact-v1"
Number = StrictFloat | StrictInt


class CasePipelineError(ValueError):
    """Clear, user-facing pipeline error (bad config, missing input, failed stage)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class PolygonSource(_Strict):
    """One polygon (or polygon set unioned into one Polygon)."""

    type: Literal["coordinates", "geojson", "shp"]
    path: StrictStr | None = Field(None, min_length=1, description="geojson/shp file, relative to the case config")
    coordinates: list[list[Number]] | None = Field(None, min_length=3, description="m; [[x, y], ...] in dem.crs, ring closure optional")

    @model_validator(mode="after")
    def _one_source(self) -> "PolygonSource":
        if self.type == "coordinates":
            if self.coordinates is None or self.path is not None:
                raise ValueError("type coordinates needs coordinates and no path")
            if any(len(p) != 2 for p in self.coordinates):
                raise ValueError("coordinates must be [x, y] pairs")
        elif self.path is None or self.coordinates is not None:
            raise ValueError(f"type {self.type} needs path and no coordinates")
        return self


class LineSource(_Strict):
    name: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    points: list[list[Number]] = Field(min_length=2, description="m; polyline [[x, y], ...] in dem.crs, strictly inside the domain")


class DomainSection(_Strict):
    polygon: PolygonSource
    holes: list[PolygonSource] = Field(default_factory=list, description="unmeshed areas (walls); polygon interiors are holes too")
    constraints: list[LineSource] = Field(default_factory=list, description="mesh-conforming lines, e.g. dike crests")
    simplify_tolerance_m: Number | None = Field(None, ge=0.0, le=1000.0, description="m; Douglas-Peucker tolerance, null = none")
    min_vertex_spacing_m: Number | None = Field(None, gt=0.0, le=1000.0, description="m; drop ring vertices closer than this, null = none")


class DemSection(_Strict):
    path: StrictStr = Field(min_length=1, description="GeoTIFF, relative to the case config or absolute")
    crs: StrictStr = Field(min_length=1, description="projected metric CRS of the domain, sections and mesh, e.g. EPSG:4548")
    sha256: StrictStr | None = Field(None, pattern=r"^[0-9a-f]{64}$", description="optional expected DEM SHA-256 (computed when null)")
    vertical_unit: Literal["m"] | None = Field(None, description="declare only when the GeoTIFF has no vertical unit; requires vertical_unit_evidence")
    vertical_unit_evidence: StrictStr | None = Field(None, min_length=1, description="relative evidence file for vertical_unit")
    vertical_datum: StrictStr | None = Field(None, min_length=1, description="declared datum label; requires vertical_datum_evidence")
    vertical_datum_evidence: StrictStr | None = Field(None, min_length=1, description="relative evidence file for vertical_datum")

    @model_validator(mode="after")
    def _datum(self) -> "DemSection":
        if (self.vertical_datum is None) != (self.vertical_datum_evidence is None):
            raise ValueError("vertical_datum and vertical_datum_evidence must be given together")
        if (self.vertical_unit is None) != (self.vertical_unit_evidence is None):
            raise ValueError("vertical_unit and vertical_unit_evidence must be given together")
        for name in ("vertical_unit_evidence", "vertical_datum_evidence"):
            value = getattr(self, name)
            if value is not None and (Path(value).is_absolute() or PureWindowsPath(value).drive or ".." in Path(value).parts):
                raise ValueError(f"dem.{name} must be a relative path under the case config directory")
        return self


class MeshSettings(_Strict):
    default_size_m: Number = Field(50.0, gt=0.0, le=10_000.0, description="m; target edge length")
    minimum_size_m: Number = Field(1.0, gt=0.0, le=10_000.0, description="m; smallest edge length")
    boundary_adaptive: StrictBool = Field(True, description="grade sizes from short boundary segments")
    grading_ratio: Number = Field(0.3, gt=0.0, le=1.0, description="1; boundary-adaptive size growth per metre")
    maximum_nodes: StrictInt = Field(2_000_000, ge=3, le=10_000_000, description="count")
    algorithm_2d: StrictInt = Field(5, ge=1, le=11, description="Gmsh Mesh.Algorithm (5 = Delaunay)")

    @model_validator(mode="after")
    def _sizes(self) -> "MeshSettings":
        if self.minimum_size_m > self.default_size_m:
            raise ValueError("mesh.minimum_size_m must not exceed mesh.default_size_m")
        return self


class TerrainSettings(_Strict):
    max_source_pixels: StrictInt = Field(1_000_000_000, ge=1, le=1_000_000_000, description="count")
    max_candidate_overlap_checks: StrictInt = Field(1_000_000_000, ge=1, le=1_000_000_000, description="count")


class SectionItem(_Strict):
    name: StrictStr = Field(pattern=r"^[A-Za-z0-9_]{1,32}$", description="section key")
    role: Literal["inflow", "outflow"]
    xy: list[Number] = Field(min_length=2, max_length=2, description="m; point near the river crossing of the domain boundary")
    column: StrictStr = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="discharge CSV column (m3/s, magnitudes)")
    name_zh: StrictStr = Field("", max_length=200)
    status: Literal["RECONSTRUCTED_NOT_MEASURED", "MEASURED", "COMPUTED_OUTFLOW"] = "RECONSTRUCTED_NOT_MEASURED"
    distribution: Literal["length", "conveyance_depth_5_3"] | None = None
    boundary_type: Literal["prescribed_outflow", "free_outflow"] | None = Field(None, description="outflow only; default prescribed_outflow")
    discharge_coefficient: Number | None = Field(None, gt=0.0, le=1.5, description="free_outflow only")
    crest_elevation_m: Number | None = Field(None, ge=-1000.0, le=10_000.0, description="free_outflow only; m, DEM datum")

    @model_validator(mode="after")
    def _roles(self) -> "SectionItem":
        if self.role == "inflow" and (self.boundary_type is not None or self.discharge_coefficient is not None or self.crest_elevation_m is not None):
            raise ValueError(f"section {self.name}: boundary_type/discharge_coefficient/crest_elevation_m are outflow-only")
        if self.boundary_type != "free_outflow" and (self.discharge_coefficient is not None or self.crest_elevation_m is not None):
            raise ValueError(f"section {self.name}: discharge_coefficient/crest_elevation_m need boundary_type free_outflow")
        if self.boundary_type == "free_outflow" and self.distribution is not None:
            raise ValueError(f"section {self.name}: distribution is not allowed for free_outflow")
        return self


class SectionsSettings(_Strict):
    items: list[SectionItem] = Field(min_length=1)
    max_point_distance_m: Number = Field(100.0, gt=0.0, le=10_000.0, description="m; section point to nearest exterior edge")
    rule: dict[str, Any] = Field(default_factory=dict, description="river_section_selection ChannelRuleParameters overrides")

    @field_validator("items")
    @classmethod
    def _unique(cls, value: list[SectionItem]) -> list[SectionItem]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError(f"section names must be unique: {names}")
        return value


class RunSettings(_Strict):
    hours: StrictInt = Field(ge=1, le=8760, description="h; hourly output frames 0..hours")
    manning_n: Number = Field(0.035, gt=0.0, le=1.0, description="s m^-1/3")
    initial_depth_m: Number = Field(0.0, ge=0.0, le=1000.0, description="m")
    time_origin: StrictStr = Field(description="ISO 8601 with UTC offset")
    cfl_number: Number = Field(0.5, gt=0.0, le=1.0)
    kernel: Literal["auto", "numpy", "numba"] = "auto"
    checkpoint_interval_s: Number | None = Field(21600.0, gt=0.0, description="s; null disables --resume")
    long_run_options: dict[str, Any] = Field(default_factory=dict, description="extra triangular-long-run-options-v1 fields")


class OutputSettings(_Strict):
    root: StrictStr = Field("work/results", min_length=1, description="results root; run dir = <root>/<run id>")
    run_id: StrictStr | None = Field(None, description="run directory name; null = Beijing date + ns")
    profile: dict[str, Any] | StrictStr | None = None
    export: StrictBool = True


class CaseConfig(_Strict):
    format: Literal["case-pipeline-config-v1"] = CASE_FORMAT
    schema_version: Literal["1.0"] = CASE_VERSION
    case_id: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    description: StrictStr | None = None
    work_dir: StrictStr = Field(min_length=1, description="stage artifacts directory, relative to the case config or absolute")
    dem: DemSection
    domain: DomainSection
    mesh: MeshSettings = Field(default_factory=MeshSettings)
    terrain: TerrainSettings = Field(default_factory=TerrainSettings)
    # sections / forcing / run: required from stage "sections" on; omit all three for a mesh-only case
    # (stages geometry, mesh, terrain).
    sections: SectionsSettings | None = None
    forcing: dict[str, StrictStr] | None = Field(None, description="{'path': discharge CSV with time_s + section columns}")
    run: RunSettings | None = None
    output: OutputSettings = Field(default_factory=OutputSettings)

    @field_validator("forcing")
    @classmethod
    def _forcing(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None and (set(value) != {"path"} or not value["path"].strip()):
            raise ValueError("forcing must be {'path': <csv>}")
        return value

    @property
    def mesh_only(self) -> bool:
        return self.sections is None and self.forcing is None and self.run is None

    def require_hydro(self, stage: str) -> None:
        missing = [name for name in ("sections", "forcing", "run") if getattr(self, name) is None]
        if missing:
            raise CasePipelineError(f"stage {stage!r} needs {', '.join(missing)} in the case config; "
                                    "a mesh-only config can run --stage geometry, mesh or terrain")


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or PureWindowsPath(value).drive:
        return path
    return (base / path).resolve()


def load_case_config(path: str | Path) -> tuple[CaseConfig, Path]:
    source = Path(path).resolve()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise CasePipelineError(f"cannot read case config {source}: {exc}") from exc
    try:
        if source.suffix.lower() in (".yaml", ".yml"):
            import yaml
            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
    except Exception as exc:
        raise CasePipelineError(f"case config {source} is not valid YAML/JSON: {exc}") from exc
    try:
        return CaseConfig.model_validate(raw), source.parent
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise CasePipelineError(f"invalid {CASE_FORMAT}: {details}") from exc


def case_config_json_schema() -> dict[str, Any]:
    return CaseConfig.model_json_schema()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def _crs_equivalent(left: str, right: str) -> bool:
    from .river_section_selection import _crs_equivalent_text
    return _crs_equivalent_text(left, right)


def _check_prj(path: Path, crs: str) -> None:
    prj = path.with_suffix(".prj")
    if prj.is_file() and not _crs_equivalent(prj.read_text(encoding="utf-8", errors="replace"), crs):
        raise CasePipelineError(f"{path.name}: .prj CRS differs from dem.crs {crs}; reproject outside the pipeline (no CRS conversion here)")


def _polygon_from_source(source: PolygonSource, base: Path, crs: str) -> Any:
    from shapely.geometry import MultiPolygon, Polygon, shape
    from shapely.ops import unary_union

    if source.type == "coordinates":
        geometry = Polygon([(float(x), float(y)) for x, y in source.coordinates])
    else:
        path = _resolve(base, source.path)
        if not path.is_file():
            raise CasePipelineError(f"polygon file not found: {path}")
        parts = []
        if source.type == "geojson":
            data = json.loads(path.read_text(encoding="utf-8"))
            declared = (data.get("crs") or {}).get("properties", {}).get("name")
            if declared and not _crs_equivalent(declared, crs):
                raise CasePipelineError(f"{path.name}: GeoJSON crs {declared} differs from dem.crs {crs}")
            features = data.get("features") if data.get("type") == "FeatureCollection" else [data]
            for feature in features:
                geom = feature.get("geometry", feature)
                if geom and geom.get("type") in ("Polygon", "MultiPolygon"):
                    parts.append(shape(geom))
        else:
            import shapefile
            _check_prj(path, crs)
            for record in shapefile.Reader(str(path)).shapes():
                geo = record.__geo_interface__
                if geo["type"] in ("Polygon", "MultiPolygon"):
                    parts.append(shape(geo))
        if not parts:
            raise CasePipelineError(f"{path.name}: no polygon features")
        geometry = unary_union(parts)
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    if isinstance(geometry, MultiPolygon):
        sizes = sorted((g.area for g in geometry.geoms), reverse=True)
        raise CasePipelineError(f"domain must be one connected polygon; got {len(sizes)} parts with areas {[round(a, 1) for a in sizes[:5]]} m2")
    if geometry.is_empty or geometry.area <= 0.0:
        raise CasePipelineError("polygon is empty")
    return geometry


def _thin_ring(coords: Sequence[tuple[float, float]], spacing: float) -> list[tuple[float, float]]:
    ring = list(coords[:-1]) if coords[0] == coords[-1] else list(coords)
    kept = [ring[0]]
    for point in ring[1:]:
        if math.dist(point, kept[-1]) >= spacing:
            kept.append(point)
    while len(kept) > 3 and math.dist(kept[-1], kept[0]) < spacing:
        kept.pop()
    return kept


def _clean_polygon(polygon: Any, tolerance: float | None, spacing: float | None) -> Any:
    from shapely.geometry import Polygon
    result = polygon
    if tolerance:
        result = result.simplify(float(tolerance), preserve_topology=True)
    if spacing:
        shell = _thin_ring(list(result.exterior.coords), float(spacing))
        holes = [_thin_ring(list(r.coords), float(spacing)) for r in result.interiors]
        result = Polygon(shell, [h for h in holes if len(h) >= 3])
    if not result.is_valid or result.is_empty:
        raise CasePipelineError("domain simplification produced an invalid polygon; lower simplify_tolerance_m / min_vertex_spacing_m")
    return result


# ---------------------------------------------------------------------------
# Stage plumbing
# ---------------------------------------------------------------------------
class Pipeline:
    def __init__(self, config: CaseConfig, base: Path, *, log: Any = None, force: bool = False):
        self.config, self.base, self.force = config, base, force
        self.work = _resolve(base, config.work_dir)
        self.dem_path = _resolve(base, config.dem.path)
        self.log = log or (lambda message: None)
        self._dem_sha: str | None = None

    # -- identity -----------------------------------------------------------
    def dem_sha256(self) -> str:
        if self._dem_sha is None:
            if not self.dem_path.is_file():
                raise CasePipelineError(f"DEM not found: {self.dem_path}")
            actual = _sha_file(self.dem_path)
            if self.config.dem.sha256 is not None and actual != self.config.dem.sha256:
                raise CasePipelineError(f"DEM sha256 {actual} differs from dem.sha256 {self.config.dem.sha256}")
            self._dem_sha = actual
        return self._dem_sha

    def _manifest(self, stage: str) -> Path:
        return self.work / stage / "stage.json"

    def _cached(self, stage: str, key: str) -> dict[str, Any] | None:
        manifest = self._manifest(stage)
        if self.force or not manifest.is_file():
            return None
        data = json.loads(manifest.read_text(encoding="utf-8"))
        # outputs are stored relative to work_dir so a work_dir can be copied to another machine/OS
        outputs = {k: (self.work / Path(PureWindowsPath(v).as_posix())) for k, v in data.get("outputs", {}).items()}
        if data.get("input_key") != key or not all(p.is_file() for p in outputs.values()):
            return None
        data["outputs"] = {k: str(v) for k, v in outputs.items()}
        self.log(f"[{stage}] cached (inputs unchanged)")
        return data

    def _done(self, stage: str, key: str, outputs: Mapping[str, Path], summary: Mapping[str, Any], started: float) -> dict[str, Any]:
        data = {"stage": stage, "input_key": key, "outputs": {k: Path(v).resolve().relative_to(self.work.resolve()).as_posix() for k, v in outputs.items()},
                "summary": dict(summary), "seconds": round(time.perf_counter() - started, 2)}
        _write_json(self._manifest(stage), data)
        data["outputs"] = {k: str(Path(v).resolve()) for k, v in outputs.items()}
        self.log(f"[{stage}] done in {data['seconds']} s: {json.dumps(data['summary'], ensure_ascii=False, default=str)[:400]}")
        return data

    # -- geometry -------------------------------------------------------------
    def geometry(self) -> dict[str, Any]:
        cfg = self.config
        inputs: dict[str, Any] = {"domain": cfg.domain.model_dump(mode="json"), "crs": cfg.dem.crs}
        for i, src in enumerate([cfg.domain.polygon, *cfg.domain.holes]):
            if src.path:
                inputs[f"file_{i}"] = _sha_file(_resolve(self.base, src.path))
        key = _sha_json(inputs)
        cached = self._cached("geometry", key)
        if cached:
            return cached
        started = time.perf_counter()
        domain = _polygon_from_source(cfg.domain.polygon, self.base, cfg.dem.crs)
        for hole_source in cfg.domain.holes:
            hole = _polygon_from_source(hole_source, self.base, cfg.dem.crs)
            if not hole.within(domain) or hole.distance(domain.exterior) <= 0.0:
                raise CasePipelineError("every domain.holes polygon must lie strictly inside the domain polygon")
            domain = domain.difference(hole)
        domain = _clean_polygon(domain, cfg.domain.simplify_tolerance_m, cfg.domain.min_vertex_spacing_m)
        from shapely.geometry import LineString
        constraints = []
        for line in cfg.domain.constraints:
            geom = LineString([(float(x), float(y)) for x, y in line.points])
            if not domain.contains(geom):
                raise CasePipelineError(f"constraint {line.name} must lie strictly inside the domain (not on/over its boundary or holes)")
            constraints.append({"name": line.name, "points": [[float(x), float(y)] for x, y in line.points]})
        geometry = {
            "crs": cfg.dem.crs,
            "outer_boundary": [[round(x, 3), round(y, 3)] for x, y in list(domain.exterior.coords)[:-1]],
            "holes": [[[round(x, 3), round(y, 3)] for x, y in list(r.coords)[:-1]] for r in domain.interiors],
            "constraints": constraints,
            "area_m2": float(domain.area),
        }
        out = self.work / "geometry" / "geometry.json"
        _write_json(out, geometry)
        return self._done("geometry", key, {"geometry": out}, {
            "area_km2": round(domain.area / 1e6, 4), "outer_vertices": len(geometry["outer_boundary"]),
            "holes": len(geometry["holes"]), "constraints": len(constraints)}, started)

    # -- mesh -----------------------------------------------------------------
    def mesh(self) -> dict[str, Any]:
        geo_stage = self.geometry()
        geometry_path = Path(geo_stage["outputs"]["geometry"])
        key = _sha_json({"geometry": _sha_file(geometry_path), "mesh": self.config.mesh.model_dump(mode="json")})
        cached = self._cached("mesh", key)
        if cached:
            return cached
        started = time.perf_counter()
        from .mesh_generation import generate_gmsh_mesh
        from .mesh_preparation import GmshMeshConfig

        geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
        settings = self.config.mesh
        gmsh_config = GmshMeshConfig(
            type="gmsh", outer_boundary=geometry["outer_boundary"], holes=geometry["holes"],
            constraints=[{"name": c["name"], "points": c["points"]} for c in geometry["constraints"]],
            default_size_m=float(settings.default_size_m), minimum_size_m=float(settings.minimum_size_m),
            maximum_nodes=int(settings.maximum_nodes))
        plan = {"format": "triangle-mesh-algorithm-plan-v1", "schema_version": "1.0", "algorithm_2d": int(settings.algorithm_2d),
                "smoothing": 1, "algorithm_switch_on_failure": 0, "optimizer": "none", "iterations": 0, "force": False,
                "entity_scope": "active_surface"}
        receipt: dict[str, Any] = {}
        sizing = {"enabled": True, "grading_ratio": float(settings.grading_ratio)} if settings.boundary_adaptive else None
        try:
            mesh = generate_gmsh_mesh(gmsh_config, coordinate_system=self.config.dem.crs, geometry_tolerance_m=1e-8,
                                      minimum_face_area_m2=1e-12, mesh_algorithm_plan=plan, generation_receipt=receipt,
                                      boundary_adaptive_sizing=sizing)
        except Exception as exc:
            raise CasePipelineError(f"mesh generation failed: {type(exc).__name__}: {exc}") from exc
        xy = np.asarray(mesh.node_xy, dtype=float)
        faces = np.asarray(mesh.face_nodes, dtype=np.int64)
        quality = mesh_quality(xy, faces)
        mesh_id = f"{self.config.case_id}_" + hashlib.sha256(faces.tobytes() + xy.tobytes()).hexdigest()[:16]
        artifact = {
            "format": MESH_ARTIFACT_FORMAT, "mesh_only": True, "coordinate_system": self.config.dem.crs,
            "geometry_tolerance_m": 1e-8, "minimum_face_area_m2": 1e-12, "mesh_id": mesh_id,
            "node_xy": xy.tolist(), "face_nodes": faces.tolist(), "face_id": [int(v) for v in mesh.face_id],
            "provenance": {"geometry": geometry,"route": "hydro_pipeline.case_pipeline -> mesh_generation.generate_gmsh_mesh", "case_id": self.config.case_id,
                           "mesh_settings": settings.model_dump(mode="json"), "mesh_algorithm_plan": plan, "quality": quality,
                           "receipt": receipt},
        }
        out = self.work / "mesh" / "mesh.json"
        _write_json(out, artifact)
        return self._done("mesh", key, {"mesh": out}, {"mesh_id": mesh_id, "nodes": int(xy.shape[0]), "faces": int(faces.shape[0]), **quality}, started)

    # -- terrain --------------------------------------------------------------
    def terrain(self) -> dict[str, Any]:
        mesh_stage = self.mesh()
        mesh_path = Path(mesh_stage["outputs"]["mesh"])
        dem = self.config.dem
        key = _sha_json({"mesh": _sha_file(mesh_path), "dem": self.dem_sha256(), "crs": dem.crs, "datum": dem.vertical_datum, "unit": dem.vertical_unit,
                         "evidence": [_sha_file(_resolve(self.base, e)) for e in (dem.vertical_unit_evidence, dem.vertical_datum_evidence) if e],
                         "settings": self.config.terrain.model_dump(mode="json")})
        cached = self._cached("terrain", key)
        if cached:
            return cached
        started = time.perf_counter()
        from .dem_contract import GeoTIFFDEMReader
        from .river_section_selection import load_mesh_artifact
        from .terrain_windowed_mapping import (WindowedTerrainMappingError, WindowedTriangleMappingConfig,
                                               map_triangular_mesh_from_windows, write_windowed_mapping_report)

        mesh, _ = load_mesh_artifact(mesh_path)
        options: dict[str, Any] = {"max_source_pixels": int(self.config.terrain.max_source_pixels),
                                   "max_candidate_overlap_checks": int(self.config.terrain.max_candidate_overlap_checks),
                                   "coverage_policy": "diagnostic_only"}
        if dem.vertical_datum is not None:
            evidence = _resolve(self.base, dem.vertical_datum_evidence)
            options.update(source_vertical_datum_declaration=dem.vertical_datum,
                           source_vertical_datum_evidence_path=dem.vertical_datum_evidence,
                           source_vertical_datum_evidence_sha256=_sha_file(evidence))
        if dem.vertical_unit is not None:
            evidence = _resolve(self.base, dem.vertical_unit_evidence)
            options.update(source_vertical_unit_declaration=dem.vertical_unit,
                           source_vertical_unit_evidence_path=dem.vertical_unit_evidence,
                           source_vertical_unit_evidence_sha256=_sha_file(evidence))
        reader = EquivalentCRSReader(GeoTIFFDEMReader(), dem.crs)
        self.log(f"[terrain] exact mapping of {mesh.n_face} faces from {self.dem_path.name} (can take hours for 1 m DEMs)")
        try:
            _, report = map_triangular_mesh_from_windows(reader, self.dem_path, mesh, source_sha256=self.dem_sha256(),
                                                         config=WindowedTriangleMappingConfig(**options), config_base=self.base)
        except WindowedTerrainMappingError as exc:
            hint = (" -- faces outside the DEM or over NoData are rejected (NoData is never filled); shrink the domain or supply a covering DEM"
                    if "zero valid DEM overlap" in str(exc) else "")
            raise CasePipelineError(f"terrain mapping failed: {str(exc)[:600]}{hint}") from exc
        rows = report["faces"]
        z = np.array([np.nan if r["elevation_m"] is None else float(r["elevation_m"]) for r in rows])
        face_id = np.array([int(r["face_id"]) for r in rows], dtype=np.int64)
        cover = np.array([float(r["coverage_ratio"]) for r in rows])
        missing = int(np.isnan(z).sum())
        if missing:
            bad = face_id[np.isnan(z)][:10].tolist()
            raise CasePipelineError(f"{missing} faces have no valid DEM value (outside the DEM or all NoData), e.g. face_id {bad}; "
                                    "shrink the domain or supply a DEM covering it (NoData is never filled)")
        out = self.work / "terrain" / "terrain_faces.npz"
        out.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(out, face_id=face_id, elevation_m=z, coverage=cover)
        report_path = write_windowed_mapping_report(self.work / "terrain" / "mapping_report.json", report)
        return self._done("terrain", key, {"terrain": out, "report": Path(report_path)}, {
            "faces": int(z.size), "z_min_m": float(z.min()), "z_max_m": float(z.max()),
            "faces_partial_coverage": int((cover < 1.0 - 1e-9).sum()), "vertical_datum": report.get("vertical_datum"),
            "alias": reader.evidence}, started)

    # -- sections ---------------------------------------------------------------
    def sections(self) -> dict[str, Any]:
        self.config.require_hydro("sections")
        mesh_stage = self.mesh()
        mesh_path = Path(mesh_stage["outputs"]["mesh"])
        settings = self.config.sections
        request_doc = {
            "format": "assumed-river-sections-request-v1", "schema_version": "1.0", "crs": self.config.dem.crs,
            "max_point_distance_m": float(settings.max_point_distance_m), "dem_sha256": self.dem_sha256(), "rule": settings.rule,
            "sections": [{"name": s.name, "role": s.role, "xy": [float(s.xy[0]), float(s.xy[1])],
                          "discharge_series_column": s.column, "name_zh": s.name_zh or s.name} for s in settings.items],
        }
        key = _sha_json({"mesh": _sha_file(mesh_path), "request": request_doc})
        cached = self._cached("sections", key)
        if cached:
            return cached
        started = time.perf_counter()
        from .river_section_selection import (RiverSectionSelectionError, RiverSectionsRequest, load_mesh_artifact,
                                              select_river_sections, write_audit)
        mesh, mesh_sha = load_mesh_artifact(mesh_path)
        try:
            request = RiverSectionsRequest.from_mapping(request_doc)
            audit = select_river_sections(request, mesh, self.dem_path, mesh_artifact=str(mesh_path).replace("\\", "/"),
                                          mesh_artifact_sha256=mesh_sha, expected_dem_sha256=self.dem_sha256())
        except RiverSectionSelectionError as exc:
            raise CasePipelineError(f"section selection failed: {exc}") from exc
        out = self.work / "sections" / "sections_audit.json"
        _write_json(self.work / "sections" / "request.json", request_doc)
        write_audit(audit, out)
        summary = {name: {"edges": s["edge_count"], "width_m": round(s["width_m"], 1)} for name, s in audit["sections"].items()}
        return self._done("sections", key, {"sections": out}, summary, started)

    # -- driver config ------------------------------------------------------------
    def driver_config(self) -> dict[str, Any]:
        cfg = self.config
        cfg.require_hydro("config")
        mesh_stage, terrain_stage, sections_stage = self.mesh(), self.terrain(), self.sections()
        forcing = _resolve(self.base, cfg.forcing["path"])
        if not forcing.is_file():
            raise CasePipelineError(f"forcing CSV not found: {forcing}")
        mesh_path = Path(mesh_stage["outputs"]["mesh"])
        status, distribution, outflow = {}, {}, {}
        for item in cfg.sections.items:
            status[item.name] = item.status
            if item.distribution is not None:
                distribution[item.name] = item.distribution
            if item.role == "outflow" and item.boundary_type is not None:
                outflow[item.name] = {"boundary_type": item.boundary_type, "discharge_coefficient": item.discharge_coefficient,
                                      "crest_elevation_m": item.crest_elevation_m}
        run = cfg.run
        options = dict(run.long_run_options)
        options.setdefault("kernel", run.kernel)
        options.setdefault("cfl_number", float(run.cfl_number))
        options.setdefault("progress_interval_steps", 10000)
        driver = {
            "format": "large-run-driver-config-v1", "schema_version": "1.0",
            "description": cfg.description or f"case_pipeline case {cfg.case_id}",
            "mesh": {"path": str(mesh_path), "expected_sha256": _sha_file(mesh_path)},
            "terrain": {"path": terrain_stage["outputs"]["terrain"], "dem_path": str(self.dem_path), "dem_sha256": self.dem_sha256(),
                        "mapping_report_path": terrain_stage["outputs"]["report"],
                        "method": "hydro_pipeline.terrain_windowed_mapping exact area-weighted (case_pipeline)",
                        "vertical_datum": cfg.dem.vertical_datum, "vertical_unit": "m"},
            "sections": {"path": sections_stage["outputs"]["sections"], "discharge_series_status": status,
                         "distribution": distribution, "outflow_boundary": outflow},
            "forcing": {"path": str(forcing), "rainfall": None},
            "run": {"hours": int(run.hours), "output_times_s": None, "initial_depth_m": float(run.initial_depth_m),
                    "manning_n": float(run.manning_n), "long_run_options": options,
                    "checkpoint_interval_s": None if run.checkpoint_interval_s is None else float(run.checkpoint_interval_s),
                    "time_origin": run.time_origin, "run_id": None, "case_id": cfg.case_id, "coordinate_system": cfg.dem.crs},
            "output": {"root": str(_resolve(self.base, cfg.output.root)), "script_id": cfg.output.run_id,
                       "profile": cfg.output.profile, "export": cfg.output.export},
        }
        out = self.work / "driver_config.json"
        _write_json(out, driver)
        self.log(f"[config] wrote {out}")
        return {"stage": "config", "outputs": {"driver_config": str(out)}}

    # -- run --------------------------------------------------------------------------
    def run(self, *, resume: bool = False, run_id: str | None = None, check_only: bool = False) -> dict[str, Any]:
        from .large_run_driver import check_case, run_large_case
        path = self.driver_config()["outputs"]["driver_config"]
        if check_only:
            return check_case(path, resume=resume, script_id=run_id)
        return run_large_case(path, resume=resume, log=self.log, script_id=run_id)

    # -- light preflight ----------------------------------------------------------------
    def check(self) -> dict[str, Any]:
        """Config + input files + DEM CRS/extent vs domain; no meshing or mapping."""
        report: dict[str, Any] = {"format": "case-pipeline-check-v1", "case_id": self.config.case_id, "errors": [], "warnings": []}
        errors = report["errors"]
        try:
            from pyproj import CRS
            crs = CRS.from_user_input(self.config.dem.crs)
            if not crs.is_projected:
                errors.append(f"dem.crs {self.config.dem.crs} is not a projected CRS")
        except Exception as exc:
            errors.append(f"dem.crs cannot be parsed: {exc}")
        try:
            from .dem_contract import GeoTIFFDEMReader
            meta = GeoTIFFDEMReader().read_metadata(self.dem_path)
            if not _crs_equivalent(meta.crs or "", self.config.dem.crs):
                errors.append(f"DEM CRS is not equivalent to dem.crs {self.config.dem.crs}")
            left, bottom, right, top = meta.xmin, meta.ymin, meta.xmax, meta.ymax
            report["dem"] = {"path": str(self.dem_path), "size": [meta.width, meta.height], "bounds": [left, bottom, right, top],
                             "vertical_unit": meta.vertical_unit, "vertical_datum": meta.vertical_datum}
            domain = _polygon_from_source(self.config.domain.polygon, self.base, self.config.dem.crs)
            report["domain_area_km2"] = round(domain.area / 1e6, 4)
            from shapely.geometry import Point, box
            if not box(left, bottom, right, top).contains(domain):
                errors.append("domain polygon extends outside the DEM bounds")
            for item in (self.config.sections.items if self.config.sections is not None else []):
                distance = domain.exterior.distance(Point(item.xy))
                if distance > float(self.config.sections.max_point_distance_m):
                    errors.append(f"section {item.name} is {distance:.1f} m from the domain boundary (> max_point_distance_m)")
        except CasePipelineError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"DEM/domain check failed: {type(exc).__name__}: {exc}")
        report["mesh_only"] = self.config.mesh_only
        if self.config.mesh_only:
            pass  # geometry / mesh / terrain only: no sections or discharge to check
        elif self.config.sections is None or self.config.forcing is None or self.config.run is None:
            errors.append("sections, forcing and run must be given together (or all omitted for a mesh-only case)")
        elif not (forcing := _resolve(self.base, self.config.forcing["path"])).is_file():
            errors.append(f"forcing CSV not found: {forcing}")
        else:
            import csv
            with open(forcing, encoding="utf-8-sig", newline="") as stream:
                header = next(csv.reader(stream), [])
            missing = [c for c in ["time_s", *(i.column for i in self.config.sections.items)] if c not in header]
            if missing:
                errors.append(f"forcing CSV lacks columns {missing}")
        report["stages_cached"] = [s for s in STAGES[:4] if self._manifest(s).is_file()]
        report["status"] = "ok" if not errors else "invalid"
        return report


class EquivalentCRSReader:
    """DEM reader wrapper that relabels a pyproj-equivalent DEM CRS as the case CRS string (no coordinate change)."""

    def __init__(self, reader: Any, target_crs: str):
        self._reader, self._target = reader, target_crs
        self.evidence: dict[str, Any] | None = None

    def _metadata(self, metadata: Any) -> Any:
        if self.evidence is None:
            source = metadata.crs or ""
            if not _crs_equivalent(source, self._target):
                raise CasePipelineError(f"DEM CRS is not equivalent to dem.crs {self._target}; reproject the DEM outside the pipeline")
            self.evidence = {"source_crs_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), "target_crs": self._target,
                             "policy": "pyproj equivalence (bound wrapper unwrapped, axis order ignored); coordinates unchanged"}
        return replace(metadata, crs=self._target)

    def read_metadata(self, source: str | Path) -> Any:
        return self._metadata(self._reader.read_metadata(source))

    def iter_windows_selected(self, source: str | Path, **kwargs: Any) -> Any:
        include = kwargs.get("include_window")
        child = dict(kwargs)
        if callable(include):
            child["include_window"] = lambda metadata, window: include(self._metadata(metadata), window)
        iterator = iter(self._reader.iter_windows_selected(source, **child))
        try:
            for window, chunk in iterator:
                yield window, replace(chunk, metadata=self._metadata(chunk.metadata))
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()


def mesh_quality(xy: np.ndarray, faces: np.ndarray) -> dict[str, Any]:
    p = xy[faces]
    area = 0.5 * np.abs((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1]))
    edges = np.stack([np.hypot(*(p[:, (i + 1) % 3] - p[:, i]).T) for i in range(3)], axis=1)
    q = 4.0 * math.sqrt(3.0) * area / (edges ** 2).sum(axis=1)
    r = area / edges.sum(axis=1)
    return {"min_quality": float(q.min()), "faces_quality_lt_0_1": int((q < 0.1).sum()), "min_area_m2": float(area.min()),
            "min_area_over_perimeter_m": float(r.min()), "area_km2": float(area.sum() / 1e6)}


def run_pipeline(config_path: str | Path, *, stage: str = "run", force: bool = False, resume: bool = False,
                 run_id: str | None = None, log: Any = None) -> dict[str, Any]:
    """Run every stage up to and including ``stage``; returns that stage's manifest/report."""
    if stage not in STAGES:
        raise CasePipelineError(f"stage must be one of {STAGES}")
    config, base = load_case_config(config_path)
    pipeline = Pipeline(config, base, log=log, force=force)
    if stage == "run":
        return pipeline.run(resume=resume, run_id=run_id)
    if stage == "config":
        return pipeline.driver_config()
    return getattr(pipeline, stage)()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hydro_pipeline.case_pipeline",
                                     description="Any DEM + domain + sections + discharge CSV -> mesh, terrain, sections, run, bundle.")
    parser.add_argument("config", help=f"{CASE_FORMAT} YAML/JSON")
    parser.add_argument("--stage", choices=STAGES, default="run", help="run all stages up to this one (default run)")
    parser.add_argument("--check", action="store_true", help="light preflight (config, files, DEM CRS/extent); no meshing")
    parser.add_argument("--force", action="store_true", help="recompute cached stages")
    parser.add_argument("--resume", action="store_true", help="resume the run stage from its newest checkpoint")
    parser.add_argument("--id", dest="run_id", default=None, help="run directory name under output.root (overrides output.run_id)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--print-schema", action="store_true", help="print the case config JSON schema and exit")
    args = parser.parse_args(argv)
    if args.print_schema:
        print(json.dumps(case_config_json_schema(), ensure_ascii=False, indent=1))
        return 0
    log = None if args.quiet else (lambda message: print(message, file=sys.stderr, flush=True))
    try:
        if args.check:
            config, base = load_case_config(args.config)
            report = Pipeline(config, base, log=log).check()
            print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
            return 0 if report["status"] == "ok" else 1
        report = run_pipeline(args.config, stage=args.stage, force=args.force, resume=args.resume, run_id=args.run_id, log=log)
    except CasePipelineError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # driver/solver errors keep their own types
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True, default=str))
    return 0 if report.get("status", "complete") in ("complete", "ok") or args.stage != "run" else 1


__all__ = ["CASE_FORMAT", "STAGES", "CaseConfig", "CasePipelineError", "EquivalentCRSReader", "Pipeline", "case_config_json_schema",
           "load_case_config", "main", "mesh_quality", "run_pipeline"]


if __name__ == "__main__":
    raise SystemExit(main())
