from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest

from hydrodynamics import FRAME_MAGIC, MESH_MAGIC, Nc2WebError, convert_netcdf_to_step_json, convert_netcdf_to_web, parse_netcdf_for_web
from hydrodynamics.netcdf_writer import OutputDependencyError


def _write_nc(path: Path, *, with_time: bool = True, crs: str = "EPSG:4548", active: bool = True) -> Path:
    netCDF4 = pytest.importorskip("netCDF4")
    ny, nx = 2, 3
    xmin, ymin, dx, dy = 474150.0, 4299600.0, 50.0, 50.0
    with netCDF4.Dataset(str(path), "w") as ds:
        if with_time:
            ds.createDimension("time", 2)
        ds.createDimension("y", ny)
        ds.createDimension("x", nx)
        xv = ds.createVariable("x", "f8", ("x",))
        yv = ds.createVariable("y", "f8", ("y",))
        xv[:] = xmin + (np.arange(nx) + 0.5) * dx
        yv[:] = ymin + (np.arange(ny) + 0.5) * dy
        ds.coordinate_system = crs
        ds.artifact_type = "solver_result" if with_time else "terrain_preparation"
        ds.origin = f"{xmin},{ymin}"
        ds.grid_spacing = f"{dx},{dy}"
        am = ds.createVariable("active_mask", "i1", ("y", "x"))
        mask = np.ones((ny, nx), dtype=np.int8)
        if active:
            mask[1, 2] = 0
        am[:] = mask
        if with_time:
            tv = ds.createVariable("time", "f8", ("time",))
            tv[:] = [0.0, 3600.0]
            depth = ds.createVariable("water_depth", "f8", ("time", "y", "x"))
            u = ds.createVariable("velocity_x", "f8", ("time", "y", "x"))
            v = ds.createVariable("velocity_y", "f8", ("time", "y", "x"))
            depth[0] = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
            depth[1] = [[1.0, 1.1, 1.2], [1.3, 1.4, 1.5]]
            u[:] = 0.2
            v[:] = -0.1
    return path


def test_nc2web_writes_epsg4548_mesh_and_frames(tmp_path):
    source = _write_nc(tmp_path / "result.nc")
    out = tmp_path / "web"
    manifest = convert_netcdf_to_web(source, out)
    assert manifest.epsg == "EPSG:4548"
    assert manifest.status == "pass"
    assert manifest.nx == 3 and manifest.ny == 2
    assert manifest.origin == (474150.0, 4299600.0)
    assert manifest.node_count == 4 * 3
    assert manifest.triangle_count == 2 * (2 * 3 - 1)
    payload = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert payload["epsg"] == "EPSG:4548"
    mesh = (out / "mesh.bin").read_bytes()
    assert mesh.startswith(MESH_MAGIC)
    n_nodes, n_tri, xmin, ymin, scale = struct.unpack_from("<IIfff", mesh, len(MESH_MAGIC))
    assert n_nodes == 12 and n_tri == 10
    assert xmin == 474150.0 and ymin == 4299600.0
    frame = (out / "frame_000.bin").read_bytes()
    assert frame.startswith(FRAME_MAGIC)
    ny, nx, n_vars = struct.unpack_from("<III", frame, len(FRAME_MAGIC))
    assert (ny, nx, n_vars) == (2, 3, 3)
    depth = np.frombuffer(frame, dtype="<f4", offset=len(FRAME_MAGIC) + 12, count=6).reshape(2, 3)
    assert depth[0, 0] == pytest.approx(0.1)
    assert np.isnan(depth[1, 2])


def test_terrain_nc_writes_mesh_without_inventing_depth(tmp_path):
    source = _write_nc(tmp_path / "terrain.nc", with_time=False)
    manifest = convert_netcdf_to_web(source, tmp_path / "web")
    assert manifest.epsg == "EPSG:4548"
    assert manifest.status == "warn"
    assert manifest.frames == ()
    assert any("mesh only" in item for item in manifest.warnings)
    assert (tmp_path / "web" / "mesh.bin").is_file()
    assert list((tmp_path / "web").glob("frame_*.bin")) == []


def test_wrong_target_epsg_is_rejected(tmp_path):
    source = _write_nc(tmp_path / "result.nc")
    with pytest.raises(Nc2WebError, match="EPSG:4548"):
        convert_netcdf_to_web(source, tmp_path / "web", target_epsg="EPSG:4326")


def test_step_json_one_file_per_step(tmp_path):
    source = _write_nc(tmp_path / "result.nc")
    out = tmp_path / "stepjson"
    report = convert_netcdf_to_step_json(source, out)
    assert report.status == "pass"
    assert (out / "step_000.json").is_file()
    assert (out / "step_001.json").is_file()
    payload = json.loads((out / "step_000.json").read_text(encoding="utf-8"))
    assert list(payload) == ["all"]
    assert payload["all"][0].split("_")[0] == "1"
    ids = [int(item.split("_")[0]) for item in payload["all"]]
    assert 6 not in ids
    depth = float(payload["all"][0].split("_")[1])
    assert depth == pytest.approx(0.1, abs=1e-6)
    still = [item for item in payload["all"] if item.endswith("_180.0")]
    moving = json.loads((out / "step_000.json").read_text(encoding="utf-8"))["all"]
    assert any(len(item.split("_")) == 4 for item in moving)


def test_parse_netcdf_for_web_uses_sibling_folders(tmp_path):
    source = _write_nc(tmp_path / "result.nc")
    bundle = tmp_path / "bundle"
    summary = parse_netcdf_for_web(source, bundle)
    assert (bundle / "nc2web" / "manifest.json").is_file()
    assert (bundle / "stepjson" / "step_000.json").is_file()
    assert (bundle / "parse_summary.json").is_file()
    assert summary["nc2web"]["epsg"] == "EPSG:4548"
