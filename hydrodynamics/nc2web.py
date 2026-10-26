from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
from typing import Any, Mapping, Sequence

import numpy as np

from .netcdf_writer import OutputDependencyError

TARGET_WEB_EPSG = "EPSG:4548"
MESH_MAGIC = b"NC2WM01\n"
FRAME_MAGIC = b"NC2WF01\n"
XY_SCALE = 0.001
HYDRO_VARS = ("water_depth", "water_level", "velocity_x", "velocity_y", "u", "v")


class Nc2WebError(ValueError):
    """Raised when a NetCDF artifact cannot be converted to the web mesh/frames."""


@dataclass(frozen=True)
class Nc2WebManifest:
    status: str
    epsg: str
    source: str
    mesh: str
    nx: int
    ny: int
    origin: tuple[float, float]
    dx: float
    dy: float
    node_count: int
    triangle_count: int
    times: tuple[float | None, ...]
    variables: tuple[str, ...]
    frames: tuple[dict[str, Any], ...]
    extrema: dict[str, dict[str, float]]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    artifact_type: str | None = None
    xy_scale: float = XY_SCALE
    format: str = "nc2web"
    version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["origin"] = [self.origin[0], self.origin[1]]
        payload["times"] = list(self.times)
        payload["variables"] = list(self.variables)
        payload["frames"] = [dict(item) for item in self.frames]
        payload["warnings"] = list(self.warnings)
        payload["blockers"] = list(self.blockers)
        return payload


def _open_dataset(path: Path):
    try:
        import netCDF4
    except ImportError as exc:
        raise OutputDependencyError("nc2web conversion requires optional dependency 'netCDF4'") from exc
    if not path.is_file():
        raise Nc2WebError(f"NetCDF file not found: {path}")
    return netCDF4.Dataset(str(path), "r")


def _read_hydro_values(variable: Any, raw: Any, active_mask: np.ndarray | None) -> np.ndarray:
    """Read values while preserving valid zeros under the historical fill=0 contract.

    New compatibility files use NaN as the fill value. Older structured files
    used zero plus active_mask, which makes a valid active zero indistinguishable
    from the masked fill to netCDF4. Restore only masked samples whose
    underlying value is exactly that zero fill and whose metadata does not also
    classify zero as missing or outside the valid range.
    """
    masked = np.ma.isMaskedArray(raw)
    data = np.asarray(np.ma.getdata(raw) if masked else raw, dtype=np.float64).copy()
    if masked:
        missing = np.ma.getmaskarray(raw)
        fill_value = getattr(variable, "_FillValue", None)
        availability = str(getattr(variable, "availability", "")).strip().upper()
        available = not availability.startswith("NOT_AVAILABLE")
        legacy_zero_fill = (
            active_mask is not None and available and fill_value is not None
            and np.isscalar(fill_value) and float(fill_value) == 0.0
        )
        valid_legacy_zero = (
            missing & active_mask & np.isfinite(data) & (data == float(fill_value))
            if legacy_zero_fill else np.zeros(data.shape, dtype=bool)
        )
        if legacy_zero_fill:
            # A second missing marker or validity attribute can mask a value
            # independently of _FillValue. Keep those zeros unavailable.
            try:missing_values=np.asarray(getattr(variable,"missing_value"),dtype=np.float64).reshape(-1)
            except Exception:missing_values=np.asarray([],dtype=np.float64)
            if np.any(missing_values==0.0):valid_legacy_zero[:]=False
            try:valid_range=np.asarray(getattr(variable,"valid_range"),dtype=np.float64).reshape(-1)
            except Exception:valid_range=np.asarray([],dtype=np.float64)
            if valid_range.size>=2 and not (valid_range[0]<=0.0<=valid_range[1]):valid_legacy_zero[:]=False
            try:valid_min=float(getattr(variable,"valid_min"))
            except Exception:valid_min=-np.inf
            try:valid_max=float(getattr(variable,"valid_max"))
            except Exception:valid_max=np.inf
            if not (valid_min<=0.0<=valid_max):valid_legacy_zero[:]=False
            try:scale_factor=float(getattr(variable,"scale_factor",1.0));add_offset=float(getattr(variable,"add_offset",0.0))
            except Exception:scale_factor,add_offset=np.nan,np.nan
            if scale_factor!=1.0 or add_offset!=0.0:valid_legacy_zero[:]=False
        data[missing & ~valid_legacy_zero] = np.nan
        data[valid_legacy_zero] = 0.0
    if active_mask is not None:
        data = np.where(active_mask, data, np.nan)
    return np.where(np.isfinite(data), data, np.nan)


def _reject_native_triangular_artifact(path: Path) -> None:
    """Structured web encoders must not flatten native triangular face data."""
    with _open_dataset(path) as dataset:
        product_type = str(getattr(dataset, "product_type", ""))
        if product_type.startswith("experimental_triangular_") or "mesh2d" in dataset.variables or "face_nodes" in dataset.variables:
            security_note = " with security exclusion" if getattr(dataset, "security_exclusion_json", None) is not None else ""
            raise Nc2WebError(
                "UNSUPPORTED_NATIVE_TRIANGULAR_RESULT: structured nc2web/stepjson encoders cannot represent native "
                f"triangular{security_note} face sets"
            )


def _finite_diff(values: np.ndarray) -> float:
    if values.size < 2:
        raise Nc2WebError("coordinate vector must contain at least 2 samples to infer spacing")
    step = float(values[1] - values[0])
    if not np.isfinite(step) or step == 0:
        raise Nc2WebError("coordinate spacing must be a finite non-zero value")
    return step


def _normalize_epsg(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    upper = text.upper().replace(" ", "")
    if upper.isdigit():
        return f"EPSG:{upper}"
    if upper.startswith("EPSG:"):
        return f"EPSG:{upper.split(':', 1)[1]}"
    return text


def _reproject_xy(x: np.ndarray, y: np.ndarray, source_epsg: str, target_epsg: str) -> tuple[np.ndarray, np.ndarray]:
    if _normalize_epsg(source_epsg) == _normalize_epsg(target_epsg):
        return x.astype(float), y.astype(float)
    try:
        from pyproj import Transformer
    except ImportError as exc:
        raise Nc2WebError("coordinate conversion to EPSG:4548 requires optional dependency 'pyproj'") from exc
    transformer = Transformer.from_crs(source_epsg, target_epsg, always_xy=True)
    xx, yy = np.meshgrid(x.astype(float), y.astype(float))
    tx, ty = transformer.transform(xx, yy)
    if not np.isfinite(tx).all() or not np.isfinite(ty).all():
        raise Nc2WebError("coordinate conversion produced non-finite values")
    # Structured web mesh needs monotonic 1-D axes. After a true 2-D
    # projection this is only valid when the source grid is already aligned.
    if not np.allclose(tx, tx[0, :][None, :], rtol=0.0, atol=1e-6):
        raise Nc2WebError("reprojected x coordinates are not a north-up structured axis")
    if not np.allclose(ty, ty[:, 0][:, None], rtol=0.0, atol=1e-6):
        raise Nc2WebError("reprojected y coordinates are not a north-up structured axis")
    return tx[0, :].astype(float), ty[:, 0].astype(float)


def _reproject_single_axis_step(value:float,other:float,step:float,*,axis:str,source_epsg:str,target_epsg:str)->float:
    try:from pyproj import Transformer
    except ImportError as exc:raise Nc2WebError("coordinate conversion to EPSG:4548 requires optional dependency 'pyproj'") from exc
    transformer=Transformer.from_crs(source_epsg,target_epsg,always_xy=True)
    if axis=="x":
        base=transformer.transform(value,other);shifted=transformer.transform(value+step,other);delta=shifted[0]-base[0]
    else:
        base=transformer.transform(other,value);shifted=transformer.transform(other,value+step);delta=shifted[1]-base[1]
    if not np.isfinite(delta) or delta==0:raise Nc2WebError(f"cannot derive finite projected {axis} spacing for singleton axis")
    return float(delta)


def _quantize(values: np.ndarray, origin: float, scale: float) -> np.ndarray:
    return np.rint((values - origin) / scale).astype("<i4")


def _write_mesh(path: Path, *, xs: np.ndarray, ys: np.ndarray, active: np.ndarray | None, origin: tuple[float, float], scale: float) -> tuple[int, int]:
    nx = int(xs.size)
    ny = int(ys.size)
    node_x, node_y = np.meshgrid(xs, ys)
    qx = _quantize(node_x, origin[0], scale)
    qy = _quantize(node_y, origin[1], scale)
    n_nodes = nx * ny
    triangles: list[tuple[int, int, int]] = []
    cell_ny = ny - 1
    cell_nx = nx - 1
    for row in range(cell_ny):
        for col in range(cell_nx):
            if active is not None and not bool(active[row, col]):
                continue
            n00 = row * nx + col
            n10 = n00 + 1
            n01 = n00 + nx
            n11 = n01 + 1
            triangles.append((n00, n10, n01))
            triangles.append((n10, n11, n01))
    with path.open("xb") as stream:
        stream.write(MESH_MAGIC)
        stream.write(struct.pack("<IIfff", n_nodes, len(triangles), origin[0], origin[1], scale))
        stream.write(qx.astype("<i4", copy=False).tobytes())
        stream.write(qy.astype("<i4", copy=False).tobytes())
        stream.write(np.asarray(triangles, dtype="<u4").tobytes())
        stream.flush();os.fsync(stream.fileno())
    return n_nodes, len(triangles)


def _write_frame(path: Path, arrays: Sequence[np.ndarray]) -> None:
    first = arrays[0]
    ny, nx = first.shape
    with path.open("xb") as stream:
        stream.write(FRAME_MAGIC);stream.write(struct.pack("<III", ny, nx, len(arrays)))
        for item in arrays:stream.write(np.asarray(item, dtype="<f4").tobytes(order="C"))
        stream.flush();os.fsync(stream.fileno())


def _new_output_directory(path:Path)->None:
    if path.exists():raise Nc2WebError(f"output directory already exists; no-clobber refused: {path}")
    if not path.parent.is_dir():raise Nc2WebError(f"output parent directory does not exist: {path.parent}")
    path.mkdir()


def _write_json_noclobber(path:Path,value:Any)->None:
    raw=(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n").encode("utf-8")
    with path.open("xb") as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())


def convert_netcdf_to_web(
    source: str | Path,
    output_dir: str | Path,
    *,
    target_epsg: str = TARGET_WEB_EPSG,
    xy_scale: float = XY_SCALE,
) -> Nc2WebManifest:
    """Parse a 2-D hydro NetCDF artifact into manifest/mesh/frame web files.

    Coordinates in the web outputs are always declared as EPSG:4548. Source
    lon/lat axes are reprojected only when they are already a north-up
    structured grid. This function does not start the Solver or invent
    observation time series.
    """

    src = Path(source)
    out = Path(output_dir).resolve()
    _reject_native_triangular_artifact(src)
    target = _normalize_epsg(target_epsg) or TARGET_WEB_EPSG
    if target != TARGET_WEB_EPSG:
        raise Nc2WebError(f"web coordinates must use {TARGET_WEB_EPSG}, got {target}")
    _new_output_directory(out)
    warnings: list[str] = []
    blockers: list[str] = []

    with _open_dataset(src) as ds:
        if "x" not in ds.variables or "y" not in ds.variables:
            raise Nc2WebError("NetCDF must contain 1-D x and y coordinate variables")
        x = np.asarray(ds.variables["x"][:], dtype=float)
        y = np.asarray(ds.variables["y"][:], dtype=float)
        if x.ndim != 1 or y.ndim != 1 or x.size < 1 or y.size < 1:
            raise Nc2WebError("x and y must be 1-D cell-centered axes")
        source_crs = _normalize_epsg(getattr(ds, "coordinate_system", None))
        if source_crs is None:
            warnings.append("source coordinate_system missing; web epsg is declared as EPSG:4548 without reprojection")
            x_web, y_web = x, y
        else:
            x_web, y_web = _reproject_xy(x, y, source_crs, target)
        spacing_parts=str(getattr(ds,"grid_spacing","50,50")).split(",")
        if len(spacing_parts)!=2:raise Nc2WebError("grid_spacing must contain two source-axis steps")
        source_dx=float(getattr(ds,"dx",spacing_parts[0]));source_dy=float(getattr(ds,"dy",spacing_parts[1]))
        if x_web.size>1:
            dx=_finite_diff(x_web)
            if not np.allclose(np.diff(x_web),dx,rtol=0,atol=1e-6):raise Nc2WebError("projected x axis is not evenly spaced")
        elif source_crs and _normalize_epsg(source_crs)!=_normalize_epsg(target):
            dx=_reproject_single_axis_step(float(x[0]),float(y[0]),source_dx,axis="x",source_epsg=source_crs,target_epsg=target)
        else:dx=source_dx
        if y_web.size>1:
            dy=_finite_diff(y_web)
            if not np.allclose(np.diff(y_web),dy,rtol=0,atol=1e-6):raise Nc2WebError("projected y axis is not evenly spaced")
        elif source_crs and _normalize_epsg(source_crs)!=_normalize_epsg(target):
            dy=_reproject_single_axis_step(float(y[0]),float(x[0]),source_dy,axis="y",source_epsg=source_crs,target_epsg=target)
        else:dy=source_dy
        xmin=float(x_web[0]-0.5*dx);ymin=float(y_web[0]-0.5*dy)
        origin = (xmin, ymin)
        corner_x = xmin + np.arange(x_web.size + 1) * dx
        corner_y = ymin + np.arange(y_web.size + 1) * dy
        active = np.asarray(ds.variables["active_mask"][:], dtype=bool) if "active_mask" in ds.variables else None
        artifact_type = getattr(ds, "artifact_type", None)
        hydro: dict[str, Any] = {}
        hydro_dimensions:dict[str,tuple[str,...]]={}
        for name in HYDRO_VARS:
            if name not in ds.variables:
                continue
            variable=ds.variables[name]
            dims = tuple(variable.dimensions)
            if dims not in (("y","x"),("time","y","x")):
                warnings.append(f"{name} skipped because dimensions are {dims}, not (time,y,x) or (y,x)")
                continue
            if variable.shape[-2:] != (y_web.size, x_web.size):
                raise Nc2WebError(f"{name} spatial shape does not match y/x coordinates")
            if dims==("time","y","x") and "time" in ds.dimensions and variable.shape[0]!=len(ds.dimensions["time"]):
                raise Nc2WebError(f"{name} time dimension differs from the native time length")
            hydro[name]=variable;hydro_dimensions[name]=dims
        times: list[float | None]
        if "time" in ds.variables and hydro:
            times = [float(item) for item in np.asarray(ds.variables["time"][:], dtype=np.float64).tolist()]
        elif hydro:
            timed=next((variable.shape[0] for name,variable in hydro.items() if hydro_dimensions[name]==("time","y","x")),1)
            times=[None] if timed==1 else [float(i) for i in range(timed)]
            warnings.append("hydrodynamic fields have no time coordinate; frame indices are used as relative display time")
        else:
            times = []
            if artifact_type == "terrain_preparation":
                warnings.append("terrain artifact has no water_depth/velocity frames; mesh only")
            else:
                warnings.append("no water_depth/water_level/velocity variables found; mesh only")

        mesh_path = out / "mesh.bin"
        node_count, triangle_count = _write_mesh(mesh_path, xs=corner_x, ys=corner_y, active=active, origin=origin, scale=xy_scale)
        frames: list[dict[str, Any]] = []
        extrema: dict[str, dict[str, float]] = {}
        variables = tuple(hydro)
        n_time = len(times) if hydro else 0
        if times and n_time and len(times) != n_time:
            raise Nc2WebError("time coordinate length does not match hydrodynamic fields")
        for index in range(n_time):
            arrays = []
            frame_ext: dict[str, dict[str, float]] = {}
            for name in variables:
                variable=hydro[name]
                raw=variable[index,:,:] if hydro_dimensions[name]==("time","y","x") else variable[:,:]
                values=_read_hydro_values(variable,raw,active)
                encoded=values.astype("<f4")
                arrays.append(encoded)
                finite = values[np.isfinite(values)]
                if finite.size:
                    frame_ext[name] = {"min": float(finite.min()), "max": float(finite.max())}
                    current = extrema.setdefault(name, {"min": float(finite.min()), "max": float(finite.max())})
                    current["min"] = min(current["min"], float(finite.min()))
                    current["max"] = max(current["max"], float(finite.max()))
            filename = f"frame_{index:03d}.bin"
            _write_frame(out / filename, arrays)
            frames.append({"index": index, "time": times[index], "file": filename, "extrema": frame_ext})

    status = "block" if blockers else ("warn" if warnings or not frames else "pass")
    manifest = Nc2WebManifest(
        status=status,
        epsg=target,
        source=str(src.resolve()),
        mesh="mesh.bin",
        nx=int(x_web.size),
        ny=int(y_web.size),
        origin=origin,
        dx=float(dx),
        dy=float(dy),
        node_count=node_count,
        triangle_count=triangle_count,
        times=tuple(times),
        variables=variables,
        frames=tuple(frames),
        extrema=extrema,
        warnings=tuple(dict.fromkeys(warnings)),
        blockers=tuple(dict.fromkeys(blockers)),
        artifact_type=None if artifact_type is None else str(artifact_type),
        xy_scale=float(xy_scale),
    )
    _write_json_noclobber(out / "manifest.json",manifest.to_dict())
    return manifest




def _fmt_num(value: float, digits: int) -> str:
    text = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        text = "0"
    return text if "." in text else f"{text}.0"


def _flow_dir_deg(u: float, v: float, *, still_dir: float = 180.0, eps: float = 1e-12) -> tuple[float, float]:
    speed = float(np.hypot(u, v))
    if not np.isfinite(speed) or speed <= eps:
        return 0.0, still_dir
    direction = float(np.degrees(np.arctan2(u, v)) % 360.0)
    return speed, direction


def _cell_id(row: int, col: int, nx: int) -> int:
    return int(row) * int(nx) + int(col) + 1


@dataclass(frozen=True)
class Nc2WebStepJsonReport:
    status: str
    epsg: str
    source: str
    output_dir: str
    nx: int
    ny: int
    times: tuple[float | None, ...]
    files: tuple[str, ...]
    cell_id_rule: str
    record_format: str
    still_direction_deg: float
    warnings: tuple[str, ...]
    wet_counts: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["times"] = list(self.times)
        payload["files"] = list(self.files)
        payload["warnings"] = list(self.warnings)
        payload["wet_counts"] = list(self.wet_counts)
        return payload


def convert_netcdf_to_step_json(
    source: str | Path,
    output_dir: str | Path,
    *,
    min_depth: float = 0.0,
    still_direction_deg: float = 180.0,
) -> Nc2WebStepJsonReport:
    """Write one JSON per timestep: all = ['id depth speed direction', ...]."""

    src = Path(source)
    out = Path(output_dir).resolve()
    _reject_native_triangular_artifact(src)
    _new_output_directory(out)
    warnings: list[str] = []
    with _open_dataset(src) as ds:
        if "water_depth" not in ds.variables:
            raise Nc2WebError("step JSON conversion requires water_depth")
        depth_var=ds.variables["water_depth"]
        depth_dims=tuple(depth_var.dimensions)
        if depth_dims not in (("y","x"),("time","y","x")):
            raise Nc2WebError("water_depth must use (time,y,x) or (y,x)")
        ny,nx=map(int,depth_var.shape[-2:])
        components=[]
        for candidates,label in ((("velocity_x","u"),"x"),(("velocity_y","v"),"y")):
            selected=next((name for name in candidates if name in ds.variables),None)
            if selected is None:
                raise Nc2WebError(f"step JSON requires {label}-velocity; missing values are never replaced by zero")
            variable=ds.variables[selected];dims=tuple(variable.dimensions)
            if dims not in (("y","x"),("time","y","x")) or variable.shape[-2:]!=(ny,nx):
                raise Nc2WebError(f"{selected} dimensions/shape do not align with water_depth")
            components.append((selected,variable,dims))
        if "time" in ds.variables:
            times=[float(item) for item in np.asarray(ds.variables["time"][:],dtype=np.float64).tolist()]
            frame_count=len(times)
        elif depth_dims==("time","y","x"):
            frame_count=int(depth_var.shape[0]);times=[float(i) for i in range(frame_count)]
            warnings.append("time variable missing; frame index is used as relative display time")
        else:
            frame_count=1;times=[None]
            warnings.append("water_depth has no time coordinate; a single step JSON was written")
        if depth_dims==("time","y","x") and depth_var.shape[0]!=frame_count:
            raise Nc2WebError("water_depth time length differs from time coordinate")
        for name,variable,dims in components:
            if dims==("time","y","x") and variable.shape[0]!=frame_count:
                raise Nc2WebError(f"{name} time length differs from water_depth/time coordinate")
        active=np.asarray(ds.variables["active_mask"][:],dtype=bool) if "active_mask" in ds.variables else None
        files:list[str]=[];wet_counts:list[int]=[]
        for index in range(frame_count):
            raw_h=depth_var[index,:,:] if depth_dims==("time","y","x") else depth_var[:,:]
            h=_read_hydro_values(depth_var,raw_h,active)
            vectors=[]
            for _name,variable,dims in components:
                raw=variable[index,:,:] if dims==("time","y","x") else variable[:,:]
                vectors.append(_read_hydro_values(variable,raw,active))
            uu,vv=vectors
            if h.shape!=(ny,nx) or uu.shape!=h.shape or vv.shape!=h.shape:
                raise Nc2WebError("streamed step arrays differ from the fixed y/x shape")
            valid=np.isfinite(h)&np.isfinite(uu)&np.isfinite(vv)
            wet=valid&(h>min_depth)
            if active is not None:wet=wet&active
            rows,cols=np.nonzero(wet);records=[]
            for row,col in zip(rows.tolist(),cols.tolist()):
                speed,direction=_flow_dir_deg(float(uu[row,col]),float(vv[row,col]),still_dir=still_direction_deg)
                records.append(f"{_cell_id(row,col,nx)}_{_fmt_num(float(h[row,col]),3)}_{_fmt_num(speed,2)}_{_fmt_num(direction,1)}")
            filename=f"step_{index:03d}.json"
            _write_json_noclobber(out/filename,{"all":records})
            files.append(filename);wet_counts.append(len(records))
    report = Nc2WebStepJsonReport(
        status="warn" if warnings else "pass",
        epsg=TARGET_WEB_EPSG,
        source=str(src.resolve()),
        output_dir=str(out.resolve()),
        nx=nx,
        ny=ny,
        times=tuple(times),
        files=tuple(files),
        cell_id_rule="1-based row-major: id = row * nx + col + 1",
        record_format="grid_id_depth_speed_direction joined by underscore",
        still_direction_deg=float(still_direction_deg),
        warnings=tuple(dict.fromkeys(warnings)),
        wet_counts=tuple(wet_counts),
    )
    _write_json_noclobber(out / "index.json",report.to_dict())
    return report


def convert_native_netcdf_to_web(source_request:str|Path,output_dir:str|Path,*,run_id:str)->dict[str,Any]:
    """Write a separate exact-node/face web profile from a strict native source request."""
    from .native_result_stream import open_validated_native_result_frames
    from .native_triangle_web import NativeTriangleWebWriter
    native=open_validated_native_result_frames(source_request)
    writer=NativeTriangleWebWriter(output_dir,native,run_id=run_id)
    for frame in native.iter_frames():writer.write_frame(frame)
    return writer.finish()



def write_socio_overlay_rasters(
    output_dir: str | Path,
    *,
    nx: int,
    ny: int,
    xmin: float,
    ymin: float,
    dx: float,
    dy: float,
    shp_path: str | Path = Path("data/grid_50m_social_economy/shp/grid_cells_socio.shp"),
) -> dict[str, Any]:
    """Bin socio centroids onto the model grid for visualization only."""
    from pyproj import Transformer
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    shp = Path(shp_path)
    # Read the full DBF directly; the previous sampled-audit result was unused.
    import struct
    raw = shp.with_suffix(".dbf").read_bytes()
    count = struct.unpack("<I", raw[4:8])[0]
    header_len = struct.unpack("<H", raw[8:10])[0]
    rec_len = struct.unpack("<H", raw[10:12])[0]
    names = []
    pos = 32
    while pos + 32 <= header_len - 1 and raw[pos] != 13:
        names.append(raw[pos:pos+11].split(b"\0", 1)[0].decode("ascii", "replace"))
        pos += 32
    lon_i, lat_i = names.index("lon"), names.index("lat")
    pop_i, farm_i = names.index("s_pop"), names.index("s_ar_mu")
    tr = Transformer.from_crs("EPSG:4326", "EPSG:4548", always_xy=True)
    pop = np.zeros((ny, nx), dtype="<f4")
    farm = np.zeros((ny, nx), dtype="<f4")
    n = 0
    for i in range(count):
        rec = raw[header_len + i * rec_len: header_len + (i + 1) * rec_len]
        off = 1
        vals = []
        widths = []
        pos = 32
        while pos + 32 <= header_len - 1 and raw[pos] != 13:
            widths.append(raw[pos+16])
            pos += 32
        for w in widths:
            vals.append(rec[off:off+w].decode("utf-8", "replace").strip())
            off += w
        try:
            lon, lat = float(vals[lon_i]), float(vals[lat_i])
            xx, yy = tr.transform(lon, lat)
            col = int(np.floor((xx - xmin) / dx))
            row = int(np.floor((yy - ymin) / dy))
            if 0 <= row < ny and 0 <= col < nx:
                pop[row, col] += float(vals[pop_i] or 0)
                farm[row, col] += float(vals[farm_i] or 0)
                n += 1
        except Exception:
            continue
    (out / "socio_pop.bin").write_bytes(pop.tobytes())
    (out / "socio_farm.bin").write_bytes(farm.tobytes())
    meta = {"cells_hit": n, "pop_max": float(pop.max()), "farm_max": float(farm.max()), "note": "visualization overlay from centroids; not official risk"}
    (out / "socio_overlay.json").write_text(__import__("json").dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def parse_netcdf_for_web(source: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Run both web parsers into sibling folders under output_dir."""

    root = Path(output_dir).resolve()
    _reject_native_triangular_artifact(Path(source))
    _new_output_directory(root)
    web = convert_netcdf_to_web(source, root / "nc2web")
    steps = convert_netcdf_to_step_json(source, root / "stepjson")
    try:
        socio = write_socio_overlay_rasters(
            root / "nc2web", nx=web.nx, ny=web.ny, xmin=web.origin[0], ymin=web.origin[1], dx=web.dx, dy=web.dy
        )
    except Exception as exc:
        socio = {"status": "warn", "error": str(exc)}
    summary = {
        "source": str(Path(source).resolve()),
        "output_dir": str(root.resolve()),
        "nc2web": {"dir": "nc2web", "status": web.status, "frames": len(web.frames), "epsg": web.epsg},
        "stepjson": {"dir": "stepjson", "status": steps.status, "files": len(steps.files), "epsg": steps.epsg},
        "socio": socio,
    }
    _write_json_noclobber(root / "parse_summary.json",summary)
    return summary

__all__ = ["MESH_MAGIC", "FRAME_MAGIC", "Nc2WebError", "Nc2WebManifest", "Nc2WebStepJsonReport", "TARGET_WEB_EPSG", "convert_netcdf_to_web", "convert_netcdf_to_step_json", "convert_native_netcdf_to_web", "parse_netcdf_for_web"]
