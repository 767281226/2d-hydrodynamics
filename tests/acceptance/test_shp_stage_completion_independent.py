from pathlib import Path
import pytest
from test_native_triangle_shp_independent import source,request

def test_failure_in_owned_stage_preserves_concurrently_added_file(source,monkeypatch):
    import hydrodynamics.triangular_shp_export as module
    path,_,output=request(source,'owned_stage_content')
    original_writer=module._write_layer
    observed={}
    def write_then_add_foreign_file(stage,context,built):
        original_writer(stage,context,built)
        stage=Path(stage);observed['stage']=stage
        (stage/'independent_foreign_file.txt').write_bytes(b'preserve concurrent content in an owned root')
        raise module.NativeTriangleSHPError('independent failure with concurrent content in an owned stage')
    monkeypatch.setattr(module,'_write_layer',write_then_add_foreign_file)
    with pytest.raises((ValueError,RuntimeError,OSError)):
        module.export_native_triangle_shp(path)
    assert not output.exists()
    assert (observed['stage']/'independent_foreign_file.txt').read_bytes()==b'preserve concurrent content in an owned root'
    assert not (observed['stage']/'manifest.json').exists()

def test_real_source_drift_before_stage_validation_retires_owned_success_marker(source,monkeypatch):
    import hydrodynamics.triangular_shp_export as module
    path,_,output=request(source,'owned_stage_marker')
    original_validator=module.validate_native_triangle_shp_package
    observed={}
    def change_source_before_real_validation(request_path,package_directory=None):
        stage=Path(package_directory) if package_directory is not None else None
        if stage is not None and stage.name.startswith('.owned_stage_marker.stage-'):
            observed['stage']=stage
            assert (stage/'manifest.json').is_file()
            with Path(request_path).open('ab') as stream:
                stream.write(b'\n# independent source drift before real stage validation\n')
        return original_validator(request_path,package_directory)
    monkeypatch.setattr(module,'validate_native_triangle_shp_package',change_source_before_real_validation)
    with pytest.raises((ValueError,RuntimeError,OSError)):
        module.export_native_triangle_shp(path)
    assert not output.exists()
    assert 'stage' in observed
    assert not (observed['stage']/'manifest.json').exists(),'a failed owned stage must not retain its successful manifest'
