import pytest
import numpy as np
from hydrodynamics import NetCDFWriter, OutputDependencyError, OutputError, OutputField, SimulationOutput

def test_netcdf_writer_dependency_or_fixture(tmp_path):
    f=OutputField("water_depth",np.ones((2,2,2)),2,2,30,40,0,0,times=[0,1],valid_mask=[[True,False],[True,True]])
    try:
        p=NetCDFWriter.write(SimulationOutput([f]),tmp_path/"a.nc")
    except OutputDependencyError: return
    import netCDF4
    with netCDF4.Dataset(p) as ds:
        assert ds.variables["x"][:].tolist()==[15.,45.]
        assert ds.variables["water_depth"].units=="m"

def test_netcdf_overwrite_protected(tmp_path):
    f=OutputField("water_depth",[[1]],1,1,30,30,0,0)
    p=tmp_path/"a.nc"; p.write_bytes(b"x")
    with pytest.raises(Exception): NetCDFWriter.write(SimulationOutput([f]),p)
def test_writer_rejects_non_netcdf(tmp_path):
    import pytest
    f=OutputField("water_depth",[[1]],1,1,30,30,0,0)
    with pytest.raises(OutputError, match="output.format=netcdf"):
        NetCDFWriter.write(SimulationOutput([f],"csv"),tmp_path/"x.nc")


def test_writer_rejects_inconsistent_vertical_datums_before_dependency_lookup(tmp_path):
    water_level = OutputField("water_level", [[1]], 1, 1, 30, 30, 0, 0, vertical_datum="Datum-A")
    depth_with_datum = OutputField("water_depth", [[1]], 1, 1, 30, 30, 0, 0, vertical_datum="Datum-B")
    with pytest.raises(OutputError, match="consistent datum"):
        NetCDFWriter.write(SimulationOutput([water_level, depth_with_datum]), tmp_path / "inconsistent.nc")


def test_writer_allows_consistent_vertical_datums_to_reach_dependency_check(tmp_path):
    water_level = OutputField("water_level", [[1]], 1, 1, 30, 30, 0, 0, vertical_datum="CGVD2013")
    depth_with_datum = OutputField("water_depth", [[1]], 1, 1, 30, 30, 0, 0, vertical_datum=" CGVD2013 ")
    try:
        NetCDFWriter.write(SimulationOutput([water_level, depth_with_datum]), tmp_path / "consistent.nc")
    except OutputDependencyError:
        pass
