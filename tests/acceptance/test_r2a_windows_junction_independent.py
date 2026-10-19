from pathlib import Path
import os
import subprocess
import pytest
import yaml
from test_r2a_independent import source

@pytest.mark.skipif(os.name!='nt',reason='native Windows junction contract')
def test_output_cannot_escape_request_directory_via_windows_junction(source):
    import hydrodynamics.rehearsal_package as package
    request_root=source['root'].resolve()
    outside=request_root.parent/('outside-'+request_root.name)
    assert outside.parent==request_root.parent and outside!=request_root
    outside.mkdir()
    guard=outside/'keep.txt';guard.write_bytes(b'concurrent outside target')
    junction=request_root/'output_alias'
    result=subprocess.run(['cmd.exe','/c','mklink','/J',str(junction),str(outside)],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert junction.exists() and not junction.is_symlink()
    if hasattr(junction,'is_junction'):assert junction.is_junction()
    try:
        source['request']['output_directory']='output_alias/package'
        source['request_path'].write_text(yaml.safe_dump(source['request']),encoding='utf-8')
        with pytest.raises((ValueError,RuntimeError,OSError)):
            package.export_result_package(source['request_path'])
        assert not (outside/'package').exists(),'a child output key escaped through a Windows junction'
        assert guard.read_bytes()==b'concurrent outside target'
    finally:
        # This is the exact test-created junction, not a recursive target deletion.
        os.rmdir(junction)
