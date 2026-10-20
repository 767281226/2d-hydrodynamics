"""Strict geometry and evidence helpers for excluding a no-inundation security area.

Only vector geometry and CRS metadata are read here. The module never opens
DBF business records, DEM pixels, or solver state, and it does not repair
invalid polygons with buffering or coordinate snapping.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator

from .config import Text
from .triangular_mesh import TriangularMesh, TriangularMeshError


class SecurityExclusionError(ValueError):
    """Raised when security-exclusion geometry or domain evidence is unsupported."""


class _SecurityStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True, allow_inf_nan=False)


class SecurityExclusionInput(_SecurityStrictModel):
    format: Literal["security-area-exclusion-v1"]
    schema_version: Literal["1.0"]
    enabled: StrictBool
    source_file: Text = Field(description="Relative path to the source Polygon/MultiPolygon SHP or GeoJSON; DBF attributes are not used")
    source_crs: Text = Field(description="Explicit source CRS; must agree with SHP PRJ when the source is a Shapefile")
    constraint: Literal["excluded_closed_wall"]
    domain_geometry_file: Text | None = Field(default=None, description="Required for imported P2 meshes: source Polygon/MultiPolygon geometry D")
    domain_crs: Text | None = Field(default=None, description="Explicit CRS of domain_geometry_file; no CRS is inferred")

    @field_validator("source_file")
    @classmethod
    def source_is_relative(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("security source_file must be relative to the config that declares it")
        return value

    @field_validator("domain_geometry_file")
    @classmethod
    def domain_file_is_relative(cls, value: str | None) -> str | None:
        if value is not None and (Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive):
            raise ValueError("domain_geometry_file must be relative to the security-exclusion config")
        return value

    @model_validator(mode="after")
    def domain_file_crs_pair(self) -> "SecurityExclusionInput":
        if (self.domain_geometry_file is None) != (self.domain_crs is None):
            raise ValueError("domain_geometry_file and domain_crs must be supplied together")
        if self.domain_crs is not None and not self.domain_crs.strip():
            raise ValueError("domain_crs must be a non-empty CRS declaration")
        return self


class SecurityExclusionEvidence(_SecurityStrictModel):
    format: Literal["security-exclusion-evidence-v1"]
    schema_version: Literal["1.0"]
    enabled: Literal[True]
    constraint: Literal["excluded_closed_wall"]
    mesh_id: Text
    mesh_crs: Text
    topology_fingerprint: Text
    security_input_config_path: Text
    security_input_config_sha256: Text
    source_file_path: Text
    source_file_sha256: Text
    source_shx_sha256: Text | None
    source_prj_sha256: Text | None
    source_crs: Text
    source_feature_count: StrictInt = Field(ge=1)
    source_feature_indices: list[StrictInt] = Field(min_length=1)
    source_geometry_sha256: Text
    source_union_geometry_sha256: Text
    domain_source_kind: Literal["P2_GMSH_CONFIG", "DOMAIN_GEOJSON", "WORKFLOW_CASE_DOMAIN"]
    domain_source_path: Text
    domain_source_sha256: Text
    domain_source_crs: Text
    domain_geometry_sha256: Text
    domain_geometry_wkb_hex: Text
    security_union_wkb_hex: Text
    original_domain_area_m2: float = Field(gt=0.0)
    security_union_area_m2: float = Field(gt=0.0)
    security_inside_area_m2: float = Field(ge=0.0)
    security_outside_area_m2: float = Field(ge=0.0)
    active_domain_area_m2: float = Field(gt=0.0)
    active_mesh_area_m2: float = Field(gt=0.0)
    coverage_residual_m2: float = Field(ge=0.0)
    mesh_face_count: StrictInt = Field(ge=1)
    active_face_id_sha256: Text
    security_boundary_group_name: Text
    security_boundary_edge_count: StrictInt = Field(ge=0)
    security_boundary_edge_sha256: Text

    @field_validator(
        "security_input_config_sha256", "source_file_sha256", "source_shx_sha256", "source_prj_sha256",
        "source_geometry_sha256", "source_union_geometry_sha256", "domain_source_sha256",
        "domain_geometry_sha256", "topology_fingerprint", "active_face_id_sha256", "security_boundary_edge_sha256",
    )
    @classmethod
    def optional_hashes_are_sha256(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
            raise ValueError("security evidence hashes must be lowercase SHA-256 values")
        return value

    @model_validator(mode="after")
    def evidence_counts_and_areas_are_consistent(self) -> "SecurityExclusionEvidence":
        if self.source_feature_count != len(self.source_feature_indices) or len(set(self.source_feature_indices)) != len(self.source_feature_indices):
            raise ValueError("source_feature_indices must uniquely identify every source geometry record")
        area_residual = abs(self.original_domain_area_m2 - self.security_inside_area_m2 - self.active_domain_area_m2)
        tolerance = max(1e-8, self.original_domain_area_m2 * 1e-10)
        if area_residual > tolerance:
            raise ValueError("original domain area must equal excluded in-domain area plus active domain area")
        if abs(self.active_mesh_area_m2 - self.active_domain_area_m2) > max(tolerance, self.coverage_residual_m2 + tolerance):
            raise ValueError("active mesh/domain areas differ beyond the recorded coverage residual")
        for value in (self.source_union_geometry_sha256, self.domain_geometry_sha256,
                      self.active_face_id_sha256, self.security_boundary_edge_sha256):
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError("security evidence hashes must be lowercase SHA-256 values")
        return self


@dataclass(frozen=True)
class SecurityAreaGeometry:
    policy: SecurityExclusionInput
    policy_path: Path
    policy_bytes: bytes
    source_path: Path
    source_crs: Any
    target_crs: Any
    source_geometries: tuple[Any, ...]
    source_union: Any
    source_feature_indices: tuple[int, ...]
    source_file_sha256: str
    source_shx_sha256: str | None
    source_prj_sha256: str | None
    source_geometry_sha256: str
    source_union_geometry_sha256: str


@dataclass(frozen=True)
class SecurityDomainAssessment:
    domain_geometry: Any
    source_union_geometry: Any
    security_inside_geometry: Any
    security_outside_geometry: Any
    active_geometry: Any
    original_domain_area_m2: float
    security_union_area_m2: float
    security_inside_area_m2: float
    security_outside_area_m2: float
    active_domain_area_m2: float


@dataclass(frozen=True)
class SecurityMeshCheck:
    mesh: TriangularMesh
    active_mesh_area_m2: float
    coverage_residual_m2: float
    security_boundary_edge_indices: tuple[int, ...]
    security_boundary_edge_sha256: str


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_geometry_bytes(geometry: Any) -> bytes:
    try:
        from shapely import normalize, to_wkb
        return bytes(to_wkb(normalize(geometry), hex=False, byte_order=1, output_dimension=2, include_srid=False))
    except ImportError as exc:
        raise SecurityExclusionError("security geometry requires optional Shapely; install the vector extra") from exc


def _geometry_sha256(geometry: Any) -> str:
    return _sha256(_canonical_geometry_bytes(geometry))


def _mesh_topology_fingerprint(mesh: TriangularMesh) -> str:
    groups = {
        name: [list(mesh.edge_nodes[index]) for index in indices]
        for name, indices in sorted(mesh.boundary_groups.items())
    }
    constraints = {
        name: [list(mesh.edge_nodes[index]) for index in indices]
        for name, indices in sorted(mesh.constraint_groups.items())
    }
    payload = {
        "mesh_id": mesh.mesh_id, "coordinate_system": mesh.coordinate_system,
        "node_xy": mesh.node_xy, "face_nodes": mesh.face_nodes, "face_id": mesh.face_id,
        "edge_nodes": mesh.edge_nodes, "boundary_groups": groups, "constraint_groups": constraints,
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _relative_input_path(base: Path, value: str, label: str) -> Path:
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise SecurityExclusionError(f"{label} must be relative to its declaring config")
    path = (base / value).resolve()
    if not path.is_file():
        raise SecurityExclusionError(f"{label} file does not exist: {path}")
    return path


def load_security_exclusion_input(path: str | Path) -> tuple[SecurityExclusionInput, Path, bytes]:
    import yaml

    config_path = Path(path).resolve()
    try:
        raw = config_path.read_bytes()
        data = yaml.safe_load(raw.decode("utf-8"))
        policy = SecurityExclusionInput.model_validate(data)
    except FileNotFoundError as exc:
        raise SecurityExclusionError(f"security-exclusion config not found: {config_path}") from exc
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise SecurityExclusionError(f"invalid security-exclusion config {config_path}: {exc}") from exc
    if policy.enabled is not True:
        raise SecurityExclusionError("a referenced security-exclusion input must explicitly set enabled: true")
    return policy, config_path, raw


def _validate_polygon_geometry(geometry: Any, label: str) -> None:
    if geometry is None or geometry.is_empty:
        raise SecurityExclusionError(f"{label} geometry must not be empty")
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise SecurityExclusionError(f"{label} geometry must be Polygon or MultiPolygon, not {geometry.geom_type}")
    if not geometry.is_valid:
        raise SecurityExclusionError(f"{label} geometry is invalid/self-intersecting; no buffer(0) repair is applied")
    if not math.isfinite(float(geometry.area)) or geometry.area <= 0.0:
        raise SecurityExclusionError(f"{label} geometry must have finite positive area")
    for polygon in ([geometry] if geometry.geom_type == "Polygon" else geometry.geoms):
        for ring in (polygon.exterior, *polygon.interiors):
            for x, y in ring.coords:
                if not math.isfinite(float(x)) or not math.isfinite(float(y)):
                    raise SecurityExclusionError(f"{label} coordinates must be finite")


def _read_shapefile_geometries(path: Path) -> tuple[tuple[Any, ...], str, str]:
    try:
        import shapefile
        from shapely.geometry import shape
    except ImportError as exc:
        raise SecurityExclusionError("security SHP geometry requires optional pyshp and Shapely; install the workflow/shp extras") from exc
    prj_path, shx_path = path.with_suffix(".prj"), path.with_suffix(".shx")
    if not prj_path.is_file() or not shx_path.is_file():
        raise SecurityExclusionError("security Shapefile requires readable .shx and declared .prj sidecars")
    try:
        # Supply only SHP and SHX streams. DBF records are never opened or read.
        with path.open("rb") as shp_stream, shx_path.open("rb") as shx_stream:
            reader = shapefile.Reader(shp=shp_stream, shx=shx_stream)
            try:
                if reader.shapeType not in {shapefile.POLYGON, getattr(shapefile, "POLYGONM", -1), getattr(shapefile, "POLYGONZ", -1)}:
                    raise SecurityExclusionError("security Shapefile shapeType must be Polygon/MultiPolygon")
                source_shapes = reader.shapes()
            finally:
                reader.close()
        prj_text = prj_path.read_text(encoding="utf-8")
        from pyproj import CRS
        prj_crs = CRS.from_wkt(prj_text)
        geometries = tuple(shape(item.__geo_interface__) for item in source_shapes)
        if not geometries:
            raise SecurityExclusionError("security Shapefile must contain at least one geometry record")
        for index, geometry in enumerate(geometries):
            _validate_polygon_geometry(geometry, f"security SHP record {index}")
        return geometries, prj_text, _sha256(path.read_bytes())
    except SecurityExclusionError:
        raise
    except ImportError as exc:
        raise SecurityExclusionError("security SHP CRS verification requires optional pyproj; install the triangular extra") from exc
    except Exception as exc:
        raise SecurityExclusionError(f"cannot read security Shapefile geometry/PRJ without DBF attributes: {exc}") from exc


def _read_geojson_geometries(path: Path) -> tuple[tuple[Any, ...], str, str | None]:
    try:
        import json
        from shapely.geometry import shape
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (ImportError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityExclusionError(f"cannot read security GeoJSON geometry: {exc}") from exc
    if not isinstance(payload, dict):
        raise SecurityExclusionError("security GeoJSON root must be an object")
    declared_crs: str | None = None
    if "crs" in payload:
        crs_data = payload["crs"]
        if not isinstance(crs_data, dict) or not isinstance(crs_data.get("type"), str):
            raise SecurityExclusionError("security GeoJSON crs declaration must be an object with a recognized type")
        crs_type = crs_data["type"].strip().lower()
        properties = crs_data.get("properties")
        if not isinstance(properties, dict):
            raise SecurityExclusionError("security GeoJSON crs.properties must be an object")
        if crs_type == "name":
            name = properties.get("name")
            if not isinstance(name, str) or not name.strip():
                raise SecurityExclusionError("security GeoJSON crs.name must be a non-empty CRS declaration")
            declared_crs = name.strip()
        elif crs_type == "epsg":
            code = properties.get("code")
            if isinstance(code, bool) or not isinstance(code, (str, int)) or not str(code).strip():
                raise SecurityExclusionError("security GeoJSON EPSG crs.code must be a non-empty code")
            declared_crs = f"EPSG:{str(code).strip()}"
        else:
            raise SecurityExclusionError(f"security GeoJSON crs.type '{crs_data['type']}' is unsupported or unresolvable")
    geometries: list[Any] = []
    if payload.get("type") == "FeatureCollection":
        features = payload.get("features")
        if not isinstance(features, list):
            raise SecurityExclusionError("security GeoJSON FeatureCollection.features must be an array")
        for index, feature in enumerate(features):
            if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
                raise SecurityExclusionError(f"security GeoJSON feature {index} must contain a geometry")
            geometry = shape(feature["geometry"])
            _validate_polygon_geometry(geometry, f"security GeoJSON feature {index}")
            geometries.append(geometry)
    elif payload.get("type") == "Feature":
        if not isinstance(payload.get("geometry"), dict):
            raise SecurityExclusionError("security GeoJSON Feature must contain a geometry")
        geometry = shape(payload["geometry"])
        _validate_polygon_geometry(geometry, "security GeoJSON feature")
        geometries.append(geometry)
    else:
        geometry = shape(payload)
        _validate_polygon_geometry(geometry, "security GeoJSON geometry")
        geometries.append(geometry)
    if not geometries:
        raise SecurityExclusionError("security GeoJSON must contain at least one polygon geometry")
    return tuple(geometries), _sha256(raw), declared_crs


def load_security_area_geometry(policy: SecurityExclusionInput, config_path: str | Path, target_crs_text: str) -> SecurityAreaGeometry:
    try:
        from pyproj import CRS, Transformer
        from shapely.ops import unary_union, transform as transform_geometry
    except ImportError as exc:
        raise SecurityExclusionError("security-area compatibility requires optional pyproj and Shapely; install workflow/triangular extras") from exc
    config = Path(config_path).resolve()
    policy_path = config
    policy_bytes = config.read_bytes()
    source = _relative_input_path(config.parent, policy.source_file, "security source")
    source_crs = CRS.from_user_input(policy.source_crs)
    target_crs = CRS.from_user_input(target_crs_text)
    if not target_crs.is_projected or any(not math.isclose(float(axis.unit_conversion_factor), 1.0, abs_tol=1e-12, rel_tol=0.0) for axis in target_crs.axis_info[:2]):
        raise SecurityExclusionError("security exclusion target CRS must be projected in metres")
    source_hash = _sha256(source.read_bytes())
    shx_hash: str | None = None
    prj_hash: str | None = None
    if source.suffix.lower() == ".shp":
        geometries, prj_text, source_hash = _read_shapefile_geometries(source)
        prj_crs = CRS.from_wkt(prj_text)
        if not prj_crs.equals(source_crs, ignore_axis_order=True):
            raise SecurityExclusionError("security source_crs does not match the Shapefile .prj")
        shx_hash = _sha256(source.with_suffix(".shx").read_bytes())
        prj_hash = _sha256(source.with_suffix(".prj").read_bytes())
    elif source.suffix.lower() in {".json", ".geojson"}:
        geometries, source_hash, declared_source_crs = _read_geojson_geometries(source)
        if declared_source_crs is not None:
            try:
                embedded_crs = CRS.from_user_input(declared_source_crs)
            except Exception as exc:
                raise SecurityExclusionError(f"security GeoJSON crs declaration is malformed or unrecognized: {exc}") from exc
            if not embedded_crs.equals(source_crs, ignore_axis_order=True):
                raise SecurityExclusionError("security GeoJSON crs declaration conflicts with explicit source_crs")
    else:
        raise SecurityExclusionError("security source_file extension must be .shp, .json or .geojson")
    if source_crs.equals(target_crs, ignore_axis_order=True):
        projected = geometries
    else:
        transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
        projected = tuple(transform_geometry(transformer.transform, geometry) for geometry in geometries)
    for index, geometry in enumerate(projected):
        _validate_polygon_geometry(geometry, f"projected security geometry {index}")
    union = unary_union(projected)
    _validate_polygon_geometry(union, "normalized security union")
    canonical_source = b"".join(_canonical_geometry_bytes(geometry) for geometry in projected)
    return SecurityAreaGeometry(
        policy=policy, policy_path=policy_path, policy_bytes=policy_bytes, source_path=source,
        source_crs=source_crs, target_crs=target_crs, source_geometries=tuple(projected),
        source_union=union, source_feature_indices=tuple(range(len(projected))),
        source_file_sha256=source_hash, source_shx_sha256=shx_hash, source_prj_sha256=prj_hash,
        source_geometry_sha256=_sha256(canonical_source), source_union_geometry_sha256=_geometry_sha256(union),
    )


def _geometry_wkb_hex(geometry: Any) -> str:
    return _canonical_geometry_bytes(geometry).hex()


def _area_tolerance(area_m2: float, geometry_tolerance_m: float) -> float:
    return max(1e-8, min(1e-4, area_m2 * 1e-10), geometry_tolerance_m * geometry_tolerance_m * 10.0)


def assess_security_domain(domain_geometry: Any, security: SecurityAreaGeometry) -> SecurityDomainAssessment:
    try:
        from shapely.ops import unary_union
    except ImportError as exc:
        raise SecurityExclusionError("security domain difference requires optional Shapely") from exc
    _validate_polygon_geometry(domain_geometry, "original computational domain D")
    safe = security.source_union
    inside = safe.intersection(domain_geometry)
    outside = safe.difference(domain_geometry)
    active = domain_geometry.difference(safe)
    if active.is_empty or not math.isfinite(float(active.area)) or active.area <= 0.0:
        raise SecurityExclusionError("security exclusion removes the entire original domain; D\\S must have positive area")
    if not active.is_valid:
        raise SecurityExclusionError("D\\S geometry is invalid; no buffer(0) repair is applied")
    return SecurityDomainAssessment(
        domain_geometry=domain_geometry, source_union_geometry=safe,
        security_inside_geometry=inside, security_outside_geometry=outside, active_geometry=active,
        original_domain_area_m2=float(domain_geometry.area), security_union_area_m2=float(safe.area),
        security_inside_area_m2=float(inside.area), security_outside_area_m2=float(outside.area),
        active_domain_area_m2=float(active.area),
    )


def domain_from_gmsh_config(mesh_config: Any) -> Any:
    try:
        from shapely.geometry import Polygon
    except ImportError as exc:
        raise SecurityExclusionError("security geometry requires optional Shapely; install the vector extra") from exc
    if not hasattr(mesh_config, "outer_boundary") or not hasattr(mesh_config, "holes"):
        raise SecurityExclusionError("original domain D requires a Gmsh config with an explicit outer_boundary and holes")
    domain = Polygon(mesh_config.outer_boundary, holes=mesh_config.holes)
    _validate_polygon_geometry(domain, "original Gmsh domain D")
    return domain


def gmsh_config_for_active_domain(mesh_config: Any, assessment: SecurityDomainAssessment, *, tolerance_m: float) -> Any:
    r"""Replace Gmsh's generation domain with the exact D\S polygon without changing meshing physics."""
    try:
        from shapely.geometry import LineString
        from .mesh_preparation import GmshMeshConfig
    except ImportError as exc:
        raise SecurityExclusionError("Gmsh security exclusion requires the existing mesh preparation adapter") from exc
    active = assessment.active_geometry
    if active.geom_type != "Polygon":
        raise SecurityExclusionError(
            f"UNSUPPORTED_SECURITY_DOMAIN_MULTICOMPONENT: existing Gmsh adapter accepts one connected surface, D\\S produced {active.geom_type}"
        )
    active_buffer = active.boundary.buffer(tolerance_m)
    for group in mesh_config.boundary_groups:
        coordinates = [(float(point[0]), float(point[1])) for point in group.points]
        if group.closed and coordinates[0] == coordinates[-1]:
            coordinates.pop()
        if len(coordinates) < 2:
            raise SecurityExclusionError(f"UNSUPPORTED_SECURITY_BOUNDARY_GROUP: group {group.name!r} has fewer than two retained points")
        line = LineString(coordinates)
        if line.difference(active_buffer).length > tolerance_m:
            raise SecurityExclusionError(
                f"UNSUPPORTED_SECURITY_BOUNDARY_GROUP: configured boundary group {group.name!r} is cut by D\\S and is not silently shortened"
            )
    for group in mesh_config.constraints:
        line = LineString([(float(point[0]), float(point[1])) for point in group.points])
        if line.difference(active.buffer(tolerance_m)).length > tolerance_m:
            raise SecurityExclusionError(
                f"UNSUPPORTED_CONSTRAINT_CROSSES_EXCLUSION: constraint group {group.name!r} intersects the excluded security area"
            )
    values = mesh_config.model_dump(mode="python")
    values["outer_boundary"] = [tuple(map(float, point)) for point in list(active.exterior.coords)[:-1]]
    values["holes"] = [[tuple(map(float, point)) for point in list(ring.coords)[:-1]] for ring in active.interiors]
    try:
        return GmshMeshConfig.model_validate(values)
    except Exception as exc:
        raise SecurityExclusionError(f"UNSUPPORTED_SECURITY_GMSH_DOMAIN: D\\S cannot be expressed by the current polygon-with-holes mesher: {exc}") from exc


def load_domain_geojson(path: str | Path, source_crs: str, target_crs: Any) -> tuple[Any, Path, str]:
    """Load an explicit original-domain Polygon/MultiPolygon GeoJSON and project only its geometry."""
    import json
    try:
        from pyproj import CRS, Transformer
        from shapely.geometry import shape
        from shapely.ops import transform as transform_geometry
    except ImportError as exc:
        raise SecurityExclusionError("security-domain GeoJSON requires optional pyproj/Shapely") from exc
    domain_path = Path(path).resolve()
    raw = domain_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityExclusionError(f"original domain GeoJSON is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != "security-domain-geometry-v1" or payload.get("schema_version") != "1.0":
        raise SecurityExclusionError("domain GeoJSON must use security-domain-geometry-v1 schema_version 1.0")
    if set(payload) != {"format", "schema_version", "coordinate_system", "geometry"}:
        raise SecurityExclusionError("domain GeoJSON keys must be exactly format/schema_version/coordinate_system/geometry")
    declared = CRS.from_user_input(payload["coordinate_system"])
    source = CRS.from_user_input(source_crs)
    if not declared.equals(source, ignore_axis_order=True):
        raise SecurityExclusionError("domain_geometry_crs conflicts with the GeoJSON coordinate_system declaration")
    domain = shape(payload["geometry"])
    _validate_polygon_geometry(domain, "original domain D")
    if source.equals(target_crs, ignore_axis_order=True):
        projected = domain
    else:
        transformer = Transformer.from_crs(source, target_crs, always_xy=True)
        projected = transform_geometry(transformer.transform, domain)
    _validate_polygon_geometry(projected, "projected original domain D")
    return projected, domain_path, _sha256(raw)


def _line_parts(geometry: Any) -> list[Any]:
    if geometry.is_empty:
        return []
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [geometry]
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        return [line for item in geometry.geoms for line in _line_parts(item)]
    return []


def check_mesh_against_security(
    mesh: TriangularMesh, assessment: SecurityDomainAssessment, *, tolerance_m: float,
) -> SecurityMeshCheck:
    try:
        from shapely.geometry import LineString, Polygon
        from shapely.ops import unary_union
        from shapely.strtree import STRtree
    except ImportError as exc:
        raise SecurityExclusionError("security mesh validation requires optional Shapely") from exc
    if not mesh.coordinate_system.strip():
        raise SecurityExclusionError("mesh coordinate_system is required for security-domain validation")
    domain_area = assessment.active_domain_area_m2
    bounds = assessment.active_geometry.bounds
    coordinate_scale = max(
        1.0,
        *(abs(float(value)) for value in bounds),
        *(abs(float(value)) for point in mesh.node_xy for value in point),
    )
    coordinate_resolution_m = math.ulp(coordinate_scale)
    effective_tolerance_m = max(float(tolerance_m), coordinate_resolution_m * 2.0)
    faces = [Polygon([mesh.node_xy[node] for node in face]) for face in mesh.face_nodes]
    for index, face in enumerate(faces):
        if not face.is_valid or face.is_empty or face.area <= 0.0:
            raise SecurityExclusionError(f"mesh face_id {mesh.face_id[index]} has invalid active-domain geometry")
        intersection_area = float(face.intersection(assessment.security_inside_geometry).area)
        face_overlap_tolerance = max(effective_tolerance_m * float(face.length), effective_tolerance_m**2 * 10.0)
        if intersection_area > face_overlap_tolerance:
            raise SecurityExclusionError(
                f"mesh face_id {mesh.face_id[index]} overlaps the excluded security area by {intersection_area:.12g} m2"
            )

    # A domain-area-scaled tolerance can hide a small duplicate/overlapping face
    # in a very large domain. Query only spatially adjacent face pairs and judge
    # each overlap against that pair's local geometric precision.
    face_tree = STRtree(faces)
    for left_index, left_face in enumerate(faces):
        for right_index_raw in face_tree.query(left_face):
            right_index = int(right_index_raw)
            if right_index <= left_index:
                continue
            right_face = faces[right_index]
            pair_overlap = float(left_face.intersection(right_face).area)
            pair_tolerance = max(
                effective_tolerance_m**2 * 10.0,
                effective_tolerance_m * min(float(left_face.length), float(right_face.length)),
            )
            if pair_overlap > pair_tolerance:
                raise SecurityExclusionError(
                    f"mesh faces face_id {mesh.face_id[left_index]} and {mesh.face_id[right_index]} "
                    f"overlap by {pair_overlap:.12g} m2"
                )

    face_area_sum = math.fsum(float(value) for value in mesh.face_area)
    face_union = unary_union(faces)
    overlap_residual = abs(face_area_sum - float(face_union.area))
    active_boundary_length = math.fsum(float(line.length) for line in _line_parts(assessment.active_geometry.boundary))
    area_tolerance = max(
        effective_tolerance_m**2 * max(10.0, float(mesh.n_face)),
        2.0 * effective_tolerance_m * active_boundary_length,
    )
    if overlap_residual > area_tolerance:
        raise SecurityExclusionError(
            f"mesh faces overlap or duplicate active area by {overlap_residual:.12g} m2 "
            f"(local boundary tolerance {area_tolerance:.12g} m2)"
        )
    coverage_residual = float(face_union.symmetric_difference(assessment.active_geometry).area)
    boundary_rounding_band = assessment.active_geometry.boundary.buffer(effective_tolerance_m)
    uncovered = assessment.active_geometry.difference(face_union)
    extra = face_union.difference(assessment.active_geometry)
    uncovered_beyond_boundary = uncovered.difference(boundary_rounding_band)
    extra_beyond_boundary = extra.difference(boundary_rounding_band)
    if (not math.isfinite(coverage_residual)
            or not uncovered_beyond_boundary.is_empty
            or not extra_beyond_boundary.is_empty
            or coverage_residual > area_tolerance):
        raise SecurityExclusionError(
            f"mesh does not exactly cover D\\S; symmetric-difference area is {coverage_residual:.12g} m2 "
            f"(boundary-rounding tolerance {area_tolerance:.12g} m2; residual must lie within "
            f"{effective_tolerance_m:.12g} m of the D\\S boundary)"
        )
    mesh_area = math.fsum(float(value) for value in mesh.face_area)
    if abs(mesh_area - assessment.active_domain_area_m2) > area_tolerance:
        raise SecurityExclusionError(
            f"active mesh area {mesh_area:.12g} m2 differs from D\\S area {assessment.active_domain_area_m2:.12g} m2"
        )

    expected_security_boundary = assessment.active_geometry.boundary.intersection(assessment.security_inside_geometry.boundary)
    edge_indices: list[int] = []
    edge_lines: list[Any] = []
    for edge_index, (left_face, right_face) in enumerate(mesh.edge_faces):
        if right_face != -1:
            continue
        node_a, node_b = mesh.edge_nodes[edge_index]
        edge = LineString((mesh.node_xy[node_a], mesh.node_xy[node_b]))
        if edge.length <= effective_tolerance_m:
            continue
        if edge.difference(expected_security_boundary.buffer(effective_tolerance_m)).length <= effective_tolerance_m:
            if edge.intersection(expected_security_boundary.buffer(effective_tolerance_m)).length >= edge.length - effective_tolerance_m:
                edge_indices.append(edge_index)
                edge_lines.append(edge)
    expected_length = math.fsum(float(line.length) for line in _line_parts(expected_security_boundary))
    observed = unary_union(edge_lines) if edge_lines else LineString()
    boundary_residual = float(observed.symmetric_difference(expected_security_boundary).length) if not observed.is_empty else expected_length
    length_tolerance = max(effective_tolerance_m * 10.0, expected_length * 1e-9)
    if boundary_residual > length_tolerance:
        raise SecurityExclusionError(
            f"mesh does not represent the excluded closed-wall boundary exactly; line residual is {boundary_residual:.12g} m"
        )
    edge_node_pairs = sorted(
        tuple(sorted(tuple(float(value) for value in mesh.node_xy[node]) for node in mesh.edge_nodes[index]))
        for index in edge_indices
    )
    edge_digest = _sha256(json.dumps(edge_node_pairs, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    group_name = "security_exclusion_boundary"
    boundary_groups = {
        name: [mesh.edge_nodes[edge_index] for edge_index in edge_ids]
        for name, edge_ids in mesh.boundary_groups.items()
    }
    if group_name in boundary_groups:
        current_pairs = {tuple(sorted(tuple(pair))) for pair in boundary_groups[group_name]}
        expected_pairs = {tuple(sorted(mesh.edge_nodes[index])) for index in edge_indices}
        if current_pairs != expected_pairs:
            raise SecurityExclusionError("imported security_exclusion_boundary group does not match the actual D\\S wall edges")
    elif edge_indices:
        boundary_groups[group_name] = [mesh.edge_nodes[index] for index in edge_indices]
    constraints = {
        name: [mesh.edge_nodes[edge_index] for edge_index in edge_ids]
        for name, edge_ids in mesh.constraint_groups.items()
    }
    try:
        secured_mesh = TriangularMesh(
            node_xy=mesh.node_xy, face_nodes=mesh.face_nodes, coordinate_system=mesh.coordinate_system,
            mesh_id=mesh.mesh_id, face_id=mesh.face_id, boundary_groups=boundary_groups,
            constraint_groups=constraints, geometry_tolerance_m=mesh.geometry_tolerance_m,
            minimum_face_area_m2=mesh.minimum_face_area_m2,
        )
    except TriangularMeshError as exc:
        raise SecurityExclusionError(f"security boundary group cannot be represented in the P2 mesh: {exc}") from exc
    return SecurityMeshCheck(
        mesh=secured_mesh, active_mesh_area_m2=mesh_area, coverage_residual_m2=max(coverage_residual, overlap_residual),
        security_boundary_edge_indices=tuple(edge_indices), security_boundary_edge_sha256=edge_digest,
    )


def _geometry_from_wkb(hex_text: str, label: str) -> Any:
    try:
        from shapely import wkb
        geometry = wkb.loads(bytes.fromhex(hex_text))
    except Exception as exc:
        raise SecurityExclusionError(f"{label} WKB evidence is invalid: {exc}") from exc
    _validate_polygon_geometry(geometry, label)
    return geometry


def security_evidence_sha256(evidence: SecurityExclusionEvidence) -> str:
    raw = json.dumps(evidence.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _sha256(raw)


def make_security_exclusion_evidence(
    security: SecurityAreaGeometry,
    assessment: SecurityDomainAssessment,
    mesh_check: SecurityMeshCheck,
    *,
    mesh_prep_config_path: str | Path,
    domain_source_kind: Literal["P2_GMSH_CONFIG", "DOMAIN_GEOJSON"],
    domain_source_path: str | Path,
    domain_source_sha256: str,
    domain_source_crs: str,
) -> SecurityExclusionEvidence:
    mesh = mesh_check.mesh
    active_face_ids = [str(value) for value in mesh.face_id]
    raw_config = security.policy_path.read_bytes()
    edge_pairs = sorted(
        tuple(sorted(tuple(float(value) for value in mesh.node_xy[node]) for node in mesh.edge_nodes[index]))
        for index in mesh_check.security_boundary_edge_indices
    )
    return SecurityExclusionEvidence(
        format="security-exclusion-evidence-v1", schema_version="1.0", enabled=True,
        constraint="excluded_closed_wall", mesh_id=mesh.mesh_id or "", mesh_crs=mesh.coordinate_system,
        topology_fingerprint=_mesh_topology_fingerprint(mesh),
        security_input_config_path=str(security.policy_path), security_input_config_sha256=_sha256(security.policy_bytes),
        source_file_path=str(security.source_path), source_file_sha256=security.source_file_sha256,
        source_shx_sha256=security.source_shx_sha256, source_prj_sha256=security.source_prj_sha256,
        source_crs=security.source_crs.to_string(), source_feature_count=len(security.source_feature_indices),
        source_feature_indices=list(security.source_feature_indices), source_geometry_sha256=security.source_geometry_sha256,
        source_union_geometry_sha256=security.source_union_geometry_sha256,
        domain_source_kind=domain_source_kind, domain_source_path=str(Path(domain_source_path).resolve()),
        domain_source_sha256=domain_source_sha256, domain_source_crs=domain_source_crs,
        domain_geometry_sha256=_geometry_sha256(assessment.domain_geometry),
        domain_geometry_wkb_hex=_geometry_wkb_hex(assessment.domain_geometry),
        security_union_wkb_hex=_geometry_wkb_hex(security.source_union),
        original_domain_area_m2=assessment.original_domain_area_m2,
        security_union_area_m2=assessment.security_union_area_m2,
        security_inside_area_m2=assessment.security_inside_area_m2,
        security_outside_area_m2=assessment.security_outside_area_m2,
        active_domain_area_m2=assessment.active_domain_area_m2,
        active_mesh_area_m2=mesh_check.active_mesh_area_m2,
        coverage_residual_m2=mesh_check.coverage_residual_m2,
        mesh_face_count=mesh.n_face, active_face_id_sha256=_sha256(json.dumps(active_face_ids, separators=(",", ":")).encode("utf-8")),
        security_boundary_group_name="security_exclusion_boundary",
        security_boundary_edge_count=len(mesh_check.security_boundary_edge_indices),
        security_boundary_edge_sha256=mesh_check.security_boundary_edge_sha256,
    )


def read_security_exclusion_evidence(path: str | Path) -> SecurityExclusionEvidence | None:
    """Read the optional P2 UGRID security evidence attribute without reading DEM pixels."""
    try:
        import netCDF4
        from .netcdf_path_io import open_netcdf_dataset
    except ImportError as exc:
        raise SecurityExclusionError("reading P2 security evidence requires netCDF4") from exc
    source = Path(path)
    try:
        with open_netcdf_dataset(netCDF4, source, "r") as dataset:
            raw = getattr(dataset, "security_exclusion_json", None)
            digest = getattr(dataset, "security_exclusion_sha256", None)
    except OSError as exc:
        raise SecurityExclusionError(f"cannot read P2 security evidence {source}: {exc}") from exc
    if raw is None and digest is None:
        return None
    if not isinstance(raw, str) or not isinstance(digest, str):
        raise SecurityExclusionError("P2 security evidence JSON and SHA-256 attributes must appear together")
    try:
        evidence = SecurityExclusionEvidence.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SecurityExclusionError(f"P2 security_exclusion_json is invalid: {exc}") from exc
    if security_evidence_sha256(evidence) != digest:
        raise SecurityExclusionError("P2 security evidence SHA-256 does not match its metadata")
    return evidence


def domain_from_geojson_file(path: str | Path, declared_crs: str, target_crs: Any) -> tuple[Any, Path, str]:
    return load_domain_geojson(path, declared_crs, target_crs)


def build_security_evidence(
    security: SecurityAreaGeometry,
    assessment: SecurityDomainAssessment,
    mesh_check: SecurityMeshCheck,
    *,
    domain_source_kind: Literal["P2_GMSH_CONFIG", "DOMAIN_GEOJSON"],
    domain_source_path: str | Path,
    domain_source_sha256: str,
    domain_source_crs: str,
) -> SecurityExclusionEvidence:
    mesh = mesh_check.mesh
    active_face_ids = [str(value) for value in mesh.face_id]
    edge_pairs = sorted(
        tuple(sorted(tuple(float(value) for value in mesh.node_xy[node]) for node in mesh.edge_nodes[index]))
        for index in mesh_check.security_boundary_edge_indices
    )
    return SecurityExclusionEvidence(
        format="security-exclusion-evidence-v1", schema_version="1.0", enabled=True,
        constraint="excluded_closed_wall", mesh_id=mesh.mesh_id or "", mesh_crs=mesh.coordinate_system,
        topology_fingerprint=_mesh_topology_fingerprint(mesh),
        security_input_config_path=str(security.policy_path), security_input_config_sha256=_sha256(security.policy_bytes),
        source_file_path=str(security.source_path), source_file_sha256=security.source_file_sha256,
        source_shx_sha256=security.source_shx_sha256, source_prj_sha256=security.source_prj_sha256,
        source_crs=security.source_crs.to_string(), source_feature_count=len(security.source_feature_indices),
        source_feature_indices=list(security.source_feature_indices), source_geometry_sha256=security.source_geometry_sha256,
        source_union_geometry_sha256=security.source_union_geometry_sha256,
        domain_source_kind=domain_source_kind, domain_source_path=str(Path(domain_source_path).resolve()),
        domain_source_sha256=domain_source_sha256, domain_source_crs=domain_source_crs,
        domain_geometry_sha256=_geometry_sha256(assessment.domain_geometry),
        domain_geometry_wkb_hex=_geometry_wkb_hex(assessment.domain_geometry),
        security_union_wkb_hex=_geometry_wkb_hex(security.source_union),
        original_domain_area_m2=assessment.original_domain_area_m2,
        security_union_area_m2=assessment.security_union_area_m2,
        security_inside_area_m2=assessment.security_inside_area_m2,
        security_outside_area_m2=assessment.security_outside_area_m2,
        active_domain_area_m2=assessment.active_domain_area_m2,
        active_mesh_area_m2=mesh_check.active_mesh_area_m2,
        coverage_residual_m2=mesh_check.coverage_residual_m2,
        mesh_face_count=mesh.n_face,
        active_face_id_sha256=_sha256(json.dumps(active_face_ids, separators=(",", ":")).encode("utf-8")),
        security_boundary_group_name="security_exclusion_boundary",
        security_boundary_edge_count=len(mesh_check.security_boundary_edge_indices),
        security_boundary_edge_sha256=mesh_check.security_boundary_edge_sha256,
    )


def revalidate_security_exclusion_evidence(evidence: SecurityExclusionEvidence, mesh: TriangularMesh) -> SecurityMeshCheck:
    """Re-read the input SHP/GeoJSON and D source; a changed security source invalidates P2/P3/P4 reuse."""
    config_path = Path(evidence.security_input_config_path)
    try:
        config_bytes = config_path.read_bytes()
    except OSError as exc:
        raise SecurityExclusionError(f"security-exclusion config source is unavailable: {config_path}: {exc}") from exc
    if _sha256(config_bytes) != evidence.security_input_config_sha256:
        raise SecurityExclusionError("security-exclusion config SHA-256 changed since P2 preparation")
    # Validate the policy identity from the original config, including its source-file value.
    import yaml
    try:
        config_data = yaml.safe_load(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SecurityExclusionError(f"security-exclusion config is no longer valid YAML: {exc}") from exc
    policy_data = config_data.get("security_exclusion") if isinstance(config_data, dict) else None
    if policy_data is None:
        raise SecurityExclusionError("the recorded P2 config no longer declares security_exclusion")
    policy = SecurityExclusionInput.model_validate(policy_data)
    from pyproj import CRS
    try:
        source_crs_matches = CRS.from_user_input(policy.source_crs).equals(
            CRS.from_user_input(evidence.source_crs), ignore_axis_order=True,
        )
    except Exception as exc:
        raise SecurityExclusionError(f"security source CRS cannot be revalidated: {exc}") from exc
    if not policy.enabled or policy.constraint != evidence.constraint or not source_crs_matches:
        raise SecurityExclusionError("security-exclusion config enabled/source CRS/constraint differs from P2 evidence")
    security = load_security_area_geometry(policy, config_path, evidence.mesh_crs)
    if (security.source_path.resolve() != Path(evidence.source_file_path).resolve()
            or security.source_file_sha256 != evidence.source_file_sha256
            or security.source_shx_sha256 != evidence.source_shx_sha256
            or security.source_prj_sha256 != evidence.source_prj_sha256
            or security.source_union_geometry_sha256 != evidence.source_union_geometry_sha256
            or security.source_geometry_sha256 != evidence.source_geometry_sha256):
        raise SecurityExclusionError("security source geometry/CRS/file digest changed since P2 preparation")

    domain_path = Path(evidence.domain_source_path)
    try:
        domain_bytes = domain_path.read_bytes()
    except OSError as exc:
        raise SecurityExclusionError(f"original-domain source D is unavailable: {domain_path}: {exc}") from exc
    if _sha256(domain_bytes) != evidence.domain_source_sha256:
        raise SecurityExclusionError("original-domain source D changed since P2 preparation")
    if evidence.domain_source_kind == "P2_GMSH_CONFIG":
        try:
            raw = yaml.safe_load(domain_bytes.decode("utf-8"))
            mesh_cfg = raw["mesh"]
            from shapely.geometry import Polygon
            domain = Polygon(mesh_cfg["outer_boundary"], holes=mesh_cfg.get("holes", []))
        except (UnicodeDecodeError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
            raise SecurityExclusionError(f"cannot re-read original Gmsh domain D: {exc}") from exc
    else:
        domain, resolved_domain_path, digest = load_domain_geojson(domain_path, evidence.domain_source_crs, security.target_crs)
        if resolved_domain_path.resolve() != domain_path.resolve() or digest != evidence.domain_source_sha256:
            raise SecurityExclusionError("original-domain GeoJSON path or digest differs from P2 evidence")
    _validate_polygon_geometry(domain, "original computational domain D")
    if _geometry_sha256(domain) != evidence.domain_geometry_sha256 or _geometry_wkb_hex(domain) != evidence.domain_geometry_wkb_hex:
        raise SecurityExclusionError("original-domain geometry differs from P2 security evidence")
    assessment = assess_security_domain(domain, security)
    if any(not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-8) for actual, expected in (
        (assessment.original_domain_area_m2, evidence.original_domain_area_m2),
        (assessment.security_union_area_m2, evidence.security_union_area_m2),
        (assessment.security_inside_area_m2, evidence.security_inside_area_m2),
        (assessment.security_outside_area_m2, evidence.security_outside_area_m2),
        (assessment.active_domain_area_m2, evidence.active_domain_area_m2),
    )):
        raise SecurityExclusionError("security/domain area accounting differs from P2 evidence")
    checked = check_mesh_against_security(mesh, assessment, tolerance_m=mesh.geometry_tolerance_m)
    if checked.mesh.boundary_groups != mesh.boundary_groups:
        raise SecurityExclusionError("P2 mesh security_exclusion_boundary group is missing or inconsistent")
    if _mesh_topology_fingerprint(checked.mesh) != evidence.topology_fingerprint:
        raise SecurityExclusionError("P2 mesh topology fingerprint differs from security evidence")
    if (checked.mesh.n_face != evidence.mesh_face_count
            or not math.isclose(checked.active_mesh_area_m2, evidence.active_mesh_area_m2, rel_tol=1e-10, abs_tol=1e-8)):
        raise SecurityExclusionError("P2 active face count/area differs from security evidence")
    face_ids = [str(value) for value in mesh.face_id]
    if _sha256(json.dumps(face_ids, separators=(",", ":")).encode("utf-8")) != evidence.active_face_id_sha256:
        raise SecurityExclusionError("P2 active face_id set differs from security evidence")
    if (len(checked.security_boundary_edge_indices) != evidence.security_boundary_edge_count
            or checked.security_boundary_edge_sha256 != evidence.security_boundary_edge_sha256):
        raise SecurityExclusionError("P2 security closed-wall boundary edge set differs from evidence")
    return checked
