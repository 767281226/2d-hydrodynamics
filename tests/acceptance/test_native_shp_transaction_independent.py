from pathlib import Path
import json
import pytest
from test_native_triangle_shp_independent import source,request


def test_failed_export_retires_modified_marker_and_preserves_concurrent_content(source,monkeypatch):
    import hydrodynamics.triangular_shp_export as module
    path,_,directory=request(source,'modified_marker')
    original_rename=module.os.rename
    expected={}
    def publish_then_modify(src,dst,*args,**kwargs):
        result=original_rename(src,dst,*args,**kwargs)
        if Path(dst).resolve()==directory.resolve():
            marker=directory/'manifest.json';data=json.loads(marker.read_bytes())
            data['concurrent_user_note']='preserve these exact independent recovery bytes'
            raw=json.dumps(data,ensure_ascii=False,indent=1).encode('utf-8');marker.write_bytes(raw);expected['raw']=raw
            (directory/'concurrent.txt').write_bytes(b'independent concurrent file')
        return result
    monkeypatch.setattr(module.os,'rename',publish_then_modify)
    with pytest.raises((ValueError,RuntimeError,OSError)):module.export_native_triangle_shp(path)
    assert not (directory/'manifest.json').exists(),'failed SHP export retained a success marker'
    assert (directory/'concurrent.txt').read_bytes()==b'independent concurrent file'
    assert any(p.is_file() and p.read_bytes()==expected['raw'] for p in source['root'].rglob('*')),'concurrent manifest edits were not recoverable'


def test_failed_export_does_not_delete_a_replaced_root_even_with_same_marker_bytes(source,monkeypatch):
    import hydrodynamics.triangular_shp_export as module
    path,_,directory=request(source,'replaced_root')
    moved=source['root']/'moved_original_output'
    original_rename=module.os.rename
    expected={}
    def publish_then_replace_root(src,dst,*args,**kwargs):
        result=original_rename(src,dst,*args,**kwargs)
        if Path(dst).resolve()==directory.resolve():
            original_rename(directory,moved)
            directory.mkdir()
            raw=(moved/'manifest.json').read_bytes();expected['raw']=raw
            (directory/'manifest.json').write_bytes(raw)
            (directory/'concurrent.txt').write_bytes(b'foreign root content')
        return result
    monkeypatch.setattr(module.os,'rename',publish_then_replace_root)
    with pytest.raises((ValueError,RuntimeError,OSError)):module.export_native_triangle_shp(path)
    assert (directory/'manifest.json').read_bytes()==expected['raw'],'rollback removed a foreign marker solely because its bytes matched'
    assert (directory/'concurrent.txt').read_bytes()==b'foreign root content'
    assert (moved/'manifest.json').read_bytes()==expected['raw']
