from pathlib import Path
import pytest
from test_native_triangle_shp_independent import source,request

def test_prepublication_failure_does_not_recursively_delete_a_replaced_stage_root(source,monkeypatch):
    import hydrodynamics.triangular_shp_export as module
    path,_,output=request(source,'stage_root_replacement')
    original_writer=module._write_layer
    moved=source['root']/'moved_original_stage'
    observed={}
    def write_then_replace_stage(stage,context,built):
        original_writer(stage,context,built)
        stage=Path(stage)
        module.os.rename(stage,moved)
        stage.mkdir()
        (stage/'independent_foreign_file.txt').write_bytes(b'preserve this unrelated concurrent stage root')
        observed['stage']=stage
        raise module.NativeTriangleSHPError('independent write failure after a concurrent stage-root replacement')
    monkeypatch.setattr(module,'_write_layer',write_then_replace_stage)
    with pytest.raises((ValueError,RuntimeError,OSError)):
        module.export_native_triangle_shp(path)
    assert not output.exists()
    assert (moved/'faces.shp').is_file()
    assert (observed['stage']/'independent_foreign_file.txt').read_bytes()==b'preserve this unrelated concurrent stage root'
