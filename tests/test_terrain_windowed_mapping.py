from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import weakref

import numpy as np
import pytest

pytest.importorskip("rasterio")
pytest.importorskip("pyproj")
pytest.importorskip("shapely")
import rasterio
from rasterio.transform import from_origin
from pydantic import ValidationError

import hydrodynamics.terrain_windowed_mapping as windowed
from hydrodynamics.dem_contract import DEMWindow, GeoTIFFDEMReader
from hydrodynamics.mesh_preparation import map_dem_to_triangular_mesh
from hydrodynamics.terrain_windowed_mapping import (
    WindowedTerrainMappingError,
    WindowedTriangleMappingConfig,
    map_triangular_mesh_from_windows,
    write_windowed_mapping_report,
)
from hydrodynamics.triangular_mesh import TriangularMesh


class _SpyGeoTIFFReader(GeoTIFFDEMReader):
    def __init__(self):
        self.read_dataset_calls = 0
        self.raw_windows = []
        self.gdal_cache_bytes = None
        self.previous_tile = None
        self.multiple_tiles_alive = False

    def read_dataset(self, source):
        self.read_dataset_calls += 1
        raise AssertionError("windowed triangle mapping must not call read_dataset")

    def _read_open_window(self, dataset, metadata, window, rasterio_module):
        if self.previous_tile is not None and self.previous_tile() is not None:
            self.multiple_tiles_alive = True
        self.raw_windows.append(window)
        self.gdal_cache_bytes = rasterio_module.env.getenv().get("GDAL_CACHEMAX")
        tile=super()._read_open_window(dataset, metadata, window, rasterio_module)
        self.previous_tile=weakref.ref(tile)
        return tile


def _write_raster(path: Path, values: np.ndarray, *, x0: float = 0.0, ytop: float | None = None) -> Path:
    rows, cols = values.shape
    if ytop is None:
        ytop = float(rows)
    with rasterio.open(path, "w", driver="GTiff", width=cols, height=rows, count=1,
                       dtype="float32", crs="EPSG:4548", transform=from_origin(x0, ytop, 1.0, 1.0),
                       nodata=-9999.0) as dataset:
        dataset.write(values.astype("float32"), 1)
        dataset.set_band_unit(1, "metre")
        dataset.update_tags(AREA_OR_POINT="Point")
    return path


def _square_mesh(reader: GeoTIFFDEMReader, raster: Path, *, x0=0.0, y0=0.0, size=4.0, face_id=(81, 82)):
    crs = reader.read_metadata(raster).crs
    nodes = ((x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size))
    return TriangularMesh(nodes, ((0, 1, 2), (0, 2, 3)), crs, mesh_id="m2-a-small-v1", face_id=face_id)


def _config(pixel_count, *, rows=2, cols=2, checks=10000, policy="diagnostic_only", minimum=None):
    return WindowedTriangleMappingConfig(
        max_source_pixels=pixel_count,
        tile_rows=rows,
        tile_cols=cols,
        max_candidate_overlap_checks=checks,
        coverage_policy=policy,
        minimum_valid_coverage=minimum,
    )


def _shapely_reference(dataset, mesh):
    from shapely.geometry import Polygon, box
    a,b,c,d,e,f=dataset.metadata.transform[:6]
    rows=[]
    for face_id,nodes in zip(mesh.face_id,mesh.face_nodes):
        triangle=Polygon([mesh.node_xy[node] for node in nodes])
        covered=valid=weighted=nodata=nonfinite=0.0
        for row in range(dataset.metadata.height):
            for col in range(dataset.metadata.width):
                x0=a*col+c;y1=e*row+f;x1=x0+a;y0=y1+e
                pixel=box(min(x0,x1),min(y0,y1),max(x0,x1),max(y0,y1))
                area=triangle.intersection(pixel).area
                if area<=0:continue
                covered+=area
                if dataset.nonfinite_mask[row][col]:nonfinite+=area
                elif dataset.nodata_mask[row][col]:nodata+=area
                elif dataset.valid_mask[row][col]:
                    valid+=area;weighted+=area*float(dataset.elevation[row][col])
        rows.append({"face_id":int(face_id),"covered_area_m2":covered,"valid_area_m2":valid,
                     "nodata_area_m2":nodata,"nonfinite_area_m2":nonfinite,
                     "elevation_m":weighted/valid if valid>0 else None})
    return rows


def test_cross_tile_triangle_overlap_matches_full_read_reference(tmp_path):
    values=np.arange(16,dtype=np.float32).reshape(4,4)
    values[0,0]=-9999.0
    values[1,1]=np.inf
    raster=_write_raster(tmp_path/"small-source.tif",values)
    reference_reader=GeoTIFFDEMReader()
    mesh=_square_mesh(reference_reader,raster)
    raw_reference=reference_reader.read_dataset(raster)
    expected=map_dem_to_triangular_mesh(raw_reference,mesh,maximum_overlap_checks=1000)
    independent=_shapely_reference(raw_reference,mesh)
    reader=_SpyGeoTIFFReader()
    actual,report=map_triangular_mesh_from_windows(
        reader,raster,mesh,source_sha256=hashlib.sha256(raster.read_bytes()).hexdigest(),
        config=_config(16,rows=2,cols=2),
    )
    assert reader.read_dataset_calls==0
    assert reader.multiple_tiles_alive is False
    assert reader.gdal_cache_bytes==64*1024*1024
    assert [(window.row_off,window.col_off) for window in reader.raw_windows]==[(0,0),(0,2),(2,0),(2,2)]
    assert actual.elevation_m==pytest.approx(expected.elevation_m,abs=1e-8)
    assert actual.covered_area_m2==pytest.approx(expected.covered_area_m2,abs=1e-8)
    assert actual.valid_area_m2==pytest.approx(expected.valid_area_m2,abs=1e-8)
    assert actual.coverage_ratio==pytest.approx(expected.coverage_ratio,abs=1e-10)
    for row,oracle in zip(report["faces"],independent):
        assert row["face_id"]==oracle["face_id"]
        assert row["covered_area_m2"]==pytest.approx(oracle["covered_area_m2"],abs=1e-8,rel=1e-10)
        assert row["valid_area_m2"]==pytest.approx(oracle["valid_area_m2"],abs=1e-8,rel=1e-10)
        assert row["nodata_area_m2"]==pytest.approx(oracle["nodata_area_m2"],abs=1e-8,rel=1e-10)
        assert row["nonfinite_area_m2"]==pytest.approx(oracle["nonfinite_area_m2"],abs=1e-8,rel=1e-10)
        assert row["elevation_m"]==pytest.approx(oracle["elevation_m"],abs=1e-8,rel=1e-10)
    assert report["status"]=="diagnostic_only"
    assert report["preparation_only"] is True and report["simulation_result"] is False
    assert report["production_approved"] is False
    assert report["source_file_identity"]["before"]==report["source_file_identity"]["after"]
    assert report["stats"]["tiles_selected"]==4
    assert report["stats"]["source_pixels_read"]==16
    assert report["stats"]["candidate_overlap_checks"]==24
    assert report["source_area_or_point"]=="Point"
    assert all(row["outside_raster_area_m2"]==pytest.approx(0.0) for row in report["faces"])
    assert sum(row["nodata_area_m2"] for row in report["faces"])==pytest.approx(1.0)
    assert sum(row["nonfinite_area_m2"] for row in report["faces"])==pytest.approx(1.0)


def test_window_size_and_face_order_keep_face_results_within_frozen_tolerance(tmp_path):
    values=np.arange(16,dtype=np.float32).reshape(4,4)
    raster=_write_raster(tmp_path/"tile-order.tif",values)
    reader=GeoTIFFDEMReader();mesh=_square_mesh(reader,raster)
    digest=hashlib.sha256(raster.read_bytes()).hexdigest()
    small,small_report=map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256=digest,config=_config(16,rows=1,cols=2))
    wide,wide_report=map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256=digest,config=_config(16,rows=4,cols=4))
    reversed_mesh=TriangularMesh(mesh.node_xy,tuple(reversed(mesh.face_nodes)),mesh.coordinate_system,
        mesh_id=mesh.mesh_id,face_id=tuple(reversed(mesh.face_id)))
    reversed_field,_=map_triangular_mesh_from_windows(reader,raster,reversed_mesh,source_sha256=digest,config=_config(16,rows=2,cols=2))
    by_id={int(face_id):index for index,face_id in enumerate(reversed_mesh.face_id)}
    for i,face_id in enumerate(mesh.face_id):
        j=by_id[int(face_id)]
        assert small.elevation_m[i]==pytest.approx(wide.elevation_m[i],abs=1e-8,rel=1e-10)
        assert small.valid_area_m2[i]==pytest.approx(wide.valid_area_m2[i],abs=1e-8,rel=1e-10)
        assert small.elevation_m[i]==pytest.approx(reversed_field.elevation_m[j],abs=1e-8,rel=1e-10)
    assert small_report["stats"]["candidate_overlap_checks"]==small_report["stats"]["clip_calls"]
    assert wide_report["stats"]["candidate_overlap_checks"]==wide_report["stats"]["clip_calls"]
    assert small_report["stats"]["candidate_overlap_checks"]<=small_report["resource_limits"]["max_candidate_overlap_checks"]
    assert wide_report["stats"]["candidate_overlap_checks"]<=wide_report["resource_limits"]["max_candidate_overlap_checks"]


def test_tiles_without_triangle_overlap_are_skipped_before_raw_window_read(tmp_path):
    values=np.arange(64,dtype=np.float32).reshape(8,8)
    raster=_write_raster(tmp_path/"sparse-domain.tif",values,ytop=8.0)
    reader=_SpyGeoTIFFReader();crs=reader.read_metadata(raster).crs
    mesh=TriangularMesh(((0,6),(2,6),(2,8),(0,8)),((0,1,2),(0,2,3)),crs,mesh_id="top-left-only",face_id=(901,902))
    _,report=map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256=hashlib.sha256(raster.read_bytes()).hexdigest(),config=_config(64,rows=2,cols=2))
    assert [(window.row_off,window.col_off) for window in reader.raw_windows]==[(0,0)]
    assert report["stats"]["tiles_enumerated"]==16
    assert report["stats"]["tiles_selected"]==1
    assert report["stats"]["tiles_skipped_no_face_overlap"]==15
    assert report["stats"]["source_pixels_read"]==4


def test_large_projected_coordinates_preserve_small_overlap_area(tmp_path):
    origin_x=500000.0;origin_ytop=4400002.0
    raster=_write_raster(tmp_path/"large-coordinates.tif",np.asarray([[0.0,2.0],[4.0,6.0]],dtype=np.float32),x0=origin_x,ytop=origin_ytop)
    reader=GeoTIFFDEMReader();mesh=_square_mesh(reader,raster,x0=origin_x,y0=origin_ytop-2.0,size=2.0)
    field,report=map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256=hashlib.sha256(raster.read_bytes()).hexdigest(),config=_config(4,rows=1,cols=1))
    oracle=_shapely_reference(reader.read_dataset(raster),mesh)
    assert sum(mesh.face_area)==pytest.approx(4.0)
    assert sum(field.covered_area_m2)==pytest.approx(4.0,abs=1e-8)
    assert sum(field.valid_area_m2)==pytest.approx(4.0,abs=1e-8)
    assert field.elevation_m==pytest.approx(tuple(row["elevation_m"] for row in oracle),abs=1e-8)
    assert report["stats"]["candidate_overlap_checks"]==report["stats"]["clip_calls"]


def test_mesh_tolerance_is_fingerprinted_and_ignored_overlap_is_not_outside(tmp_path):
    raster=_write_raster(tmp_path/"tolerance-sliver.tif",np.ones((2,2),dtype=np.float32),ytop=2.0)
    reader=GeoTIFFDEMReader();crs=reader.read_metadata(raster).crs
    nodes=((-1.95,0.0),(0.05,0.0),(0.05,1.2))
    strict=TriangularMesh(nodes,((0,1,2),),crs,mesh_id="tolerance-case",face_id=(707,),geometry_tolerance_m=1e-8)
    permissive=TriangularMesh(nodes,((0,1,2),),crs,mesh_id="tolerance-case",face_id=(707,),geometry_tolerance_m=0.2)
    assert windowed.mesh_fingerprint_sha256(strict)!=windowed.mesh_fingerprint_sha256(permissive)
    with pytest.raises(WindowedTerrainMappingError,match="ignored in-raster overlap") as error:
        map_triangular_mesh_from_windows(reader,raster,permissive,source_sha256=hashlib.sha256(raster.read_bytes()).hexdigest(),config=_config(4,rows=1,cols=1))
    row=error.value.diagnostics["faces"][0]
    assert row["ignored_in_raster_area_m2"]>1e-8
    assert row["outside_raster_area_m2"]==pytest.approx(
        row["face_area_m2"]-row["covered_area_m2"]-row["ignored_in_raster_area_m2"],abs=1e-10)


def test_tiny_ignored_overlap_that_exceeds_elevation_error_is_rejected(tmp_path):
    raster=_write_raster(tmp_path/"elevation-error-bound.tif",np.asarray([[0.0,1e20]],dtype=np.float32),x0=-1.0,ytop=1.0)
    reader=GeoTIFFDEMReader();crs=reader.read_metadata(raster).crs
    mesh=TriangularMesh(((-2.0,0.0),(5e-9,0.0),(5e-9,1.0)),((0,1,2),),crs,
        mesh_id="tiny-weighted-sliver",face_id=(708,),geometry_tolerance_m=1e-4)
    with pytest.raises(WindowedTerrainMappingError,match="ignored overlap elevation effect") as error:
        map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256=hashlib.sha256(raster.read_bytes()).hexdigest(),config=_config(2,rows=1,cols=1))
    row=error.value.diagnostics["faces"][0]
    assert row["ignored_in_raster_area_m2"]<1e-8
    assert row["ignored_elevation_delta_m"]>row["ignored_elevation_tolerance_m"]


def test_source_pixel_cap_fails_before_window_values_are_read(tmp_path):
    raster=_write_raster(tmp_path/"source-cap.tif",np.ones((4,4),dtype=np.float32))
    reader=_SpyGeoTIFFReader();mesh=_square_mesh(reader,raster)
    with pytest.raises(WindowedTerrainMappingError,match="before reading raster values"):
        map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256="a"*64,config=_config(15))
    assert reader.raw_windows==[]
    assert reader.read_dataset_calls==0


def test_early_or_overlapping_window_stream_is_rejected(tmp_path):
    raster=_write_raster(tmp_path/"bad-window-stream.tif",np.ones((4,4),dtype=np.float32))
    reader=GeoTIFFDEMReader();mesh=_square_mesh(reader,raster);metadata=reader.read_metadata(raster)
    config=_config(16,rows=2,cols=2)
    def early(source,*,window_width,window_height,gdal_cache_mib,include_window):
        window=DEMWindow(0,0,2,2)
        if include_window(metadata,window):yield window,reader.read_window(source,window)
    reader.iter_windows_selected=early
    with pytest.raises(WindowedTerrainMappingError,match="ended early"):
        map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256="8"*64,config=config)

    reader=GeoTIFFDEMReader();metadata=reader.read_metadata(raster);mesh=_square_mesh(reader,raster)
    def overlapping(source,*,window_width,window_height,gdal_cache_mib,include_window):
        first=DEMWindow(0,0,2,2)
        if include_window(metadata,first):yield first,reader.read_window(source,first)
        second=DEMWindow(0,1,2,2)
        if include_window(metadata,second):yield second,reader.read_window(source,second)
    reader.iter_windows_selected=overlapping
    with pytest.raises(WindowedTerrainMappingError,match="complete nonoverlapping row-major"):
        map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256="9"*64,config=config)

    reader=_SpyGeoTIFFReader();metadata=reader.read_metadata(raster);mesh=_square_mesh(reader,raster)
    def oversized(source,*,window_width,window_height,gdal_cache_mib,include_window):
        window=DEMWindow(0,0,4,4)
        if include_window(metadata,window):yield window,reader.read_window(source,window)
    reader.iter_windows_selected=oversized
    with pytest.raises(WindowedTerrainMappingError,match="exceeds configured tile dimensions before raw read"):
        map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256="7"*64,config=config)
    assert reader.raw_windows==[]


def test_source_file_change_during_window_mapping_aborts_result(tmp_path):
    raster=_write_raster(tmp_path/"source-change.tif",np.ones((4,4),dtype=np.float32))
    reader=GeoTIFFDEMReader();mesh=_square_mesh(reader,raster)
    original=reader.iter_windows_selected
    def changing(source,*,window_width,window_height,gdal_cache_mib,include_window):
        count=0
        for window,tile in original(source,window_width=window_width,window_height=window_height,
                                    gdal_cache_mib=gdal_cache_mib,include_window=include_window):
            yield window,tile
            count+=1
            if count==1:
                stat=Path(source).stat()
                os.utime(source,ns=(stat.st_atime_ns,stat.st_mtime_ns+1_000_000_000))
    reader.iter_windows_selected=changing
    with pytest.raises(WindowedTerrainMappingError,match="source file changed during window mapping"):
        map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256=hashlib.sha256(raster.read_bytes()).hexdigest(),
                                         config=_config(16,rows=2,cols=2))


def test_overlap_budget_rejects_before_the_over_budget_clip(tmp_path,monkeypatch):
    raster=_write_raster(tmp_path/"overlap-budget.tif",np.ones((4,4),dtype=np.float32))
    reader=_SpyGeoTIFFReader();mesh=_square_mesh(reader,raster)
    original=windowed._clip_convex;calls=0
    def counted(subject,clip):
        nonlocal calls
        calls+=1
        return original(subject,clip)
    monkeypatch.setattr(windowed,"_clip_convex",counted)
    with pytest.raises(WindowedTerrainMappingError,match="max_candidate_overlap_checks") as error:
        map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256="b"*64,config=_config(16,checks=1))
    assert calls==1
    assert error.value.diagnostics["stats"]["candidate_overlap_checks"]==2
    assert error.value.diagnostics["source_pixel_row"]==0


def test_zero_valid_face_fails_with_per_face_diagnostic(tmp_path):
    raster=_write_raster(tmp_path/"all-nodata.tif",np.full((4,4),-9999.0,dtype=np.float32))
    reader=GeoTIFFDEMReader();mesh=_square_mesh(reader,raster)
    with pytest.raises(WindowedTerrainMappingError,match="zero valid DEM overlap") as error:
        map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256="c"*64,config=_config(16))
    assert len(error.value.diagnostics["faces"])==2
    assert all(row["valid_area_m2"]==0.0 and row["nodata_area_m2"]>0.0 for row in error.value.diagnostics["faces"])


def test_outside_raster_and_unapproved_coverage_are_not_marked_ready(tmp_path):
    raster=_write_raster(tmp_path/"partial-source.tif",np.ones((2,2),dtype=np.float32),ytop=2.0)
    reader=GeoTIFFDEMReader();crs=reader.read_metadata(raster).crs
    mesh=TriangularMesh(((-1,0),(1,0),(1,2)),((0,1,2),),crs,mesh_id="partial-outside",face_id=(1001,))
    _,report=map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256="d"*64,config=_config(4,rows=1,cols=1))
    row=report["faces"][0]
    assert 0.0<row["outside_raster_area_m2"]<mesh.face_area[0]
    assert 0.0<row["coverage_ratio"]<1.0
    assert report["status"]=="diagnostic_only"


def test_strict_coverage_requires_source_approval_evidence():
    with pytest.raises(ValueError,match="source-approved evidence path/SHA-256"):
        WindowedTriangleMappingConfig(max_source_pixels=16,max_candidate_overlap_checks=100,
            coverage_policy="minimum_valid_coverage",minimum_valid_coverage=0.9)
    with pytest.raises(ValueError,match="source_vertical_unit_declaration requires"):
        WindowedTriangleMappingConfig(max_source_pixels=16,max_candidate_overlap_checks=100,
            coverage_policy="diagnostic_only",source_vertical_unit_declaration="m")


def test_coverage_evidence_hash_is_checked_before_window_reads(tmp_path):
    raster=_write_raster(tmp_path/"evidence-bound.tif",np.ones((4,4),dtype=np.float32))
    reader=_SpyGeoTIFFReader();mesh=_square_mesh(reader,raster)
    evidence=tmp_path/"coverage-policy.json";evidence.write_text('{"policy":"synthetic-test-only"}\n',encoding="utf-8")
    evidence_sha=hashlib.sha256(evidence.read_bytes()).hexdigest()
    config=WindowedTriangleMappingConfig(max_source_pixels=16,max_candidate_overlap_checks=1000,
        coverage_policy="minimum_valid_coverage",minimum_valid_coverage=0.5,
        coverage_policy_evidence_path=evidence.name,coverage_policy_evidence_sha256=evidence_sha)
    _,report=map_triangular_mesh_from_windows(reader,raster,mesh,source_sha256=hashlib.sha256(raster.read_bytes()).hexdigest(),
        config=config,config_base=tmp_path)
    assert report["status"]=="coverage_policy_passed_preparation_only"
    assert report["source_evidence"]["coverage_policy"]["sha256"]==evidence_sha
    bad=config.model_copy(update={"coverage_policy_evidence_sha256":"0"*64})
    reader2=_SpyGeoTIFFReader()
    with pytest.raises(WindowedTerrainMappingError,match="evidence SHA-256 differs"):
        map_triangular_mesh_from_windows(reader2,raster,mesh,source_sha256="1"*64,config=bad,config_base=tmp_path)
    assert reader2.raw_windows==[]


def test_crs_mismatch_and_rotated_affine_are_rejected_without_transform(tmp_path):
    raster=_write_raster(tmp_path/"strict-crs.tif",np.ones((4,4),dtype=np.float32))
    reader=GeoTIFFDEMReader();mesh=_square_mesh(reader,raster)
    authority_alias=TriangularMesh(mesh.node_xy,mesh.face_nodes,"EPSG:4548",mesh_id=mesh.mesh_id,face_id=mesh.face_id)
    with pytest.raises(WindowedTerrainMappingError,match="source CRS text differs"):
        map_triangular_mesh_from_windows(reader,raster,authority_alias,source_sha256="e"*64,config=_config(16))

    from affine import Affine
    rotated=tmp_path/"rotated.tif"
    with rasterio.open(rotated,"w",driver="GTiff",width=4,height=4,count=1,dtype="float32",
                       crs="EPSG:4548",transform=Affine(1.0,0.1,0.0,0.1,-1.0,4.0),nodata=-9999.0) as ds:
        ds.write(np.ones((4,4),dtype="float32"),1);ds.set_band_unit(1,"metre")
    rotated_reader=GeoTIFFDEMReader();rotated_mesh=_square_mesh(rotated_reader,rotated)
    with pytest.raises(WindowedTerrainMappingError,match="north-up"):
        map_triangular_mesh_from_windows(rotated_reader,rotated,rotated_mesh,source_sha256="f"*64,config=_config(16))


def test_config_schema_example_and_report_are_prepare_only_and_no_clobber(tmp_path):
    example_dir=Path(__file__).parents[1]/"examples"/"triangular_mesh"
    example=json.loads((example_dir/"windowed_mapping_config.example.json").read_text(encoding="utf-8"))
    schema=json.loads((example_dir/"windowed_mapping_config.schema.json").read_text(encoding="utf-8"))
    config=WindowedTriangleMappingConfig.model_validate(example)
    assert config.max_source_pixels==1_000_000_000
    assert config.tile_rows*config.tile_cols==65_536
    assert config.max_inflight_tiles==1 and config.gdal_cache_mib==64
    assert schema["additionalProperties"] is False
    assert set(schema["required"]).issubset(example)
    assert set(example).issubset(schema["properties"])
    assert schema["properties"]["format"]["const"]==example["format"]
    assert schema["properties"]["format"]["const"]==example["format"]
    assert set(schema["required"]).issubset(example)
    report={"format":"triangle-terrain-windowed-report-v1","status":"diagnostic_only",
        "preparation_only":False,"simulation_result":True,"production_approved":True}
    target=tmp_path/"reports"/"mapping.json";target.parent.mkdir()
    write_windowed_mapping_report(target,report)
    saved=json.loads(target.read_text(encoding="utf-8"))
    assert saved["preparation_only"] is True and saved["simulation_result"] is False
    assert saved["production_approved"] is False
    with pytest.raises(WindowedTerrainMappingError,match="already exists"):
        write_windowed_mapping_report(target,report)


def test_json_schema_and_strict_config_loader_have_matching_acceptance_set():
    jsonschema=pytest.importorskip("jsonschema")
    example_dir=Path(__file__).parents[1]/"examples"/"triangular_mesh"
    schema=json.loads((example_dir/"windowed_mapping_config.schema.json").read_text(encoding="utf-8"))
    example=json.loads((example_dir/"windowed_mapping_config.example.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    validator=jsonschema.Draft202012Validator(schema)

    def accepted(payload):
        try:
            WindowedTriangleMappingConfig.model_validate(payload)
            runtime=True
        except ValidationError:
            runtime=False
        return runtime,validator.is_valid(payload)

    assert accepted(example)==(True,True)
    assert accepted({**example,"max_candidate_overlap_checks":1.0})==(True,True)
    strict={**example,"coverage_policy":"minimum_valid_coverage","minimum_valid_coverage":0.9,
        "coverage_policy_evidence_path":"evidence/coverage.json","coverage_policy_evidence_sha256":"a"*64}
    assert accepted(strict)==(True,True)
    invalid=[
        {**example,"max_source_pixels":True},
        {**example,"tile_rows":True},
        {**example,"tile_cols":True},
        {**example,"max_inflight_tiles":True},
        {**example,"gdal_cache_mib":True},
        {**example,"max_faces":True},
        {**example,"max_nodes":True},
        {**example,"max_candidate_overlap_checks":True},
        {**example,"source_vertical_unit_evidence_path":"evidence/unit.json","source_vertical_unit_evidence_sha256":"b"*64},
        {**example,"source_vertical_datum_evidence_path":"evidence/datum.json","source_vertical_datum_evidence_sha256":"c"*64},
        {**example,"source_vertical_unit_declaration":"m","source_vertical_unit_evidence_path":"C:/outside/unit.json","source_vertical_unit_evidence_sha256":"d"*64},
        {**example,"source_vertical_unit_declaration":"m","source_vertical_unit_evidence_path":"\\outside\\unit.json","source_vertical_unit_evidence_sha256":"f"*64},
        {**example,"source_vertical_unit_declaration":"m","source_vertical_unit_evidence_path":"../outside/unit.json","source_vertical_unit_evidence_sha256":"e"*64},
        {**example,"unknown_route_flag":True},
        {**strict,"minimum_valid_coverage":True},
        {**example,"max_candidate_overlap_checks":1.5},
    ]
    assert all(accepted(payload)==(False,False) for payload in invalid)


def test_report_publish_failure_leaves_no_partial_success_marker(tmp_path,monkeypatch):
    target=tmp_path/"reports"/"failure.json";target.parent.mkdir()
    def fail_link(*args,**kwargs):
        raise OSError("injected publish failure")
    monkeypatch.setattr(windowed.os,"link",fail_link)
    with pytest.raises(WindowedTerrainMappingError,match="injected publish failure"):
        write_windowed_mapping_report(target,{"status":"diagnostic_only"})
    assert not target.exists()
    assert list(target.parent.iterdir())==[]
