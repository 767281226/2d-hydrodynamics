from pathlib import Path
import numpy as np
import netCDF4
from security_fixture import make_inputs,assert_actual_face_coverage
from test_security_integration_independent import configure,prepare,prepare_sources

def test_valid_security_mesh_runs_manning_and_keeps_bound_active_faces(tmp_path):
    from hydrodynamics.triangular_manning import run_triangular_manning
    from hydrodynamics.triangular_manning_netcdf import read_triangular_manning_result
    source=configure(make_inputs(tmp_path/'manning_positive'))
    mesh,_=prepare(source);prepare_sources(source,'manning')
    report=run_triangular_manning(source['root']/'manning_run.yaml')
    result=read_triangular_manning_result(source['root']/'manning.nc',source['root']/'manning_run.yaml')
    assert result.security_exclusion is not None
    assert result.sources_applied=='manning' and result.manning_enabled is True
    assert tuple(result.face_id)==tuple(mesh.face_id)
    assert_actual_face_coverage(mesh,source)
    with netCDF4.Dataset(source['root']/'manning.nc') as ds:
        h=np.asarray(ds.variables['water_depth'][:]);a=np.asarray(ds.variables['face_area'][:])
        assert h.shape==(3,mesh.n_face)
        assert np.max(np.abs(h-.2))<1e-12
        assert np.max(np.abs(h@a-3.))<1e-12
    assert all(abs(row['residual_m3'])<1e-12 for row in report['mass_budget'])
