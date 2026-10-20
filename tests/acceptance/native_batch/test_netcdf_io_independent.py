from pathlib import Path
import errno
import os

import netCDF4
import numpy as np
import pytest

from hydrodynamics import netcdf_path_io as module


@pytest.fixture
def scratch(tmp_path,monkeypatch):
    directory=tmp_path/'ascii-scratch';directory.mkdir()
    monkeypatch.setattr(module,'_temp_parent_candidates',lambda:(directory,))
    return directory


def create(target):
    target.parent.mkdir(exist_ok=True)
    with module.open_netcdf_dataset(netCDF4,target,'w',format='NETCDF4') as dataset:
        dataset.createDimension('n',1)
        dataset.createVariable('value','i8',('n',))[:]=[9007199254740993]
        dataset.units='m'


def test_unicode_clobber_false_preserves_existing_target(tmp_path,scratch):
    target=tmp_path/'中文目录'/'existing.nc';target.parent.mkdir()
    expected=b'independent pre-existing user bytes'
    target.write_bytes(expected)
    refused=False
    try:
        with module.open_netcdf_dataset(netCDF4,target,'w',clobber=False,format='NETCDF4') as dataset:
            dataset.createDimension('n',1)
    except (FileExistsError,OSError):
        refused=True
    assert target.read_bytes()==expected,'clobber=False overwrote the original Unicode destination'
    assert refused,'clobber=False did not refuse an existing Unicode destination'


def test_r_plus_change_during_publish_copy_is_preserved(tmp_path,scratch,monkeypatch):
    target=tmp_path/'中文目录'/'source.nc';create(target)
    expected=b'independent user update during final publication staging'
    original_copy=module.shutil.copyfileobj
    triggered=[]
    def actual_copy_then_concurrent_change(source,destination,length=0):
        result=original_copy(source,destination,length)
        if Path(destination.name).name.startswith('.hydrodynamics-netcdf-'):
            target.write_bytes(expected)
            triggered.append(True)
        return result
    monkeypatch.setattr(module.shutil,'copyfileobj',actual_copy_then_concurrent_change)
    refused=False
    try:
        with module.open_netcdf_dataset(netCDF4,target,'r+') as dataset:
            dataset.review_marker='independent update'
    except OSError:
        refused=True
    assert triggered,'the test did not reach the real final publication copy'
    assert target.read_bytes()==expected,'r+ erased a real concurrent change after its identity precheck'
    assert refused,'r+ did not report the conflicting final publication'


def test_clobber_false_file_appearing_during_publish_is_preserved(tmp_path,scratch,monkeypatch):
    target=tmp_path/'中文目录'/'new.nc';target.parent.mkdir()
    expected=b'independent file that appeared while output bytes were staged'
    original_copy=module.shutil.copyfileobj
    triggered=[]
    def actual_copy_then_create_target(source,destination,length=0):
        result=original_copy(source,destination,length)
        if Path(destination.name).name.startswith('.hydrodynamics-netcdf-'):
            target.write_bytes(expected)
            triggered.append(True)
        return result
    monkeypatch.setattr(module.shutil,'copyfileobj',actual_copy_then_create_target)
    refused=False
    try:
        with module.open_netcdf_dataset(netCDF4,target,'w',clobber=False,format='NETCDF4') as dataset:
            dataset.createDimension('n',1)
    except OSError:
        refused=True
    assert triggered
    assert target.read_bytes()==expected,'clobber=False erased a destination created during staging'
    assert refused


def test_read_source_change_during_copy_is_rejected_and_preserved(tmp_path,scratch,monkeypatch):
    target=tmp_path/'中文目录'/'source.nc';create(target)
    expected=b'independent replacement during read copy'
    original_copy=module.shutil.copyfileobj
    def actual_copy_then_change(source,destination,length=0):
        result=original_copy(source,destination,length)
        if Path(source.name)==target:
            target.write_bytes(expected)
        return result
    monkeypatch.setattr(module.shutil,'copyfileobj',actual_copy_then_change)
    with pytest.raises(OSError,match='source changed'):
        with module.open_netcdf_dataset(netCDF4,target,'r'):
            raise AssertionError('the changed source reached the reader body')
    assert target.read_bytes()==expected
    assert list(scratch.iterdir())==[]


def test_ascii_permission_failure_does_not_trigger_path_fallback(tmp_path,monkeypatch):
    failure=PermissionError(errno.EACCES,'independent actual permission failure')
    class Backend:
        @staticmethod
        def Dataset(*args,**kwargs):
            raise failure
    def forbidden_fallback():
        raise AssertionError('ASCII permission failure was misclassified as a Unicode path problem')
    monkeypatch.setattr(module,'_temporary_ascii_directory',forbidden_fallback)
    with pytest.raises(PermissionError) as captured:
        with module.open_netcdf_dataset(Backend,tmp_path/'ascii.nc','w'):
            raise AssertionError('permission failure was swallowed')
    assert captured.value is failure


def test_synthetic_32mib_netcdf_uses_bounded_streaming_and_keeps_int64(tmp_path,scratch,monkeypatch):
    target=tmp_path/'中文 大文件 路径'/'synthetic-32mib.nc';target.parent.mkdir()
    original_copy=module.shutil.copyfileobj
    buffers=[]
    def bounded_actual_copy(source,destination,length=0):
        assert 0<length<=1024*1024,'unbounded transport copy'
        buffers.append(length)
        return original_copy(source,destination,length)
    def forbidden_whole_file_read(self):
        raise AssertionError('the transport attempted an unbounded Path.read_bytes')
    monkeypatch.setattr(module.shutil,'copyfileobj',bounded_actual_copy)
    monkeypatch.setattr(Path,'read_bytes',forbidden_whole_file_read)
    count=4*1024*1024
    chunk=128*1024
    with module.open_netcdf_dataset(netCDF4,target,'w',format='NETCDF4') as dataset:
        dataset.createDimension('n',count)
        values=dataset.createVariable('value','i8',('n',))
        for start in range(0,count,chunk):
            values[start:start+chunk]=np.arange(start,start+chunk,dtype=np.int64)+9007199254740993
        dataset.provenance='中文来源；单纯传输测试，无求解器'
    assert target.stat().st_size>=32*1024*1024
    with module.open_netcdf_dataset(netCDF4,target,'r') as dataset:
        assert dataset['value'].dtype==np.dtype('int64')
        for index in [0,count//2,count-1]:
            assert int(dataset['value'][index])==9007199254740993+index
        assert dataset.provenance=='中文来源；单纯传输测试，无求解器'
    assert len(buffers)>=2
    assert list(scratch.iterdir())==[]
    assert list(target.parent.glob('.hydrodynamics-netcdf-*.tmp'))==[]
