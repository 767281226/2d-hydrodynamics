"""Windows NetCDF path compatibility using short-lived ASCII disk copies.

The installed netCDF4/HDF5 stack may fail on otherwise valid non-ASCII Windows
paths. This adapter keeps NetCDF access on an ASCII temporary path and uses
Python's Unicode-aware filesystem APIs to copy bytes to or from the requested
path. Copies are streamed with a fixed-size buffer; the whole file is never
loaded into memory.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import ctypes
import hashlib
import os
import shutil
import tempfile


_COPY_BUFFER_BYTES = 1024 * 1024
_WRITE_MODES = {"w", "r+"}


def _needs_ascii_temp_path(path: str | os.PathLike[str]) -> bool:
    return os.name == "nt" and not os.fspath(path).isascii()


def _short_windows_path(path: Path) -> Path | None:
    """Return an ASCII 8.3 alias when Windows provides one."""
    if os.name != "nt":
        return None
    try:
        get_short_path_name = ctypes.windll.kernel32.GetShortPathNameW
        get_short_path_name.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        get_short_path_name.restype = ctypes.c_uint32
        needed = get_short_path_name(str(path), None, 0)
        if needed == 0:
            return None
        buffer = ctypes.create_unicode_buffer(needed)
        written = get_short_path_name(str(path), buffer, needed)
        if written == 0 or written >= needed:
            return None
        result = buffer.value
        if not result.isascii():
            return None
        return Path(result)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _temp_parent_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    package_root = Path(__file__).absolute().parent.parent
    if os.fspath(package_root).isascii():
        project_work = package_root / "work"
        if project_work.is_dir():
            candidates.append(project_work)

    system_temp = Path(tempfile.gettempdir())
    candidates.append(system_temp)
    short_temp = _short_windows_path(system_temp)
    if short_temp is not None and short_temp != system_temp:
        candidates.append(short_temp)

    windows_temp = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp"
    if windows_temp not in candidates:
        candidates.append(windows_temp)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = os.path.abspath(os.fspath(candidate))
        if value not in seen:
            seen.add(value)
            unique.append(Path(value))
    return tuple(unique)


def _temporary_ascii_directory() -> tempfile.TemporaryDirectory[str]:
    failures: list[OSError] = []
    for parent in _temp_parent_candidates():
        parent_text = os.fspath(parent)
        if not parent_text.isascii() or not parent.is_dir():
            continue
        try:
            workspace = tempfile.TemporaryDirectory(prefix="hydrodynamics-nc-", dir=parent_text)
        except OSError as exc:
            failures.append(exc)
            continue
        if not os.fspath(workspace.name).isascii():
            workspace.cleanup()
            continue
        return workspace
    detail = str(failures[-1]) if failures else "no writable ASCII temporary directory was found"
    raise OSError(f"cannot create an ASCII temporary directory for NetCDF path compatibility: {detail}")


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _copy_and_sync(source: Path, destination: Path) -> None:
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=_COPY_BUFFER_BYTES)
        output_stream.flush()
        os.fsync(output_stream.fileno())


def _copy_stable_source(source: Path, destination: Path) -> tuple[int, int, int, int, int]:
    before = _file_identity(source)
    _copy_and_sync(source, destination)
    after = _file_identity(source)
    if before != after or destination.stat().st_size != before[2]:
        destination.unlink(missing_ok=True)
        raise OSError(f"NetCDF source changed while it was copied to a temporary file: {source}")
    return before


@contextmanager
def _publication_mutex(destination: Path) -> Iterator[None]:
    """Serialize adapter-managed publishers for this destination on Windows."""
    if os.name != "nt":
        yield
        return

    normalized = os.path.normcase(os.path.abspath(os.fspath(destination)))
    digest = hashlib.sha256(normalized.encode("utf-8", "surrogatepass")).hexdigest()
    name = f"Local\\HydrodynamicsNetCDF-{digest}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    wait_for_single_object.restype = ctypes.c_uint32
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = (ctypes.c_void_p,)
    release_mutex.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int

    handle = create_mutex(None, 0, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        wait_result = wait_for_single_object(handle, 30_000)
        if wait_result in {0, 0x80}:  # WAIT_OBJECT_0 or WAIT_ABANDONED
            acquired = True
        elif wait_result == 0x102:  # WAIT_TIMEOUT
            raise TimeoutError(f"timed out waiting for NetCDF publication lock: {destination}")
        else:
            raise ctypes.WinError(ctypes.get_last_error())
        yield
    finally:
        release_error = None
        if acquired and not release_mutex(handle):
            release_error = ctypes.WinError(ctypes.get_last_error())
        close_handle(handle)
        if release_error is not None:
            raise release_error


def _rename_without_replacing(source: Path, destination: Path) -> None:
    """Atomically publish only if destination is still absent."""
    if os.name == "nt":
        # Windows os.rename fails when destination exists; unlike os.replace it
        # remains a no-clobber primitive when a file appears after staging.
        os.rename(source, destination)
        return
    # A same-volume hard-link creation is atomic and fails with EEXIST.
    os.link(source, destination)
    source.unlink()


def _publish_atomically(
    source: Path,
    destination: Path,
    *,
    clobber: bool,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".hydrodynamics-netcdf-", suffix=".tmp",
            dir=destination.parent, delete=False,
        ) as output_stream:
            temporary = Path(output_stream.name)
            with source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output_stream, length=_COPY_BUFFER_BYTES)
            output_stream.flush()
            os.fsync(output_stream.fileno())

        if not clobber:
            _rename_without_replacing(temporary, destination)
            return

        with _publication_mutex(destination):
            # The staged bytes are complete before this identity check. The
            # named mutex serializes adapter-managed publishers from this
            # recheck through replacement, but is not a filesystem CAS for
            # arbitrary external writers.
            if expected_identity is not None and _file_identity(destination) != expected_identity:
                raise OSError(
                    f"NetCDF source changed during final staging; refusing to replace it: {destination}"
                )
            os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def open_netcdf_dataset(
    netcdf4: Any,
    path: str | os.PathLike[str],
    mode: str = "r",
    **kwargs: Any,
) -> Iterator[Any]:
    """Open a NetCDF dataset, adapting non-ASCII Windows paths via disk copies.

    Read-only opens copy the source to an ASCII temporary directory. Write and
    read/write opens operate there and publish the completed bytes atomically
    through a temporary file beside the requested destination. ``r+`` refuses
    to publish if the original changed during the edit. All copies stream in
    1 MiB chunks and temporary files are removed on success or failure.
    """
    target = Path(path)
    if not _needs_ascii_temp_path(path):
        with netcdf4.Dataset(os.fspath(path), mode, **kwargs) as dataset:
            yield dataset
        return
    if mode not in {"r", "w", "r+"}:
        raise ValueError(f"non-ASCII Windows NetCDF compatibility does not support mode {mode!r}")

    clobber = kwargs.get("clobber", True) if mode == "w" else True
    if mode == "w" and clobber is False and target.exists():
        raise FileExistsError(f"clobber=False refuses to replace the existing NetCDF destination: {target}")

    workspace = _temporary_ascii_directory()
    temporary_path = Path(workspace.name) / "dataset.nc"
    original_identity: tuple[int, int, int, int, int] | None = None
    try:
        if mode in {"r", "r+"}:
            original_identity = _copy_stable_source(target, temporary_path)

        dataset = netcdf4.Dataset(os.fspath(temporary_path), mode, **kwargs)
        try:
            yield dataset
        except BaseException:
            try:
                dataset.close()
            except Exception:
                pass
            raise
        else:
            dataset.close()
            if mode in _WRITE_MODES:
                _publish_atomically(
                    temporary_path,
                    target,
                    clobber=bool(clobber),
                    expected_identity=original_identity if mode == "r+" else None,
                )
    finally:
        workspace.cleanup()


__all__ = ["open_netcdf_dataset"]
