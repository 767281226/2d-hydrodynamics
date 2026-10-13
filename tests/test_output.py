import pytest
from hydrodynamics import OutputError, OutputField, SimulationOutput

def test_output_units_shapes_and_masks():
 f=OutputField("water_depth",[[1,2],[3,4]],2,2,30,40,0,0,valid_mask=[[True,True],[True,False]])
 assert f.unit=="m" and f.shape==(2,2)
 s=OutputField("velocity_x",[[[1,2],[3,4]],[[2,3],[4,5]]],2,2,30,40,0,0,times=[0,1]); assert s.shape==(2,2,2)
def test_output_rejects_bad_values_times_masks_and_level_datum():
 with pytest.raises(OutputError): OutputField("water_depth",[[float("nan")]],1,1,30,30,0,0)
 with pytest.raises(OutputError): OutputField("velocity_x",[[[1]]],1,1,30,30,0,0,times=[1,0])
 with pytest.raises(OutputError): OutputField("water_level",[[1]],1,1,30,30,0,0)
def test_output_dataset_format_and_duplicates():
 f=OutputField("water_depth",[[1]],1,1,30,30,0,0); out=SimulationOutput([f],"netcdf"); assert out.validate() is out
 with pytest.raises(OutputError): SimulationOutput([f,f]).validate()
 with pytest.raises(OutputError): SimulationOutput([],"bad").validate()

def test_timeseries_requires_times_and_masks_are_boolean_disjoint():
    import pytest
    with pytest.raises(OutputError): OutputField("velocity_x", [[[1]]], 1, 1, 30, 30, 0, 0)
    with pytest.raises(OutputError): OutputField("water_depth", [[1]], 1, 1, 30, 30, 0, 0, valid_mask=[[1]])
    with pytest.raises(OutputError): OutputField("water_depth", [[1]], 1, 1, 30, 30, 0, 0, valid_mask=[[True]], nodata_mask=[[True]])
