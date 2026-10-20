"""Independent Windows BAT invocation helper; only writes fresh acceptance directories."""
from pathlib import Path
import hashlib
import json
import os
import subprocess

ROOT=Path(__file__).resolve().parents[3]
BATCH=ROOT/'run_qingbei_nc.bat'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(directory):
    return {str(p.relative_to(directory)):sha(p) for p in Path(directory).rglob('*') if p.is_file()}


def invoke(batch,args,cwd,*,env=None,timeout=120):
    cwd=Path(cwd)
    wrapper=cwd/'independent_call.cmd'
    after=cwd/'independent_after.txt'
    assert not wrapper.exists() and not after.exists()
    command=subprocess.list2cmdline([str(batch),*map(str,args)])
    wrapper.write_text(chr(10).join(['@echo off','chcp 65001 >nul','call '+command,'set "INDEPENDENT_RC=%ERRORLEVEL%"','cd > "'+str(after)+'"','exit /b %INDEPENDENT_RC%','']),encoding='utf-8')
    settings=os.environ.copy()
    settings['PYTHONUTF8']='1'
    if env:settings.update(env)
    result=subprocess.run([os.environ.get('COMSPEC','cmd.exe'),'/d','/c',str(wrapper)],cwd=cwd,env=settings,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    assert after.is_file(),result.stdout+result.stderr
    assert Path(after.read_text(encoding='utf-8').strip()).resolve()==cwd.resolve(), 'BAT did not restore caller cwd'
    return result


def assert_protected_baseline():
    record=Path(__file__).parent/'baseline.json'
    if not record.is_file():
        return  # Private local-data evidence is absent from clean repository candidates.
    baseline=json.loads(record.read_text(encoding='utf-8'))
    for name,expected in baseline['protected'].items():
        assert sha(ROOT/name)==expected,name
