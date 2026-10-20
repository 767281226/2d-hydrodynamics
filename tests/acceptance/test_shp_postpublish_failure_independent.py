from pathlib import Path
import pytest
from test_native_triangle_shp_independent import source,request

def test_postpublish_real_source_drift_still_raises_after_successful_marker_retirement(source,monkeypatch):
    import hydrodynamics.triangular_shp_export as module
    path,_,output=request(source,'postpublish_source_drift')
    original_rename=module.os.rename
    observed={}
    def publish_then_change_source(src,dst,*args,**kwargs):
        result=original_rename(src,dst,*args,**kwargs)
        if Path(dst).resolve()==output.resolve():
            observed['published']=True
            with path.open('ab') as stream:
                stream.write(b'\n# independent source drift after real publication\n')
        return result
    monkeypatch.setattr(module.os,'rename',publish_then_change_source)
    with pytest.raises((ValueError,RuntimeError,OSError)):
        module.export_native_triangle_shp(path)
    assert observed.get('published') is True
    assert not (output/'manifest.json').exists()
