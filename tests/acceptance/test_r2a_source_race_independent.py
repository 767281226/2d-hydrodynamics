from pathlib import Path
import pytest
from test_r2a_independent import source,_add_bound_g1

def test_changed_real_source_after_directory_exposure_cannot_leave_complete_manifest(source,monkeypatch):
    import hydrodynamics.rehearsal_package as package
    _add_bound_g1(source)
    destination=source['root']/'package'
    original_rename=package.os.rename
    def rename_then_change_actual_request(src,dst,*args,**kwargs):
        result=original_rename(src,dst,*args,**kwargs)
        if Path(dst).resolve()==destination.resolve():
            path=source['request_path']
            path.write_bytes(path.read_bytes()+b'\n# deliberate concurrent request-byte change\n')
        return result
    monkeypatch.setattr(package.os,'rename',rename_then_change_actual_request)
    try:report=package.export_result_package(source['request_path'])
    except (ValueError,RuntimeError,OSError):
        assert not (destination/'manifest.json').exists(),'real source-graph revalidation failure left COMPLETE_OFFLINE_REVIEW manifest'
    else:
        assert report.valid
        assert package.validate_result_package(source['request_path']).valid, 'successful retry must match the actual current source graph'
