"""Socio-economic exposure overlay for large-run result bundles (opt-in, post-processing only).

Input: a 50 m socio-economic grid-cell polygon shapefile (``grid_cells_socio.shp``:
``s_pop`` persons per cell, ``s_ar_mu`` arable land in mu per cell, ``qu_dm``/``qu``
district code/name) and the native triangular mesh + saved frames of
``result_native.nc``.

Method (see ``docs/socio_exposure_overlay.md``):

* cell polygons are reprojected vertex-by-vertex from the shapefile ``.prj`` CRS to
  the run CRS (edges straight in the run CRS);
* every native triangle is intersected exactly with every grid cell it touches
  (shapely 2 vectorised intersection, STRtree candidates);
* a face receives ``value_c * area(face ∩ cell) / area(cell)`` from each cell, so a
  cell that lies partly outside the domain contributes only its inside fraction;
* faces not touched by any cell get NaN (unavailable) -- never 0 and never filled;
* conservation: sum over faces == sum over cells of value * area(cell ∩ domain)/area(cell),
  where area(cell ∩ domain) is computed independently from the mesh outline;
* exposure: a face is exposed when its depth >= the threshold (default 0.15 m); its whole
  mapped population/arable value counts. Depth classes are [e_k, e_{k+1}) and [e_last, inf).

The face mapping is cached as an npz keyed by the mesh fingerprint and the source digest.
This module never runs or modifies a solver and does no NoData filling or resampling of
hydrodynamic results. Population/arable values are exposure counts only (no asset values,
no damage functions, no economic loss).
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import sys
import time as _time
from typing import Any, Callable, Iterable

import numpy as np

SOCIO_FORMAT = "socio-exposure-overlay-v1"
MAPPING_FORMAT = "socio-face-mapping-v1"
MU_PER_KM2 = 1500.0  # 1 mu = 10000/15 m2
SOCIO_DIR = "socio_exposure"
MAPPING_FILE = "face_socio_mapping.npz"
FRAME_CSV = "exposure_by_frame.csv"
SUMMARY_JSON = "exposure_summary.json"
MAX_SURFACE_ROLES = ("maximum_wet_surface", "maximum_wet_surface_4490")

LIMITATIONS = (
    "Population is the 2020 seventh national census (七普) district resident total disaggregated to the 50 m grid by the "
    "data provider; within each district the per-cell values are uniform (area-proportional), so the overlay carries no "
    "sub-district settlement pattern.",
    "Arable land is from the third national land survey (三调) district total disaggregated to the 50 m grid in the same "
    "uniform way; it is not a parcel-level land-use map.",
    "Exposure only: no asset values, depth-damage functions or vulnerability are used, so no economic loss is computed.",
    "A face counts as exposed when its face-mean depth >= the threshold; the whole mapped value of the face is counted "
    "(no sub-face depth distribution).",
    "Frame exposure uses saved frames only; the envelope uses the saved-frame maximum depth (peaks between saved frames "
    "are not seen).",
    "Faces not covered by the grid have unavailable (NaN) population/arable values; their exposed area is reported "
    "separately and is not counted as zero exposure of a populated area.",
    "Cell polygons are reprojected vertex by vertex (WGS84 -> run CRS); straight edges in the run CRS are assumed "
    "(sub-centimetre effect for 50 m cells).",
    "Results depend on the hydrodynamic run label (forcing notice) of the bundle; non-production runs are not "
    "authoritative flood maps.",
)


class SocioExposureError(ValueError):
    """Socio-economic overlay input or consistency failure."""


# ----------------------------------------------------------------- options
@dataclass(frozen=True)
class SocioOptions:
    """Resolved options (validated by ``large_run_profile.SocioExposureOptions`` or the CLI)."""

    grid_cells_path: Path
    population_field: str = "s_pop"
    arable_mu_field: str = "s_ar_mu"
    district_code_field: str | None = "qu_dm"
    district_name_field: str | None = "qu"
    district_totals_csv: Path | None = None
    district_grid_summary_csv: Path | None = None
    exposure_depth_threshold_m: float = 0.15
    depth_class_edges_m: tuple[float, ...] = (0.15, 0.5, 1.5, 3.0)
    mapping_cache_dir: Path | None = None
    conservation_rel_tol: float = 1e-6

    def __post_init__(self) -> None:
        thr = float(self.exposure_depth_threshold_m)
        if not math.isfinite(thr) or thr <= 0 or thr > 100:
            raise SocioExposureError("exposure_depth_threshold_m must be in (0, 100] m")
        edges = tuple(float(v) for v in self.depth_class_edges_m)
        if not edges or len(edges) > 10 or any(not math.isfinite(v) or v < 0 for v in edges) \
                or any(b <= a for a, b in zip(edges, edges[1:])):
            raise SocioExposureError("depth_class_edges_m must be 1..10 finite, non-negative, strictly increasing values (m)")
        if not (0 < float(self.conservation_rel_tol) <= 1e-2):
            raise SocioExposureError("conservation_rel_tol must be in (0, 0.01]")

    def mapping_key(self) -> dict[str, Any]:
        return {"population_field": self.population_field, "arable_mu_field": self.arable_mu_field,
                "district_code_field": self.district_code_field, "district_name_field": self.district_name_field}

    @classmethod
    def from_profile(cls, value: Any) -> "SocioOptions":
        """Build from a ``SocioExposureOptions`` pydantic model or a plain mapping."""
        data = value.model_dump() if hasattr(value, "model_dump") else dict(value)
        def p(key: str) -> Path | None:
            return None if data.get(key) is None else Path(data[key])
        return cls(grid_cells_path=Path(data["grid_cells_path"]),
                   population_field=data.get("population_field", "s_pop"),
                   arable_mu_field=data.get("arable_mu_field", "s_ar_mu"),
                   district_code_field=data.get("district_code_field", "qu_dm"),
                   district_name_field=data.get("district_name_field", "qu"),
                   district_totals_csv=p("district_totals_csv"), district_grid_summary_csv=p("district_grid_summary_csv"),
                   exposure_depth_threshold_m=float(data.get("exposure_depth_threshold_m", 0.15)),
                   depth_class_edges_m=tuple(data.get("depth_class_edges_m", (0.15, 0.5, 1.5, 3.0))),
                   mapping_cache_dir=p("mapping_cache_dir"),
                   conservation_rel_tol=float(data.get("conservation_rel_tol", 1e-6)))


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default) + "\n").encode("utf-8")


def _finite(value: float) -> float | None:
    return float(value) if math.isfinite(float(value)) else None


# ------------------------------------------------------------- source grid
@dataclass
class SocioGrid:
    cells: np.ndarray            # shapely polygons in the run CRS
    cell_area_m2: np.ndarray
    pop: np.ndarray              # persons per cell
    arable_mu: np.ndarray        # mu per cell
    district: np.ndarray         # int index into district_codes, -1 = none
    district_codes: list[str]
    district_names: list[str]
    source_crs: str
    target_crs: str
    inputs: dict[str, Any]
    source_digest: str
    grid_sums_by_district: dict[str, dict[str, float]] = field(default_factory=dict)


def _source_files(path: Path) -> dict[str, Path]:
    files = {}
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        candidate = path.with_suffix(ext)
        if candidate.is_file():
            files[ext] = candidate
    for ext in (".shp", ".shx", ".dbf", ".prj"):
        if ext not in files:
            raise SocioExposureError(f"socio grid shapefile component missing: {path.with_suffix(ext)}")
    return files


def read_socio_grid(path: str | Path, target_crs: str, options: SocioOptions) -> SocioGrid:
    """Read the grid-cell polygons and attributes and reproject them to ``target_crs``."""
    try:
        import shapefile
        import shapely
        from pyproj import CRS, Transformer
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SocioExposureError(f"socio overlay requires pyshp, shapely>=2 and pyproj: {exc}") from exc
    path = Path(path)
    if path.suffix.lower() != ".shp":
        path = path.with_suffix(".shp")
    if not path.is_file():
        raise SocioExposureError(f"socio grid shapefile not found: {path}")
    files = _source_files(path)
    source = CRS.from_wkt(files[".prj"].read_text(encoding="utf-8", errors="replace"))
    target = CRS.from_user_input(target_crs)
    if not target.is_projected:
        raise SocioExposureError(f"run CRS {target_crs} is not a projected metric CRS")
    encoding = files[".cpg"].read_text(encoding="ascii", errors="replace").strip() if ".cpg" in files else "utf-8"
    with shapefile.Reader(str(path), encoding=encoding or "utf-8") as reader:
        if reader.shapeType not in (shapefile.POLYGON, shapefile.POLYGONZ, shapefile.POLYGONM):
            raise SocioExposureError(f"socio grid must be a polygon shapefile, got shapeType {reader.shapeType}")
        names = [f[0] for f in reader.fields[1:]]
        required = [options.population_field, options.arable_mu_field]
        required += [n for n in (options.district_code_field, options.district_name_field) if n]
        missing = [n for n in required if n not in names]
        if missing:
            raise SocioExposureError(f"socio grid {path.name} lacks field(s) {missing}; available: {names}")
        shapes = reader.shapes()
        records = reader.records()
    n = len(shapes)
    if n == 0:
        raise SocioExposureError("socio grid has no features")
    idx = {name: k for k, name in enumerate(names)}

    def numeric(name: str) -> np.ndarray:
        values = np.empty(n, dtype=np.float64); bad = []
        for k, rec in enumerate(records):
            v = rec[idx[name]]
            try:
                values[k] = float(v)
            except (TypeError, ValueError):
                values[k] = np.nan
            if not math.isfinite(values[k]) or values[k] < 0:
                bad.append(k)
        if bad:
            raise SocioExposureError(f"field {name}: {len(bad)} record(s) are missing, non-numeric or negative "
                                     f"(first record index {bad[0]}); no fill is applied")
        return values

    pop = numeric(options.population_field)
    arable = numeric(options.arable_mu_field)
    codes: list[str] = []; dnames: list[str] = []; district = np.full(n, -1, dtype=np.int64)
    if options.district_code_field:
        lookup: dict[str, int] = {}
        for k, rec in enumerate(records):
            code = str(rec[idx[options.district_code_field]] or "").strip()
            if not code:
                continue
            if code not in lookup:
                lookup[code] = len(codes); codes.append(code)
                dnames.append(str(rec[idx[options.district_name_field]] or "").strip() if options.district_name_field else "")
            district[k] = lookup[code]
    # geometry
    transformer = Transformer.from_crs(source, target, always_xy=True)
    simple = all(len(s.parts) == 1 and len(s.points) == 5 for s in shapes)
    if simple:
        pts = np.asarray([s.points for s in shapes], dtype=np.float64)[:, :, :2]
        x, y = transformer.transform(pts[..., 0].ravel(), pts[..., 1].ravel())
        coords = np.stack([np.asarray(x).reshape(n, 5), np.asarray(y).reshape(n, 5)], axis=-1)
        cells = shapely.polygons(coords)
    else:
        cells = np.empty(n, dtype=object)
        for k, s in enumerate(shapes):
            starts = list(s.parts) + [len(s.points)]
            rings = []
            for a, b in zip(starts, starts[1:]):
                ring = np.asarray(s.points[a:b], dtype=np.float64)[:, :2]
                rx, ry = transformer.transform(ring[:, 0], ring[:, 1])
                rings.append(np.column_stack([rx, ry]))
            shells = []; holes = []
            for ring in rings:  # ESRI: clockwise = outer ring
                signed = 0.5 * np.sum(ring[:-1, 0] * ring[1:, 1] - ring[1:, 0] * ring[:-1, 1])
                (shells if signed < 0 else holes).append(ring)
            if len(shells) == 1:
                cells[k] = shapely.Polygon(shells[0], holes)
            else:
                cells[k] = shapely.MultiPolygon([shapely.Polygon(r) for r in shells]).difference(
                    shapely.MultiPolygon([shapely.Polygon(r) for r in holes]) if holes else shapely.GeometryCollection())
    valid = shapely.is_valid(cells)
    if not valid.all():
        raise SocioExposureError(f"{int((~valid).sum())} socio grid polygon(s) are invalid after reprojection")
    area = shapely.area(cells)
    if not (area > 0).all():
        raise SocioExposureError(f"{int((area <= 0).sum())} socio grid polygon(s) have zero area")
    inputs: dict[str, Any] = {"grid_cells": {ext.lstrip("."): {"path": str(p.resolve()), "sha256": _sha_file(p), "bytes": p.stat().st_size}
                                             for ext, p in files.items()},
                              "feature_count": n, "fields": names}
    digest = hashlib.sha256()
    for ext in sorted(files):
        digest.update(ext.encode()); digest.update(inputs["grid_cells"][ext.lstrip(".")]["sha256"].encode())
    digest.update(json.dumps(options.mapping_key(), sort_keys=True).encode())
    digest.update(target.to_wkt().encode())
    sums = {}
    for d, code in enumerate(codes):
        sel = district == d
        sums[code] = {"name": dnames[d], "cell_count": int(sel.sum()), "pop": float(math.fsum(pop[sel].tolist())),
                      "arable_mu": float(math.fsum(arable[sel].tolist())), "cell_area_km2": float(area[sel].sum() / 1e6)}
    return SocioGrid(cells=cells, cell_area_m2=area, pop=pop, arable_mu=arable, district=district, district_codes=codes,
                     district_names=dnames, source_crs=source.to_string(), target_crs=target.to_string(), inputs=inputs,
                     source_digest=digest.hexdigest(), grid_sums_by_district=sums)


# ------------------------------------------------------------ face mapping
def mesh_fingerprint(node_xy: np.ndarray, face_nodes: np.ndarray, face_id: np.ndarray, crs: str) -> str:
    digest = hashlib.sha256(b"native-triangle-mesh-fingerprint-v1\n")
    digest.update(str(crs).encode("utf-8") + b"\n")
    digest.update(np.ascontiguousarray(node_xy, dtype="<f8").tobytes())
    digest.update(np.ascontiguousarray(face_nodes, dtype="<i8").tobytes())
    digest.update(np.ascontiguousarray(face_id, dtype="<i8").tobytes())
    return digest.hexdigest()


@dataclass
class FaceSocioMapping:
    mesh_fingerprint: str
    source_digest: str
    face_area_m2: np.ndarray
    pair_face: np.ndarray
    pair_cell: np.ndarray
    pair_area_m2: np.ndarray
    cell_area_m2: np.ndarray
    cell_domain_area_m2: np.ndarray
    cell_pop: np.ndarray
    cell_arable_mu: np.ndarray
    cell_district: np.ndarray
    district_codes: list[str]
    district_names: list[str]
    report: dict[str, Any]

    # derived per-face arrays ---------------------------------------------
    def __post_init__(self) -> None:
        F = self.face_area_m2.size; D = len(self.district_codes)
        w = self.pair_area_m2 / self.cell_area_m2[self.pair_cell]
        self.covered_area_m2 = np.bincount(self.pair_face, weights=self.pair_area_m2, minlength=F)
        covered = self.covered_area_m2 > 0
        pop = np.bincount(self.pair_face, weights=self.cell_pop[self.pair_cell] * w, minlength=F)
        ar = np.bincount(self.pair_face, weights=self.cell_arable_mu[self.pair_cell] * w, minlength=F)
        self.pop = np.where(covered, pop, np.nan)
        self.arable_mu = np.where(covered, ar, np.nan)
        self.coverage_fraction = np.clip(self.covered_area_m2 / self.face_area_m2, 0.0, 1.0)
        # (F, 1+D) weight matrices: column 0 = all, column 1+d = district d
        self.w_pop = np.zeros((F, 1 + D)); self.w_ar = np.zeros((F, 1 + D)); self.w_area = np.zeros((F, 1 + D))
        self.w_pop[:, 0] = pop; self.w_ar[:, 0] = ar; self.w_area[:, 0] = self.covered_area_m2
        pd = self.cell_district[self.pair_cell]
        for d in range(D):
            s = pd == d
            self.w_pop[:, 1 + d] = np.bincount(self.pair_face[s], weights=(self.cell_pop[self.pair_cell] * w)[s], minlength=F)
            self.w_ar[:, 1 + d] = np.bincount(self.pair_face[s], weights=(self.cell_arable_mu[self.pair_cell] * w)[s], minlength=F)
            self.w_area[:, 1 + d] = np.bincount(self.pair_face[s], weights=self.pair_area_m2[s], minlength=F)
        self.dominant_district = np.full(F, -1, dtype=np.int64)
        if D:
            best = np.argmax(self.w_area[:, 1:], axis=1)
            has = self.w_area[:, 1:].max(axis=1) > 0
            self.dominant_district[has] = best[has]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open("xb") as stream:
            np.savez_compressed(stream, format=np.array(MAPPING_FORMAT), mesh_fingerprint=np.array(self.mesh_fingerprint),
                                source_digest=np.array(self.source_digest), face_area_m2=self.face_area_m2,
                                pair_face=self.pair_face.astype(np.int64), pair_cell=self.pair_cell.astype(np.int64),
                                pair_area_m2=self.pair_area_m2, cell_area_m2=self.cell_area_m2,
                                cell_domain_area_m2=self.cell_domain_area_m2, cell_pop=self.cell_pop,
                                cell_arable_mu=self.cell_arable_mu, cell_district=self.cell_district,
                                district_codes=np.array(self.district_codes, dtype=str),
                                district_names=np.array(self.district_names, dtype=str),
                                report_json=np.array(json.dumps(self.report, ensure_ascii=False, default=_json_default)))

    @classmethod
    def load(cls, path: Path, *, mesh_fingerprint: str, source_digest: str) -> "FaceSocioMapping | None":
        with np.load(path, allow_pickle=False) as z:
            if str(z["format"]) != MAPPING_FORMAT or str(z["mesh_fingerprint"]) != mesh_fingerprint \
                    or str(z["source_digest"]) != source_digest:
                return None
            return cls(mesh_fingerprint=mesh_fingerprint, source_digest=source_digest, face_area_m2=z["face_area_m2"],
                       pair_face=z["pair_face"], pair_cell=z["pair_cell"], pair_area_m2=z["pair_area_m2"],
                       cell_area_m2=z["cell_area_m2"], cell_domain_area_m2=z["cell_domain_area_m2"], cell_pop=z["cell_pop"],
                       cell_arable_mu=z["cell_arable_mu"], cell_district=z["cell_district"],
                       district_codes=[str(v) for v in z["district_codes"]], district_names=[str(v) for v in z["district_names"]],
                       report=json.loads(str(z["report_json"])))


def _mesh_outline(tri: np.ndarray):
    """Domain polygon = union of all native triangles (robust for holes and self-touching rings;
    polygonizing boundary edges mis-handled a hole touching the outer ring on the real mesh)."""
    import shapely
    return shapely.union_all(tri)


def build_face_mapping(node_xy: np.ndarray, face_nodes: np.ndarray, face_area_m2: np.ndarray, grid: SocioGrid,
                       *, fingerprint: str, tol: float = 1e-6, chunk: int = 50_000,
                       log: Callable[[str], None] = lambda m: None) -> FaceSocioMapping:
    """Exact area-weighted face mapping with independent area/value conservation checks."""
    import shapely
    from shapely.strtree import STRtree
    started = _time.perf_counter()
    F = face_nodes.shape[0]
    tri = shapely.polygons(node_xy[face_nodes])
    tree = STRtree(grid.cells)
    pf: list[np.ndarray] = []; pc: list[np.ndarray] = []; pa: list[np.ndarray] = []
    for a in range(0, F, chunk):
        part = tri[a:a + chunk]
        fi, ci = tree.query(part, predicate="intersects")
        if fi.size:
            area = shapely.area(shapely.intersection(part[fi], grid.cells[ci]))
            keep = area > 0
            pf.append(fi[keep] + a); pc.append(ci[keep]); pa.append(area[keep])
    pair_face = np.concatenate(pf) if pf else np.zeros(0, np.int64)
    pair_cell = np.concatenate(pc) if pc else np.zeros(0, np.int64)
    pair_area = np.concatenate(pa) if pa else np.zeros(0)
    log(f"socio mapping: {pair_face.size} face/cell overlaps in {_time.perf_counter() - started:.1f} s")
    # Independent area(cell ∩ domain) from the mesh outline.
    domain = _mesh_outline(tri)
    shapely.prepare(domain)
    cand = np.unique(pair_cell)
    cell_dom = np.zeros(grid.cells.size)
    inside = shapely.contains_properly(domain, grid.cells[cand]) if cand.size else np.zeros(0, bool)
    cell_dom[cand[inside]] = grid.cell_area_m2[cand[inside]]
    edge = cand[~inside]
    if edge.size:
        cell_dom[edge] = shapely.area(shapely.intersection(grid.cells[edge], domain))
    alloc = np.bincount(pair_cell, weights=pair_area, minlength=grid.cells.size)
    domain_area = float(math.fsum(face_area_m2.tolist()))
    outline_area = float(shapely.area(domain))
    area_resid = alloc - cell_dom
    face_cov = np.bincount(pair_face, weights=pair_area, minlength=F)
    frac = cell_dom / grid.cell_area_m2
    expect_pop = float(math.fsum((grid.pop * frac).tolist())); expect_ar = float(math.fsum((grid.arable_mu * frac).tolist()))
    w = pair_area / grid.cell_area_m2[pair_cell]
    mapped_pop = float(math.fsum((grid.pop[pair_cell] * w).tolist()))
    mapped_ar = float(math.fsum((grid.arable_mu[pair_cell] * w).tolist()))

    def rel(a: float, b: float) -> float:
        return abs(a - b) / max(1.0, abs(b))

    # Grid self-overlap (double counting risk) among cells touching the domain.
    sub = grid.cells[cand]
    ii, jj = STRtree(sub).query(sub, predicate="intersects")
    m = ii < jj
    overlap = float(shapely.area(shapely.intersection(sub[ii[m]], sub[jj[m]])).sum()) if m.any() else 0.0
    checks = {
        "outline_area_vs_face_area_sum_rel": rel(outline_area, domain_area),
        "allocated_vs_cell_domain_area_total_rel": rel(float(alloc.sum()), float(cell_dom.sum())),
        "allocated_vs_cell_domain_area_max_abs_m2": float(np.abs(area_resid).max()) if area_resid.size else 0.0,
        "face_overallocation_max_m2": float(np.max(face_cov - face_area_m2, initial=0.0)),
        "cell_overallocation_max_m2": float(np.max(alloc - grid.cell_area_m2, initial=0.0)),
        "population_mapped_vs_expected_rel": rel(mapped_pop, expect_pop),
        "arable_mapped_vs_expected_rel": rel(mapped_ar, expect_ar),
        "grid_self_overlap_area_m2": overlap,
    }
    area_tol = tol * max(1.0, float(cell_dom.sum()))
    ok = (checks["outline_area_vs_face_area_sum_rel"] <= tol and checks["allocated_vs_cell_domain_area_total_rel"] <= tol
          and checks["population_mapped_vs_expected_rel"] <= tol and checks["arable_mapped_vs_expected_rel"] <= tol
          and checks["face_overallocation_max_m2"] <= 1e-6 * max(1.0, float(face_area_m2.max()))
          and checks["cell_overallocation_max_m2"] <= 1e-6 * max(1.0, float(grid.cell_area_m2.max()))
          and checks["allocated_vs_cell_domain_area_max_abs_m2"] <= max(area_tol, 1e-6))
    if not ok:
        raise SocioExposureError(f"socio mapping conservation check failed: {checks}")
    touched = cand.size
    full = int(np.count_nonzero(np.abs(cell_dom - grid.cell_area_m2) <= 1e-9 * grid.cell_area_m2))
    covered_faces = face_cov > 0
    partial_faces = covered_faces & (face_cov < face_area_m2 * (1 - 1e-9))
    covered_area = float(face_cov.sum())
    report = {
        "method": "exact planar triangle ∩ cell polygon area in the run CRS (shapely STRtree + vectorised intersection)",
        "elapsed_s": _time.perf_counter() - started,
        "face_count": int(F), "pair_count": int(pair_face.size),
        "domain_area_m2": domain_area, "domain_area_km2": domain_area / 1e6,
        "grid_covered_domain_area_m2": covered_area,
        "grid_coverage_fraction_of_domain": covered_area / domain_area if domain_area > 0 else 0.0,
        "faces_fully_covered": int(np.count_nonzero(covered_faces & ~partial_faces)),
        "faces_partially_covered": int(np.count_nonzero(partial_faces)),
        "faces_not_covered_unavailable": int(np.count_nonzero(~covered_faces)),
        "area_not_covered_m2": float(max(domain_area - covered_area, 0.0)),
        "grid_cells_total": int(grid.cells.size),
        "grid_cells_touching_domain": int(touched), "grid_cells_fully_inside_domain": full,
        "grid_cells_partially_inside_domain": int(touched - full),
        "grid_cells_outside_domain": int(grid.cells.size - touched),
        "grid_population_total_all_cells": float(math.fsum(grid.pop.tolist())),
        "grid_arable_mu_total_all_cells": float(math.fsum(grid.arable_mu.tolist())),
        "population_inside_domain": mapped_pop, "arable_mu_inside_domain": mapped_ar,
        "arable_km2_inside_domain": mapped_ar / MU_PER_KM2,
        "population_expected_from_cell_domain_fractions": expect_pop, "arable_mu_expected_from_cell_domain_fractions": expect_ar,
        "conservation": {"status": "PASS", "rel_tol": tol, **checks},
    }
    return FaceSocioMapping(mesh_fingerprint=fingerprint, source_digest=grid.source_digest, face_area_m2=np.asarray(face_area_m2, float),
                            pair_face=pair_face.astype(np.int64), pair_cell=pair_cell.astype(np.int64), pair_area_m2=pair_area,
                            cell_area_m2=grid.cell_area_m2, cell_domain_area_m2=cell_dom, cell_pop=grid.pop,
                            cell_arable_mu=grid.arable_mu, cell_district=grid.district, district_codes=list(grid.district_codes),
                            district_names=list(grid.district_names), report=report)


def load_or_build_mapping(node_xy: np.ndarray, face_nodes: np.ndarray, face_id: np.ndarray, face_area_m2: np.ndarray,
                          crs: str, options: SocioOptions, *, log: Callable[[str], None] = lambda m: None
                          ) -> tuple[FaceSocioMapping, SocioGrid, dict[str, Any]]:
    grid = read_socio_grid(options.grid_cells_path, crs, options)
    fp = mesh_fingerprint(node_xy, face_nodes, face_id, crs)
    cache_info: dict[str, Any] = {"mesh_fingerprint": fp, "source_digest": grid.source_digest, "cache_dir": None, "hit": False}
    cache_path = None
    if options.mapping_cache_dir is not None:
        cache_path = Path(options.mapping_cache_dir) / f"face_socio_mapping_{fp[:16]}_{grid.source_digest[:12]}.npz"
        cache_info["cache_dir"] = str(Path(options.mapping_cache_dir).resolve()); cache_info["cache_file"] = cache_path.name
        if cache_path.is_file():
            mapping = FaceSocioMapping.load(cache_path, mesh_fingerprint=fp, source_digest=grid.source_digest)
            if mapping is not None and mapping.face_area_m2.size == face_nodes.shape[0]:
                cache_info["hit"] = True; log(f"socio mapping cache hit {cache_path.name}")
                return mapping, grid, cache_info
    mapping = build_face_mapping(node_xy, face_nodes, face_area_m2, grid, fingerprint=fp, tol=options.conservation_rel_tol, log=log)
    if cache_path is not None and not cache_path.exists():
        mapping.save(cache_path)
    return mapping, grid, cache_info


# ---------------------------------------------------------------- exposure
def _class_labels(edges: tuple[float, ...]) -> list[str]:
    return [f"{lo:g}_{hi:g}" for lo, hi in zip(edges, edges[1:])] + [f"{edges[-1]:g}_inf"]


class ExposureAccumulator:
    """Per-frame and envelope exposure totals (all + per district, + depth classes)."""

    def __init__(self, mapping: FaceSocioMapping, options: SocioOptions, time_origin: str | None = None):
        self.m = mapping; self.thr = float(options.exposure_depth_threshold_m)
        self.edges = tuple(float(v) for v in options.depth_class_edges_m)
        self.labels = _class_labels(self.edges)
        self.origin = datetime.fromisoformat(time_origin) if time_origin else None
        self.rows: list[dict[str, Any]] = []
        self.scopes = [("ALL", "全部")] + list(zip(mapping.district_codes, mapping.district_names))
        self.no_socio = ~(mapping.covered_area_m2 > 0)

    def columns(self) -> list[str]:
        cols = ["aggregation", "frame_index", "time_s", "time_iso", "district_code", "district_name", "depth_threshold_m",
                "exposed_face_count", "exposed_area_m2", "exposed_area_without_socio_m2",
                "exposed_pop", "exposed_arable_mu", "exposed_arable_km2"]
        for lab in self.labels:
            cols += [f"pop_d{lab}", f"arable_mu_d{lab}", f"area_m2_d{lab}"]
        return cols

    def _rows(self, aggregation: str, index: int | None, time_s: float | None, h: np.ndarray) -> list[dict[str, Any]]:
        h = np.asarray(h, dtype=np.float64)
        exposed = h >= self.thr
        e = exposed.astype(np.float64)
        pop = e @ self.m.w_pop; ar = e @ self.m.w_ar; carea = e @ self.m.w_area
        face_area_all = float(self.m.face_area_m2[exposed].sum())
        no_socio_area = float(self.m.face_area_m2[exposed & self.no_socio].sum())
        cls = np.digitize(h, self.edges) - 1  # -1 below first edge
        per_class = []
        for k in range(len(self.labels)):
            s = (cls == k).astype(np.float64)
            per_class.append((s @ self.m.w_pop, s @ self.m.w_ar, s @ self.m.w_area, float(self.m.face_area_m2[cls == k].sum())))
        time_iso = (self.origin + timedelta(seconds=float(time_s))).isoformat() if (self.origin and time_s is not None) else ""
        out = []
        for j, (code, name) in enumerate(self.scopes):
            row = {"aggregation": aggregation, "frame_index": "" if index is None else int(index),
                   "time_s": "" if time_s is None else float(time_s), "time_iso": time_iso,
                   "district_code": code, "district_name": name, "depth_threshold_m": self.thr,
                   "exposed_face_count": int(np.count_nonzero(exposed)) if j == 0 else int(np.count_nonzero(exposed & (self.m.w_area[:, j] > 0))),
                   "exposed_area_m2": face_area_all if j == 0 else float(carea[j]),
                   "exposed_area_without_socio_m2": no_socio_area if j == 0 else 0.0,
                   "exposed_pop": float(pop[j]), "exposed_arable_mu": float(ar[j]), "exposed_arable_km2": float(ar[j]) / MU_PER_KM2}
            for lab, (cp, ca, cA, cFace) in zip(self.labels, per_class):
                row[f"pop_d{lab}"] = float(cp[j]); row[f"arable_mu_d{lab}"] = float(ca[j])
                row[f"area_m2_d{lab}"] = cFace if j == 0 else float(cA[j])
            out.append(row)
        return out

    def add_frame(self, index: int, time_s: float, h: np.ndarray) -> None:
        self.rows.extend(self._rows("FRAME", index, time_s, h))

    def add_envelope(self, max_h: np.ndarray, aggregation: str = "MAX_OVER_SAVED_FRAMES") -> list[dict[str, Any]]:
        rows = self._rows(aggregation, None, None, max_h)
        self.rows.extend(rows)
        return rows

    def csv_bytes(self) -> bytes:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=self.columns(), lineterminator="\n")
        writer.writeheader()
        for row in self.rows:
            writer.writerow({k: (repr(v) if isinstance(v, float) else v) for k, v in row.items()})
        return buffer.getvalue().encode("utf-8")


# ------------------------------------------------------------ side tables
def _read_csv(path: Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _district_context(options: SocioOptions, mapping: FaceSocioMapping, grid: SocioGrid) -> dict[str, Any]:
    out: dict[str, Any] = {}
    D = len(mapping.district_codes)
    inside = {}
    for d, code in enumerate(mapping.district_codes):
        inside[code] = {"name": mapping.district_names[d],
                        "population_inside_domain": float(mapping.w_pop[:, 1 + d].sum()),
                        "arable_mu_inside_domain": float(mapping.w_ar[:, 1 + d].sum()),
                        "arable_km2_inside_domain": float(mapping.w_ar[:, 1 + d].sum()) / MU_PER_KM2,
                        "grid_covered_domain_area_km2": float(mapping.w_area[:, 1 + d].sum()) / 1e6,
                        "grid_cells_sum": grid.grid_sums_by_district.get(code)}
    out["by_district_inside_domain"] = inside
    if D == 0:
        out["district_status"] = "NO_DISTRICT_CODE_FIELD"
    if options.district_totals_csv is not None:
        rows = _read_csv(options.district_totals_csv)
        need = {"qu_dm", "population", "area_km2", "arable_km2"}
        if not rows or not need <= set(rows[0]):
            raise SocioExposureError(f"district totals CSV {options.district_totals_csv} needs columns {sorted(need)}")
        for r in rows:
            code = r["qu_dm"].strip()
            census = {"population": float(r["population"]), "area_km2": float(r["area_km2"]), "arable_km2": float(r["arable_km2"])}
            entry = inside.setdefault(code, {"name": "", "population_inside_domain": 0.0, "arable_mu_inside_domain": 0.0})
            entry["district_census_totals"] = census
            entry["population_share_of_district_inside_domain"] = entry["population_inside_domain"] / census["population"] if census["population"] else None
            entry["arable_share_of_district_inside_domain"] = (entry.get("arable_km2_inside_domain", 0.0) / census["arable_km2"]) if census["arable_km2"] else None
    if options.district_grid_summary_csv is not None:
        rows = _read_csv(options.district_grid_summary_csv)
        need = {"区代码", "网格数", "网格内常住人口_合计", "网格内耕地_亩_合计"}
        if not rows or not need <= set(rows[0]):
            raise SocioExposureError(f"district grid summary CSV {options.district_grid_summary_csv} needs columns {sorted(need)}")
        cross = {}
        for r in rows:
            code = r["区代码"].strip(); mine = grid.grid_sums_by_district.get(code) or {}
            cross[code] = {"csv_cell_count": int(float(r["网格数"])), "shp_cell_count": mine.get("cell_count"),
                           "csv_pop": float(r["网格内常住人口_合计"]), "shp_pop": mine.get("pop"),
                           "csv_arable_mu": float(r["网格内耕地_亩_合计"]), "shp_arable_mu": mine.get("arable_mu")}
            ok = (cross[code]["shp_cell_count"] == cross[code]["csv_cell_count"]
                  and mine and abs(mine["pop"] - cross[code]["csv_pop"]) <= 0.5 + 1e-4 * cross[code]["csv_pop"]
                  and abs(mine["arable_mu"] - cross[code]["csv_arable_mu"]) <= 0.5 + 1e-4 * cross[code]["csv_arable_mu"])
            cross[code]["status"] = "PASS" if ok else "MISMATCH"
        out["grid_summary_crosscheck"] = cross
    return out


# ------------------------------------------------------------ bundle output
@dataclass
class SocioResult:
    doc: dict[str, Any]
    shp_fields: tuple
    shp_columns: Callable[[np.ndarray], dict[str, list[Any]]]


SHP_FIELDS = (("POP", "N", 20, 6, "mapped_population_persons_blank_if_no_grid_coverage", "persons"),
              ("ARABLE_MU", "N", 20, 6, "mapped_arable_land_mu_blank_if_no_grid_coverage", "mu"),
              ("SOCIO_COV", "N", 12, 6, "fraction_of_face_area_covered_by_socio_grid", "1"),
              ("QU_DM", "C", 12, 0, "dominant_district_code_by_covered_area", None),
              ("EXPOSED", "N", 2, 0, "1_if_saved_frame_max_depth_ge_exposure_threshold", None))


def _dbf_num(values: np.ndarray, decimals: int) -> list[float | None]:
    return [None if not math.isfinite(v) else v for v in np.round(values.astype(np.float64), decimals).tolist()]


def shp_columns_for(mapping: FaceSocioMapping, max_h: np.ndarray, threshold: float) -> Callable[[np.ndarray], dict[str, list[Any]]]:
    codes = mapping.district_codes
    def build(selection: np.ndarray) -> dict[str, list[Any]]:
        dom = mapping.dominant_district[selection]
        return {"POP": _dbf_num(mapping.pop[selection], 6), "ARABLE_MU": _dbf_num(mapping.arable_mu[selection], 6),
                "SOCIO_COV": _dbf_num(mapping.coverage_fraction[selection], 6),
                "QU_DM": [codes[d] if d >= 0 else "" for d in dom.tolist()],
                "EXPOSED": [int(v) for v in (max_h[selection] >= threshold).tolist()]}
    return build


def run_socio_overlay(out: Path, native: Any, options: SocioOptions, frames: Iterable[Any] | None,
                      *, native_sha256: str, log: Callable[[str], None] = lambda m: None,
                      accumulator: ExposureAccumulator | None = None, mapping: FaceSocioMapping | None = None,
                      grid: SocioGrid | None = None, cache_info: dict[str, Any] | None = None) -> SocioResult:
    """Write ``<out>/socio_exposure/`` (mapping npz, frame CSV, summary JSON). ``frames`` may be None when an
    accumulator already holds the per-frame rows (streaming export)."""
    started = _time.perf_counter()
    meta = native.metadata
    if mapping is None:
        mapping, grid, cache_info = load_or_build_mapping(native.node_xy_m, native.face_nodes, native.face_id,
                                                          native.face_area_m2, meta.coordinate_system, options, log=log)
    acc = accumulator or ExposureAccumulator(mapping, options, meta.time_origin)
    if frames is not None:
        for frame in frames:
            acc.add_frame(frame.index, frame.time_s, frame.water_depth_m)
    max_h = native.static("saved_frame_max_water_depth")
    env_rows = acc.add_envelope(max_h)
    solver_env = None
    if "solver_max_water_depth_m" in native._ds.variables:
        solver_env = acc.add_envelope(native.static("solver_max_water_depth_m"), "SOLVER_MAX_ENVELOPE")
    target = out / SOCIO_DIR
    target.mkdir()
    mapping_path = target / MAPPING_FILE
    mapping.save(mapping_path)
    csv_path = target / FRAME_CSV
    with csv_path.open("xb") as stream:
        stream.write(acc.csv_bytes())
    frame_all = [r for r in acc.rows if r["aggregation"] == "FRAME" and r["district_code"] == "ALL"]
    peak = max(frame_all, key=lambda r: r["exposed_pop"]) if frame_all else None
    last = frame_all[-1] if frame_all else None
    inputs = dict(grid.inputs) if grid is not None else {}
    for key, p in (("district_totals_csv", options.district_totals_csv), ("district_grid_summary_csv", options.district_grid_summary_csv)):
        if p is not None:
            inputs[key] = {"path": str(Path(p).resolve()), "sha256": _sha_file(Path(p)), "bytes": Path(p).stat().st_size}
    inputs["result_native.nc"] = {"sha256": native_sha256}
    context = _district_context(options, mapping, grid) if grid is not None else {}
    doc = {
        "format": SOCIO_FORMAT, "schema_version": "1.0", "run_id": meta.run_id, "case_id": meta.case_id,
        "status": "COMPLETE", "data_origin": meta.data_origin, "forcing_notice": meta.forcing.notice,
        "production_approved": meta.production_approved,
        "coordinate_system": meta.coordinate_system, "source_crs": grid.source_crs if grid else None,
        "inputs": inputs,
        "options": {"population_field": options.population_field, "arable_mu_field": options.arable_mu_field,
                    "district_code_field": options.district_code_field, "district_name_field": options.district_name_field,
                    "exposure_depth_threshold_m": options.exposure_depth_threshold_m,
                    "exposure_rule": "face exposed iff face depth >= threshold",
                    "depth_class_edges_m": list(acc.edges), "depth_classes": acc.labels,
                    "depth_class_rule": "[edge_k, edge_k+1) and [edge_last, inf), face depth",
                    "conservation_rel_tol": options.conservation_rel_tol},
        "units": {"pop": "persons", "arable_mu": "mu (亩; 1 km2 = 1500 mu)", "arable_km2": "km2", "area": "m2", "depth": "m"},
        "mapping": {"mesh_fingerprint": mapping.mesh_fingerprint, "source_digest": mapping.source_digest,
                    "cache": cache_info, **mapping.report},
        "districts": context,
        "envelope_saved_frames": env_rows,
        "envelope_solver_max": solver_env,
        "peak_saved_frame_by_exposed_pop": peak, "last_saved_frame": last,
        "method": [
            "Grid cell polygons reprojected vertex-by-vertex from the shapefile .prj CRS to the run CRS (pyproj, always_xy).",
            "Native triangle ∩ cell polygon areas computed exactly (shapely 2, STRtree candidates).",
            "Face value = sum_c value_c * area(face ∩ c) / area(c); partial cells contribute only their inside fraction.",
            "Faces with no grid coverage are unavailable (NaN), never 0; no fill or interpolation.",
            "Conservation: sum_f value_f == sum_c value_c * area(c ∩ domain)/area(c), domain from the mesh outline.",
            "Exposure per saved frame and for the saved-frame maximum-depth envelope; district split by cell qu_dm.",
        ],
        "limitations": list(LIMITATIONS),
        "files": {}, "elapsed_s": None,
    }
    doc["files"] = {name: {"sha256": _sha_file(target / name), "bytes": (target / name).stat().st_size}
                    for name in (MAPPING_FILE, FRAME_CSV)}
    doc["elapsed_s"] = _time.perf_counter() - started
    with (target / SUMMARY_JSON).open("xb") as stream:
        stream.write(_json_bytes(doc))
    return SocioResult(doc=doc, shp_fields=SHP_FIELDS,
                       shp_columns=shp_columns_for(mapping, max_h, options.exposure_depth_threshold_m))


def run_json_block(result: SocioResult) -> dict[str, Any]:
    doc = result.doc
    env = next(r for r in doc["envelope_saved_frames"] if r["district_code"] == "ALL")
    return {"status": "COMPLETE_EXPOSURE_ONLY", "summary": f"{SOCIO_DIR}/{SUMMARY_JSON}",
            "frame_csv": f"{SOCIO_DIR}/{FRAME_CSV}", "mapping": f"{SOCIO_DIR}/{MAPPING_FILE}",
            "summary_sha256": _sha_file_rel(doc), "shp_fields_on": list(MAX_SURFACE_ROLES),
            "threshold_m": doc["options"]["exposure_depth_threshold_m"],
            "population_inside_domain": doc["mapping"]["population_inside_domain"],
            "arable_mu_inside_domain": doc["mapping"]["arable_mu_inside_domain"],
            "grid_coverage_fraction_of_domain": doc["mapping"]["grid_coverage_fraction_of_domain"],
            "envelope_exposed_pop": env["exposed_pop"], "envelope_exposed_arable_mu": env["exposed_arable_mu"],
            "economic_loss": "NOT_COMPUTED_NO_ASSET_VALUES"}


def _sha_file_rel(doc: dict[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(doc)).hexdigest()


def verify_socio_outputs(out: Path, run: dict[str, Any]) -> dict[str, Any]:
    """Re-open the socio outputs and check internal consistency (used by verify_large_run_bundle)."""
    block = run.get("socio_exposure")
    target = out / SOCIO_DIR
    if not block:
        if target.exists():
            raise SocioExposureError("socio_exposure/ present but run.json has no socio_exposure block")
        return {"status": "NOT_REQUESTED"}
    doc = json.loads((target / SUMMARY_JSON).read_text(encoding="utf-8"))
    if doc.get("format") != SOCIO_FORMAT or doc.get("run_id") != run["run_id"]:
        raise SocioExposureError("socio exposure summary format/run_id mismatch")
    if hashlib.sha256((target / SUMMARY_JSON).read_bytes()).hexdigest() != block["summary_sha256"]:
        raise SocioExposureError("socio exposure summary sha256 differs from run.json")
    for name, row in doc["files"].items():
        if _sha_file(target / name) != row["sha256"]:
            raise SocioExposureError(f"socio exposure file {name} sha256 mismatch")
    with np.load(target / MAPPING_FILE, allow_pickle=False) as z:
        if str(z["mesh_fingerprint"]) != doc["mapping"]["mesh_fingerprint"] or z["face_area_m2"].size != run["native_result"]["face_count"]:
            raise SocioExposureError("socio mapping npz fingerprint/face count mismatch")
    with (target / FRAME_CSV).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    T = run["native_result"]["saved_frame_count"]
    frames = {r["frame_index"] for r in rows if r["aggregation"] == "FRAME"}
    if len(frames) != T:
        raise SocioExposureError("socio exposure CSV frame count mismatch")
    if doc["mapping"]["conservation"]["status"] != "PASS":
        raise SocioExposureError("socio mapping conservation not PASS")
    return {"status": "PASS", "csv_rows": len(rows), "population_inside_domain": doc["mapping"]["population_inside_domain"]}


# ------------------------------------------------ post-hoc bundle augmentation
def apply_socio_exposure_to_bundle(bundle_dir: str | Path, options: SocioOptions, *,
                                   log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """Add the socio overlay to an already exported large-run bundle (explicit, opt-in).

    Writes ``socio_exposure/``, re-writes the two maximum-wet-surface SHP packages with the
    POP/ARABLE_MU/SOCIO_COV/QU_DM/EXPOSED fields, updates run.json/summary.json/parse_summary.json and
    re-generates checksums.json. Refuses when socio_exposure/ already exists.
    """
    from .frame_sink import LargeNativeResult
    from .large_run_export import _export_shp, _write_checksums, verify_large_run_bundle
    from .large_run_profile import parse_profile
    out = Path(bundle_dir).resolve()
    run_path = out / "run.json"
    if not run_path.is_file() or not (out / "result_native.nc").is_file():
        raise SocioExposureError(f"not an exported large-run bundle: {out}")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if (out / SOCIO_DIR).exists() or run.get("socio_exposure"):
        raise SocioExposureError("bundle already has socio_exposure; no-clobber refused")
    profile_doc = dict(run["large_run_profile"]); profile_doc.pop("socio_exposure", None)
    profile = parse_profile(profile_doc)
    with LargeNativeResult(out / "result_native.nc") as native:
        sha = native.sha256()
        if sha != run["native_result"]["sha256"]:
            raise SocioExposureError("result_native.nc sha256 differs from run.json")
        result = run_socio_overlay(out, native, options, native.iter_frames(), native_sha256=sha, log=log)
        for role in MAX_SURFACE_ROLES:
            shutil.rmtree(out / "shp" / role)
        new_rows = _export_shp(out / "shp", native, profile, sha, socio=result, roles=MAX_SURFACE_ROLES)
    by_role = {row["role"]: row for row in new_rows}
    run["shp_packages"] = [by_role.get(row["role"], row) for row in run["shp_packages"]]
    block = run_json_block(result)
    run["socio_exposure"] = block
    run["social_economy_status"] = block["status"]
    run.setdefault("legacy_optional_artifacts", {})["social_economy_overlay"] = {"status": block["status"], "summary": block["summary"]}
    run_path.write_bytes(_json_bytes(run))
    for name in ("summary.json", "parse_summary.json"):
        path = out / name
        doc = json.loads(path.read_text(encoding="utf-8"))
        if name == "summary.json":
            doc["social_economy"] = block["status"]; doc["socio_exposure"] = block
        else:
            doc["shp_packages"] = run["shp_packages"]; doc["socio_exposure"] = {"status": "PASS", "summary": block["summary"]}
        path.write_bytes(_json_bytes(doc))
    (out / "checksums.json").unlink()
    _write_checksums(out, run["run_id"])
    report = {"status": "PASS", "bundle": str(out), "socio_exposure": block}
    report["verify"] = verify_large_run_bundle(out)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hydrodynamics-socio-exposure",
                                     description="Add the socio-economic exposure overlay to an exported large-run bundle (in place).")
    parser.add_argument("bundle_dir")
    parser.add_argument("--socio", required=True, help="grid_cells_socio.shp (50 m cells with s_pop / s_ar_mu / qu_dm / qu)")
    parser.add_argument("--district-totals", help="district census totals CSV (qu_dm,population,area_km2,arable_km2)")
    parser.add_argument("--grid-summary", help="per-district grid summary CSV (区代码,网格数,网格内常住人口_合计,网格内耕地_亩_合计)")
    parser.add_argument("--threshold", type=float, default=0.15, help="exposure depth threshold, m (default 0.15)")
    parser.add_argument("--classes", default="0.15,0.5,1.5,3", help="depth class edges, m (comma separated)")
    parser.add_argument("--cache-dir", help="directory for the reusable face mapping cache (keyed by mesh fingerprint)")
    args = parser.parse_args(argv)
    try:
        options = SocioOptions(grid_cells_path=Path(args.socio),
                               district_totals_csv=Path(args.district_totals) if args.district_totals else None,
                               district_grid_summary_csv=Path(args.grid_summary) if args.grid_summary else None,
                               exposure_depth_threshold_m=args.threshold,
                               depth_class_edges_m=tuple(float(v) for v in args.classes.split(",") if v.strip()),
                               mapping_cache_dir=Path(args.cache_dir) if args.cache_dir else None)
        report = apply_socio_exposure_to_bundle(args.bundle_dir, options, log=lambda m: print(m, file=sys.stderr, flush=True))
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=_json_default))
    except (SocioExposureError, ValueError, OSError) as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
