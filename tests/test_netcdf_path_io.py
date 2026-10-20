from __future__ import annotations

import os
from pathlib import Path

import pytest

from hydrodynamics import netcdf_path_io


def _netcdf4():
    return pytest.importorskip("netCDF4")


def _force_ascii_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scratch = tmp_path / "ascii netcdf scratch"
    scratch.mkdir()
    monkeypatch.setattr(netcdf_path_io, "_temp_parent_candidates", lambda: (scratch,))
    if os.name != "nt":
        monkeypatch.setattr(netcdf_path_io, "_needs_ascii_temp_path", lambda _path: True)
    return scratch


def _assert_no_temporary_files(target: Path, scratch: Path) -> None:
    assert list(scratch.iterdir()) == []
    assert list(target.parent.glob(".hydrodynamics-netcdf-*.tmp")) == []


@pytest.mark.parametrize("folder", ["中文路径", "中文 路径"])
def test_netcdf_roundtrip_and_read_write_support_chinese_paths(tmp_path, monkeypatch, folder):
    netcdf4 = _netcdf4()
    scratch = _force_ascii_workspace(tmp_path, monkeypatch)
    target = tmp_path / folder / "source result.nc"
    target.parent.mkdir()

    with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "w", format="NETCDF4") as dataset:
        dataset.createDimension("sample", 1)
        value = dataset.createVariable("value", "i4", ("sample",))
        value[:] = [42]
        dataset.setncattr("source_label", "中文来源")
    assert target.is_file()
    _assert_no_temporary_files(target, scratch)

    with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "r") as dataset:
        assert dataset.variables["value"][:].tolist() == [42]
        assert dataset.getncattr("source_label") == "中文来源"
    _assert_no_temporary_files(target, scratch)

    with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "r+") as dataset:
        dataset.setncattr("validated", "passed")
    with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "r") as dataset:
        assert dataset.getncattr("validated") == "passed"
    _assert_no_temporary_files(target, scratch)


def test_failed_unicode_write_preserves_existing_file_and_cleans_temporary_files(tmp_path, monkeypatch):
    netcdf4 = _netcdf4()
    scratch = _force_ascii_workspace(tmp_path, monkeypatch)
    target = tmp_path / "中文目录" / "existing.nc"
    target.parent.mkdir()
    original = b"pre-existing bytes must remain intact"
    target.write_bytes(original)

    with pytest.raises(RuntimeError, match="injected writer failure"):
        with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "w", format="NETCDF4") as dataset:
            dataset.createDimension("sample", 1)
            raise RuntimeError("injected writer failure")

    assert target.read_bytes() == original
    _assert_no_temporary_files(target, scratch)


def test_unicode_write_publish_failure_preserves_existing_file_and_cleans_temporary_files(
    tmp_path, monkeypatch,
):
    netcdf4 = _netcdf4()
    scratch = _force_ascii_workspace(tmp_path, monkeypatch)
    target = tmp_path / "中文目录 空格" / "existing.nc"
    target.parent.mkdir()
    original = b"keep this output on failed publication"
    target.write_bytes(original)

    def fail_replace(_source, _destination):
        raise PermissionError("injected destination publication failure")

    monkeypatch.setattr(netcdf_path_io.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="injected destination publication failure"):
        with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "w", format="NETCDF4") as dataset:
            dataset.createDimension("sample", 1)
            dataset.createVariable("value", "i4", ("sample",))[:] = [7]

    assert target.read_bytes() == original
    _assert_no_temporary_files(target, scratch)


def test_unicode_clobber_flag_distinguishes_refusal_from_explicit_replace(tmp_path, monkeypatch):
    netcdf4 = _netcdf4()
    scratch = _force_ascii_workspace(tmp_path, monkeypatch)
    target = tmp_path / "中文目录" / "clobber.nc"
    target.parent.mkdir()
    original = b"preserve unless clobber is explicit"
    target.write_bytes(original)

    with pytest.raises(FileExistsError, match="clobber=False"):
        with netcdf_path_io.open_netcdf_dataset(
            netcdf4, target, "w", clobber=False, format="NETCDF4",
        ) as dataset:
            dataset.createDimension("sample", 1)
    assert target.read_bytes() == original
    _assert_no_temporary_files(target, scratch)

    with netcdf_path_io.open_netcdf_dataset(
        netcdf4, target, "w", clobber=True, format="NETCDF4",
    ) as dataset:
        dataset.createDimension("sample", 1)
        dataset.createVariable("value", "i4", ("sample",))[:] = [9]
    with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "r") as dataset:
        assert dataset.variables["value"][:].tolist() == [9]
    _assert_no_temporary_files(target, scratch)


def test_unicode_clobber_false_preserves_target_created_during_final_copy(tmp_path, monkeypatch):
    netcdf4 = _netcdf4()
    scratch = _force_ascii_workspace(tmp_path, monkeypatch)
    target = tmp_path / "中文目录" / "late.nc"
    target.parent.mkdir()
    competing_bytes = b"file created during the final publication copy"
    original_copy = netcdf_path_io.shutil.copyfileobj
    triggered = []

    def copy_then_create_target(source, destination, length=0):
        result = original_copy(source, destination, length)
        if Path(destination.name).name.startswith(".hydrodynamics-netcdf-"):
            target.write_bytes(competing_bytes)
            triggered.append(True)
        return result

    monkeypatch.setattr(netcdf_path_io.shutil, "copyfileobj", copy_then_create_target)
    with pytest.raises(FileExistsError):
        with netcdf_path_io.open_netcdf_dataset(
            netcdf4, target, "w", clobber=False, format="NETCDF4",
        ) as dataset:
            dataset.createDimension("sample", 1)

    assert triggered
    assert target.read_bytes() == competing_bytes
    _assert_no_temporary_files(target, scratch)


def test_unicode_read_write_refuses_to_replace_a_concurrently_changed_file(tmp_path, monkeypatch):
    netcdf4 = _netcdf4()
    scratch = _force_ascii_workspace(tmp_path, monkeypatch)
    target = tmp_path / "中文目录" / "source.nc"
    target.parent.mkdir()
    with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "w", format="NETCDF4") as dataset:
        dataset.createDimension("sample", 1)
        dataset.createVariable("value", "i4", ("sample",))[:] = [1]
    competing_bytes = b"concurrent replacement"

    with pytest.raises(OSError, match="refusing to replace"):
        with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "r+") as dataset:
            dataset.setncattr("update", "must not publish")
            target.write_bytes(competing_bytes)

    assert target.read_bytes() == competing_bytes
    _assert_no_temporary_files(target, scratch)


def test_unicode_read_write_refuses_change_during_final_staging_copy(tmp_path, monkeypatch):
    netcdf4 = _netcdf4()
    scratch = _force_ascii_workspace(tmp_path, monkeypatch)
    target = tmp_path / "中文目录" / "source.nc"
    target.parent.mkdir()
    with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "w", format="NETCDF4") as dataset:
        dataset.createDimension("sample", 1)
        dataset.createVariable("value", "i4", ("sample",))[:] = [1]
    competing_bytes = b"user update during the final staging copy"
    original_copy = netcdf_path_io.shutil.copyfileobj
    triggered = []

    def copy_then_change_target(source, destination, length=0):
        result = original_copy(source, destination, length)
        if Path(destination.name).name.startswith(".hydrodynamics-netcdf-"):
            target.write_bytes(competing_bytes)
            triggered.append(True)
        return result

    monkeypatch.setattr(netcdf_path_io.shutil, "copyfileobj", copy_then_change_target)
    with pytest.raises(OSError, match="during final staging"):
        with netcdf_path_io.open_netcdf_dataset(netcdf4, target, "r+") as dataset:
            dataset.setncattr("update", "must not publish")

    assert triggered
    assert target.read_bytes() == competing_bytes
    _assert_no_temporary_files(target, scratch)
