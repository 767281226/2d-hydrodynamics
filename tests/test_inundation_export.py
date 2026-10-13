import numpy as np
from hydrodynamics.inundation_export import write_wet_grid_shapefile

def test_write_wet_grid_shapefile(tmp_path):
    wet = np.array([[False, True], [True, False]])
    depth = np.array([[0.0, 1.25], [0.5, 0.0]])
    path = write_wet_grid_shapefile(
        tmp_path / "wet.shp",
        wet_mask=wet,
        max_depth=depth,
        xmin=474150.0,
        ymin=4299600.0,
        dx=50.0,
        dy=50.0,
    )
    assert path.is_file()
    assert path.with_suffix(".dbf").is_file()
    assert path.with_suffix(".prj").is_file()
    dbf = path.with_suffix(".dbf").read_bytes()
    assert dbf[4:8] == (2).to_bytes(4, "little")

def test_write_grid_shapefile_includes_dry_active_cells(tmp_path):
    from hydrodynamics.inundation_export import write_grid_shapefile

    mask = np.array([[True, True], [True, False]])
    depth = np.array([[0.0, 1.25], [0.5, 0.0]])
    elev = np.array([[2.0, 1.0], [1.5, 9.0]])
    zone = np.array([["wenanwa", "qingbei"], ["qingnan", "none"]])
    path = write_grid_shapefile(
        tmp_path / "grid.shp",
        mask=mask,
        max_depth=depth,
        elevation=elev,
        zone=zone,
        xmin=474150.0,
        ymin=4299600.0,
        dx=50.0,
        dy=50.0,
    )
    dbf = path.with_suffix(".dbf").read_bytes()
    assert dbf[4:8] == (3).to_bytes(4, "little")
    assert b"wenanwa" in dbf
    assert b"qingbei" in dbf

def test_write_grid_shapefile_reprojects_to_epsg4490(tmp_path):
    import struct
    from hydrodynamics.inundation_export import write_grid_shapefile

    mask = np.array([[True]])
    path = write_grid_shapefile(
        tmp_path / "grid4490.shp",
        mask=mask,
        xmin=474150.0,
        ymin=4299600.0,
        dx=50.0,
        dy=50.0,
        crs="EPSG:4548",
        output_crs="EPSG:4490",
    )
    bbox = struct.unpack("<4d", path.read_bytes()[36:68])
    assert 116.0 < bbox[0] < 118.0
    assert 38.0 < bbox[1] < 40.0
    prj = path.with_suffix(".prj").read_text(encoding="utf-8")
    assert "CGCS2000" in prj
    assert "Gauss" not in prj


def test_shp_length_and_index_are_readable_by_independent_reader(tmp_path):
    import pytest
    import struct
    shapefile=pytest.importorskip('shapefile')
    wet=np.array([[True,False],[True,True]])
    depth=np.array([[1.,0.],[2.,3.]])
    path=write_wet_grid_shapefile(tmp_path/'readable.shp',wet_mask=wet,max_depth=depth,
                                xmin=474150,ymin=4299600,dx=50,dy=50)
    raw=path.read_bytes()
    assert struct.unpack('>I',raw[24:28])[0]*2==len(raw)
    with shapefile.Reader(str(path)) as reader:
        assert len(reader)==3 and len(reader.shapes())==3
        assert len(reader.shape(2).points)==5
        assert reader.record(2).max_h==3.0
        assert list(reader.shape(2).bbox)==[474200.,4299650.,474250.,4299700.]


def test_empty_inundation_layer_is_readable(tmp_path):
    import pytest
    shapefile=pytest.importorskip('shapefile')
    path=write_wet_grid_shapefile(tmp_path/'dry.shp',wet_mask=np.zeros((2,2),bool),max_depth=np.zeros((2,2)),
                                xmin=474150,ymin=4299600,dx=50,dy=50,output_crs='EPSG:4490')
    with shapefile.Reader(str(path)) as reader:
        assert len(reader)==0 and reader.shapes()==[]
