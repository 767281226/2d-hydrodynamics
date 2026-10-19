from pathlib import Path
import os
import pytest
from test_r2a_independent import source,_add_bound_g1

def test_replaced_parent_directory_keeps_concurrent_hardlink_after_export_failure(source,monkeypatch):
    import hydrodynamics.rehearsal_package as package
    _add_bound_g1(source)
    destination=source['root']/'package'
    moved=source['root']/'moved-original-native'
    original_rename=package.os.rename
    expected={}
    def publish_then_replace_child_parent(src,dst,*args,**kwargs):
        result=original_rename(src,dst,*args,**kwargs)
        if Path(dst).resolve()==destination.resolve():
            original_rename(destination/'native',moved)
            (destination/'native').mkdir()
            os.link(moved/'result.nc',destination/'native/result.nc')
            expected['bytes']=(destination/'native/result.nc').read_bytes()
            request=source['request_path']
            request.write_bytes(request.read_bytes()+b'\n# concurrent source change forces real final revalidation failure\n')
        return result
    monkeypatch.setattr(package.os,'rename',publish_then_replace_child_parent)
    with pytest.raises((ValueError,RuntimeError,OSError)):
        package.export_result_package(source['request_path'])
    foreign_link=destination/'native/result.nc'
    assert foreign_link.is_file(),'rollback deleted a concurrent hardlink beneath a replaced parent directory'
    assert foreign_link.read_bytes()==expected['bytes']
    assert (moved/'result.nc').read_bytes()==expected['bytes']
    assert not (destination/'manifest.json').exists()
