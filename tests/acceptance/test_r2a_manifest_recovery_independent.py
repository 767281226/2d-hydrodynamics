from pathlib import Path
import json
import pytest
from test_r2a_independent import source,_add_bound_g1

def test_modified_owned_manifest_is_quarantined_without_losing_concurrent_bytes(source,monkeypatch):
    import hydrodynamics.rehearsal_package as package
    _add_bound_g1(source)
    destination=source['root']/'package'
    original_rename=package.os.rename
    expected={}
    def publish_then_modify_manifest(src,dst,*args,**kwargs):
        result=original_rename(src,dst,*args,**kwargs)
        if Path(dst).resolve()==destination.resolve():
            path=destination/'manifest.json'
            payload=json.loads(path.read_bytes())
            payload['concurrent_user_note']='independent recovery marker: do not silently delete these bytes'
            raw=json.dumps(payload,ensure_ascii=False,indent=1).encode('utf-8')
            path.write_bytes(raw)
            expected['raw']=raw
        return result
    monkeypatch.setattr(package.os,'rename',publish_then_modify_manifest)
    with pytest.raises((ValueError,RuntimeError,OSError)):
        package.export_result_package(source['request_path'])
    assert not (destination/'manifest.json').exists(),'the invalid success marker must be retired'
    recovered=[p for p in source['root'].rglob('*') if p.is_file() and p.read_bytes()==expected['raw']]
    assert recovered,'rollback discarded concurrent modifications instead of preserving a recoverable file'
