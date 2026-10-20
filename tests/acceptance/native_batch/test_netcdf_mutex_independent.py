from pathlib import Path
import json
import multiprocessing as mp
import os
import pytest
import netCDF4
from hydrodynamics import netcdf_path_io as module


class FakeFunction:
    def __init__(self,value):
        self.value=value
        self.calls=[]
    def __call__(self,*args):
        self.calls.append(args)
        return self.value


class FakeKernel:
    def __init__(self,wait):
        self.CreateMutexW=FakeFunction(1234)
        self.WaitForSingleObject=FakeFunction(wait)
        self.ReleaseMutex=FakeFunction(1)
        self.CloseHandle=FakeFunction(1)


@pytest.mark.parametrize('wait,error',[(0x102,TimeoutError),(0xffffffff,OSError)])
def test_mutex_failed_acquisition_closes_handle_without_releasing_unowned_mutex(tmp_path,monkeypatch,wait,error):
    kernel=FakeKernel(wait)
    monkeypatch.setattr(module.ctypes,'WinDLL',lambda *args,**kwargs:kernel)
    monkeypatch.setattr(module.ctypes,'get_last_error',lambda:5)
    entered=[]
    with pytest.raises(error):
        with module._publication_mutex(tmp_path/'中文目标.nc'):
            entered.append(True)
    assert not entered
    assert len(kernel.CloseHandle.calls)==1
    assert not kernel.ReleaseMutex.calls


def mutex_child(target,entered,release,record,fail):
    try:
        with module._publication_mutex(Path(target)):
            entered.set()
            assert release.wait(10),'parent failed to release test barrier'
            if fail:
                raise RuntimeError('independent body exception')
    except RuntimeError:
        Path(record).write_text('released-after-exception',encoding='utf-8')
    else:
        Path(record).write_text('released-normal',encoding='utf-8')


def test_real_windows_mutex_serializes_processes_and_releases_after_body_exception(tmp_path):
    assert os.name=='nt','this acceptance requires the actual Windows implementation'
    context=mp.get_context('spawn')
    first_entered,second_entered=context.Event(),context.Event()
    first_release,second_release=context.Event(),context.Event()
    target=str(tmp_path/'中文同一目标.nc')
    first_record,second_record=tmp_path/'first.txt',tmp_path/'second.txt'
    first=context.Process(target=mutex_child,args=(target,first_entered,first_release,str(first_record),True))
    second=context.Process(target=mutex_child,args=(target,second_entered,second_release,str(second_record),False))
    first.start()
    try:
        assert first_entered.wait(10)
        second.start()
        assert not second_entered.wait(.3),'second process entered while the first held the mutex'
        first_release.set()
        assert second_entered.wait(10),'body exception failed to release the mutex'
        second_release.set()
        first.join(10);second.join(10)
        assert first.exitcode==0 and second.exitcode==0
        assert first_record.read_text(encoding='utf-8')=='released-after-exception'
        assert second_record.read_text(encoding='utf-8')=='released-normal'
        with module._publication_mutex(Path(target)):
            pass
    finally:
        first_release.set();second_release.set()
        for process in [first,second]:
            if process.pid is not None:
                process.join(10)
                if process.is_alive():process.terminate();process.join(5)
                process.close()


def test_two_adapter_snapshots_preserve_the_first_committed_result(tmp_path,monkeypatch):
    scratch=tmp_path/'ascii';scratch.mkdir()
    monkeypatch.setattr(module,'_temp_parent_candidates',lambda:(scratch,))
    target=tmp_path/'中文路径'/'source.nc';target.parent.mkdir()
    with module.open_netcdf_dataset(netCDF4,target,'w',format='NETCDF4') as dataset:
        dataset.createDimension('n',1)
        dataset.createVariable('value','i8',('n',))[:]=[9007199254740993]
    with pytest.raises(OSError,match='refusing to replace'):
        with module.open_netcdf_dataset(netCDF4,target,'r+') as older:
            with module.open_netcdf_dataset(netCDF4,target,'r+') as first_commit:
                first_commit.marker='keep the committed snapshot'
            older.marker='stale snapshot must fail'
    with module.open_netcdf_dataset(netCDF4,target,'r') as dataset:
        assert dataset.marker=='keep the committed snapshot'
        assert int(dataset['value'][0])==9007199254740993
    assert not list(scratch.iterdir())
    assert not list(target.parent.glob('.hydrodynamics-netcdf-*.tmp'))
