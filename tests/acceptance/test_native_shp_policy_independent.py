from pathlib import Path
import importlib.util
import sys
import json
import numpy as np
import pytest
import yaml
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

@pytest.mark.parametrize('kind',['grid','wet','active','active_reprojected'])
def test_old_public_writers_reject_without_touching_target(tmp_path,kind):
    from hydrodynamics.inundation_export import write_grid_shapefile,write_wet_grid_shapefile
    from hydrodynamics.vector_mask import ActiveMask,write_active_grid_shapefile,write_active_grid_shapefile_reprojected
    target=tmp_path/'new_directory'/'not_a_native_mesh.shp'
    active=ActiveMask(np.ones((1,1),bool),1,1,50.,50.,474150.,4299600.,'EPSG:4548',Path('synthetic'),'EPSG:4548')
    geometry=dict(xmin=474150,ymin=4299600,dx=50,dy=50)
    calls={
        'grid':lambda:write_grid_shapefile(target,mask=np.ones((1,1),bool),**geometry),
        'wet':lambda:write_wet_grid_shapefile(target,wet_mask=np.ones((1,1),bool),max_depth=np.ones((1,1)),**geometry),
        'active':lambda:write_active_grid_shapefile(active,target),
        'active_reprojected':lambda:write_active_grid_shapefile_reprojected(active,target,target_crs='EPSG:4490'),
    }
    with pytest.raises((ValueError,RuntimeError),match='(?i)triang|native|三角'):
        calls[kind]()
    assert not target.parent.exists()


def test_new_structured_workflow_still_computes_but_never_writes_shp(tmp_path):
    from hydrodynamics.workflow import run_workflow,find_latest_completed_bundle
    spec=importlib.util.spec_from_file_location('policy_fixture',ROOT/'tests/test_workflow.py')
    mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
    job=mod.job.__wrapped__(tmp_path)
    job['case']['model']['end_time']=.2;job['case']['output']['interval']=.1
    (tmp_path/'case.yaml').write_text(yaml.safe_dump(job['case']),encoding='utf-8')
    job['write_forcing']([[0.,1.,.1,0.,.1,0.],[.1,1.,.1,0.,.1,0.],[.2,1.,.1,0.,.1,0.]])
    bundle=run_workflow(job['file'],run_id='structured-policy')
    assert (bundle/'result.nc').is_file()
    assert not list(bundle.rglob('*.shp'))
    report=json.loads((bundle/'run_report.json').read_text())
    assert report['status']=='complete'
    assert report['workflow']['shp']['status'] not in ('complete','COMPLETE','pass')
    assert find_latest_completed_bundle(bundle.parent)==bundle.resolve()
