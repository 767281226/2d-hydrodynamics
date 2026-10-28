"""Windows Job Object supervisor for bounded preparation-only worker runs.

Only the worker process tree is memory-limited.  The supervisor starts the
worker suspended, assigns it to one non-breakaway Job Object with both
per-process and aggregate job memory limits, and resumes it only after the
kernel accepts the assignment.  There is no RSS-only fallback.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePath, PureWindowsPath
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping
import uuid

from .prepared_terrain import PreparedTerrainStopped


FORMAT = "triangle-terrain-preparation-worker-config-v1"
B1_PILOT_FORMAT = "triangle-b1-synthetic-pilot-config-v1"
DOMAIN_MESH_ONLY_FORMAT = "triangle-domain-mesh-only-config-v1"
ROUTE_VERSION = "windowed-triangle-preparation-b0-v1"
CHILD_POLICY = "job_object_inheritance_without_breakaway"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_SOURCE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
OUTPUT_RESERVE_BYTES = 96 * 1024
JOB_POLL_MS = 25
JOB_MEMORY_LIMIT_FLAG = 0x00000200
PROCESS_MEMORY_LIMIT_FLAG = 0x00000100
KILL_ON_JOB_CLOSE_FLAG = 0x00002000
JOB_LIMIT_FLAGS = JOB_MEMORY_LIMIT_FLAG | PROCESS_MEMORY_LIMIT_FLAG | KILL_ON_JOB_CLOSE_FLAG
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION = 7
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_MSG_PROCESS_MEMORY_LIMIT = 9
JOB_OBJECT_MSG_JOB_MEMORY_LIMIT = 10
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
ERROR_ABANDONED_WAIT_0 = 735
TERMINATION_CODE = 0xE000B001
_WORKER_BOOTSTRAP = r"""
import importlib.machinery, os, sys, types
from pathlib import Path
root = os.path.abspath(os.environ["HYDRO_B0_PROJECT_ROOT"])
package = types.ModuleType("hydrodynamics")
package.__path__ = [os.path.join(root, "hydrodynamics")]
package.__package__ = "hydrodynamics"
package.__spec__ = importlib.machinery.ModuleSpec("hydrodynamics", loader=None, is_package=True)
package.__spec__.submodule_search_locations = package.__path__
sys.modules["hydrodynamics"] = package
from hydrodynamics.restricted_worker import _worker_child
raise SystemExit(_worker_child(Path(sys.argv[1])))
"""


class RestrictedWorkerError(RuntimeError):
    """A B0 worker request was rejected or could not be safely supervised."""


class ResourceBudgetError(RestrictedWorkerError):
    """A required process/output/scratch budget is invalid or unavailable."""


class SourceIdentityError(RestrictedWorkerError):
    """A source, component, manifest, or mesh identity changed or disagreed."""


class SupervisorStopped(RestrictedWorkerError):
    """The complete B0 operation was cancelled or crossed its wall deadline."""

    def __init__(self, status: str) -> None:
        if status not in {"cancelled", "timed_out"}:
            raise ValueError("supervisor stop status must be cancelled or timed_out")
        self.status = status
        super().__init__(f"restricted preparation stopped: {status}")


class _IOCounters(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64)]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimitInformation), ("IoInfo", _IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _AssociateCompletionPort(ctypes.Structure):
    _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", wintypes.HANDLE)]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


@dataclass(frozen=True)
class WorkerResources:
    max_workers: int
    job_memory_limit_mib: int
    wall_clock_limit_s: float
    output_budget_bytes: int
    scratch_budget_bytes: int
    child_process_policy: str


@dataclass(frozen=True)
class WorkerRunResult:
    status: str
    exit_code: int
    run_id: str
    bundle_path: Path | None
    diagnostic_path: Path | None
    worker_started: bool
    message: str
    worker_receipt: Mapping[str, Any] | None = None


@dataclass
class _WorkerStartState:
    """In-memory fact set only by a successful ResumeThread call."""

    ever_started: bool = False
    process_id: int | None = None
    resume_previous_suspend_count: int | None = None
    mesh_only_route: bool = False

    def record_resume_success(self, *, process_id: int, previous_suspend_count: int) -> None:
        if type(process_id) is not int or process_id <= 0 or previous_suspend_count != 1:
            raise RestrictedWorkerError("worker resume event did not prove the suspended root thread was resumed")
        self.ever_started = True
        self.process_id = process_id
        self.resume_previous_suspend_count = previous_suspend_count


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _short_temp_path(target: Path, *, prefix: str = ".b0-") -> Path:
    return target.with_name(f"{prefix}{uuid.uuid4().hex[:12]}.tmp")


def _windows_path_units(path: Path) -> int:
    return len(str(path).encode("utf-16-le")) // 2


def _strict_positive_int(value: Any, field_name: str, *, minimum: int = 1, maximum: int = (1 << 63) - 1) -> int:
    if type(value) is int:
        normalized = value
    elif type(value) is float and math.isfinite(value) and value.is_integer():
        normalized = int(value)
    else:
        raise ResourceBudgetError(f"{field_name} must be a strict integer in [{minimum}, {maximum}]")
    if normalized < minimum or normalized > maximum:
        raise ResourceBudgetError(f"{field_name} must be a strict integer in [{minimum}, {maximum}]")
    return normalized


def parse_worker_resources(raw: Any) -> WorkerResources:
    if not isinstance(raw, dict):
        raise ResourceBudgetError("resources is required and must be an object")
    required = {"max_workers", "job_memory_limit_mib", "wall_clock_limit_s", "output_budget_bytes",
                "scratch_budget_bytes", "child_process_policy"}
    missing = sorted(required - raw.keys())
    extra = sorted(raw.keys() - required)
    if missing:
        raise ResourceBudgetError(f"required resource budgets are missing: {', '.join(missing)}")
    if extra:
        raise ResourceBudgetError(f"unknown resource budget fields: {', '.join(extra)}")
    workers = _strict_positive_int(raw["max_workers"], "max_workers", minimum=1, maximum=1)
    memory = _strict_positive_int(raw["job_memory_limit_mib"], "job_memory_limit_mib", minimum=64, maximum=65_536)
    wall = raw["wall_clock_limit_s"]
    if isinstance(wall, bool) or not isinstance(wall, (int, float)) or not math.isfinite(float(wall)) or not (0.1 <= float(wall) <= 86_400.0):
        raise ResourceBudgetError("wall_clock_limit_s must be a finite number in [0.1, 86400] seconds")
    output_budget = _strict_positive_int(raw["output_budget_bytes"], "output_budget_bytes", minimum=128 * 1024)
    scratch_budget = _strict_positive_int(raw["scratch_budget_bytes"], "scratch_budget_bytes", minimum=64 * 1024)
    if raw["child_process_policy"] != CHILD_POLICY:
        raise ResourceBudgetError(f"child_process_policy must be exactly {CHILD_POLICY!r}")
    return WorkerResources(workers, memory, float(wall), output_budget, scratch_budget, CHILD_POLICY)


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RestrictedWorkerError(f"{name} must be an object")
    return value


def _safe_relative(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RestrictedWorkerError(f"{name} must be a non-empty project-relative path")
    path = Path(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.is_absolute() or windows.drive or windows.root or ".." in path.parts or ".." in windows.parts:
        raise RestrictedWorkerError(f"{name} must remain under the project root")
    return value


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400)


def validate_config_shape(config: Any) -> dict[str, Any]:
    config = _require_mapping(config, "config")
    expected = {"format", "schema_version", "run_id", "output_root", "resources", "source",
                "mesh", "mapping_config", "crs_alias"}
    missing = sorted(expected - config.keys())
    extra = sorted(config.keys() - expected)
    if missing:
        raise RestrictedWorkerError(f"required config fields are missing: {', '.join(missing)}")
    if extra:
        raise RestrictedWorkerError(f"unknown config fields: {', '.join(extra)}")
    if config.get("format") != FORMAT or config.get("schema_version") != "1.0":
        raise RestrictedWorkerError("unsupported restricted preparation config format/version")
    run_id = config.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id) is None or run_id in {".", ".."}:
        raise RestrictedWorkerError("run_id must be a 1-64 character safe identifier")
    _safe_relative(config.get("output_root"), "output_root")
    source = _require_mapping(config.get("source"), "source")
    if set(source) != {"raster_path", "sha256", "source_manifest_path", "source_manifest_sha256", "expected_stat_identity", "components"}:
        raise RestrictedWorkerError("source fields differ from the frozen B0 config contract")
    for key in ("raster_path", "source_manifest_path"):
        _safe_relative(source.get(key), f"source.{key}")
    for key in ("sha256", "source_manifest_sha256"):
        if not isinstance(source.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", source[key]) is None:
            raise RestrictedWorkerError(f"source.{key} must be a lowercase SHA-256 digest")
    if source["expected_stat_identity"] is not None and not isinstance(source["expected_stat_identity"], dict):
        raise RestrictedWorkerError("source.expected_stat_identity must be an object or null")
    if source["expected_stat_identity"] is not None:
        stat_fields = {"size_bytes", "mtime_ns", "ctime_ns", "device", "file_id"}
        if set(source["expected_stat_identity"]) != stat_fields:
            raise RestrictedWorkerError("source.expected_stat_identity fields differ from the frozen stat contract")
        for name, value in source["expected_stat_identity"].items():
            if type(value) is int and value >= 0:
                continue
            if type(value) is float and math.isfinite(value) and value.is_integer() and value >= 0:
                source["expected_stat_identity"][name] = int(value)
                continue
            else:
                raise RestrictedWorkerError(f"source.expected_stat_identity.{name} must be a strict non-negative integer")
    if not isinstance(source["components"], list):
        raise RestrictedWorkerError("source.components must be an array")
    for index, component in enumerate(source["components"]):
        component = _require_mapping(component, f"source.components[{index}]")
        if set(component) != {"role", "path", "sha256", "required"}:
            raise RestrictedWorkerError(f"source.components[{index}] fields differ from the frozen contract")
        if not isinstance(component["role"], str) or not component["role"]:
            raise RestrictedWorkerError(f"source.components[{index}].role is required")
        _safe_relative(component["path"], f"source.components[{index}].path")
        if not isinstance(component["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", component["sha256"]) is None:
            raise RestrictedWorkerError(f"source.components[{index}].sha256 must be a lowercase SHA-256 digest")
        if type(component["required"]) is not bool:
            raise RestrictedWorkerError(f"source.components[{index}].required must be boolean")
    mesh = _require_mapping(config.get("mesh"), "mesh")
    if set(mesh) != {"path", "sha256"}:
        raise RestrictedWorkerError("mesh fields differ from the frozen B0 config contract")
    _safe_relative(mesh["path"], "mesh.path")
    if not isinstance(mesh["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", mesh["sha256"]) is None:
        raise RestrictedWorkerError("mesh.sha256 must be a lowercase SHA-256 digest")
    if not isinstance(config["mapping_config"], dict):
        raise RestrictedWorkerError("mapping_config must be an object")
    alias = config.get("crs_alias")
    if alias is not None:
        alias = _require_mapping(alias, "crs_alias")
        if set(alias) != {"mode", "target_crs", "expected_source_wkt_sha256"}:
            raise RestrictedWorkerError("crs_alias fields differ from the frozen B0 contract")
        if alias["mode"] != "verify_embedded_epsg4548_xy" or alias["target_crs"] != "EPSG:4548":
            raise RestrictedWorkerError("crs_alias only supports the verified EPSG:4548 XY alias")
        if not isinstance(alias["expected_source_wkt_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", alias["expected_source_wkt_sha256"]) is None:
            raise RestrictedWorkerError("crs_alias.expected_source_wkt_sha256 must be a lowercase SHA-256 digest")
    return config


def validate_route_config_shape(config: Any) -> dict[str, Any]:
    """Dispatch the published B0 contract or the separate synthetic pilot contract."""
    if isinstance(config, dict) and config.get("format") == DOMAIN_MESH_ONLY_FORMAT:
        from .domain_mesh_only import validate_domain_mesh_only_config_shape
        try:
            return validate_domain_mesh_only_config_shape(config)
        except RestrictedWorkerError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise RestrictedWorkerError(f"invalid domain mesh-only config: {exc}") from exc
    if isinstance(config, dict) and config.get("format") == B1_PILOT_FORMAT:
        from .b1_synthetic_pilot import validate_pilot_config_shape
        try:
            return validate_pilot_config_shape(config)
        except RestrictedWorkerError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise RestrictedWorkerError(f"invalid synthetic pilot config: {exc}") from exc
    return validate_config_shape(config)


def _sha256_file(path: Path, *, maximum_bytes: int | None = None,
                 abort_check: Callable[[], str | None] | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            if abort_check is not None:
                reason = abort_check()
                if reason is not None:
                    raise SupervisorStopped(reason)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if maximum_bytes is not None and total > maximum_bytes:
                raise RestrictedWorkerError(f"file exceeds its bounded read limit ({maximum_bytes} bytes): {path}")
            digest.update(chunk)
    return digest.hexdigest(), total


def _stat_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size_bytes": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns),
            "ctime_ns": int(stat.st_ctime_ns), "device": int(stat.st_dev), "file_id": int(stat.st_ino)}


def _resolve_project_path(root: Path, value: str, name: str) -> Path:
    relative = _safe_relative(value, name)
    candidate = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if _is_reparse_or_symlink(current):
            raise RestrictedWorkerError(f"{name} must not traverse a symlink/reparse point")
    # Resolve parents to reject reparse/symlink escapes.  Input symlinks are
    # rejected separately; output paths may be new but may not traverse links.
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise RestrictedWorkerError(f"{name} escapes the project root")
    return resolved


def _tree_bytes(root: Path, *, skip_names: set[str] | None = None) -> int:
    if not root.exists():
        return 0
    total = 0
    stack = [root]
    skip_names = skip_names or set()
    while stack:
        current = stack.pop()
        for entry in os.scandir(current):
            if entry.name in skip_names and current == root:
                continue
            if entry.is_symlink():
                raise RestrictedWorkerError(f"worker output/scratch must not contain symlinks: {entry.path}")
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
    return total


def _write_atomic_json(path: Path, value: Any, *, output_budget: int | None = None,
                       root: Path | None = None, reserve_bytes: int = 0) -> None:
    payload = _json_bytes(value)
    if output_budget is not None and root is not None:
        used = _tree_bytes(root, skip_names={"scratch"})
        if used + len(payload) + reserve_bytes > output_budget:
            raise ResourceBudgetError("supervisor diagnostic metadata would exceed output_budget_bytes")
    temporary = _short_temp_path(path)
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_no_clobber_bytes(path: Path, payload: bytes, *, output_budget: int,
                            root: Path, reserve_bytes: int = 0) -> None:
    used = _tree_bytes(root, skip_names={"scratch"})
    if used + len(payload) + reserve_bytes > output_budget:
        raise ResourceBudgetError("supervisor output file would exceed output_budget_bytes")
    temporary = _short_temp_path(path)
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def _clear_success_scratch(scratch: Path, attempt_root: Path) -> None:
    """Remove only this completed attempt's private scratch directory."""
    scratch = scratch.resolve()
    attempt_root = attempt_root.resolve()
    if scratch.parent != attempt_root or scratch.name != "scratch" or scratch.is_symlink():
        raise RestrictedWorkerError("refusing to clear scratch outside the current attempt")
    directories: list[Path] = [scratch]
    files: list[Path] = []
    index = 0
    while index < len(directories):
        directory = directories[index]
        index += 1
        for entry in os.scandir(directory):
            path = Path(entry.path)
            if entry.is_symlink():
                raise RestrictedWorkerError(f"scratch contains an unexpected symlink: {path}")
            if entry.is_dir(follow_symlinks=False):
                directories.append(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
            else:
                raise RestrictedWorkerError(f"scratch contains a non-regular item: {path}")
    for path in files:
        path.unlink()
    for directory in reversed(directories):
        directory.rmdir()


def _module_code_hashes(project_root: Path, *, pilot_route: bool = False,
                        domain_mesh_only_route: bool = False) -> dict[str, str]:
    names = ("hydrodynamics/restricted_worker.py", "hydrodynamics/prepared_terrain.py",
             "hydrodynamics/preparation_crs.py", "hydrodynamics/terrain_windowed_mapping.py",
             "hydrodynamics/dem_contract.py", "hydrodynamics/triangular_mesh.py",
             "hydrodynamics/mesh_preparation.py")
    if pilot_route:
        names = (*names, "hydrodynamics/b1_synthetic_pilot.py", "hydrodynamics/mesh_generation.py")
    if domain_mesh_only_route:
        package_root = project_root / "hydrodynamics"
        if not package_root.is_dir() or package_root.is_symlink():
            raise RestrictedWorkerError("hydrodynamics source package is missing or a symlink")
        # Importing hydrodynamics executes its package initializer. Bind every
        # Python source file in that package so the mesh-only worker's import
        # graph cannot drift between supervisor check and worker execution.
        names = tuple(sorted(path.relative_to(project_root).as_posix()
                             for path in package_root.rglob("*.py")
                             if path.is_file() and "__pycache__" not in path.parts))
    result: dict[str, str] = {}
    for name in names:
        path = project_root / name
        if not path.is_file() or path.is_symlink():
            raise RestrictedWorkerError(f"required route source file is missing or a symlink: {name}")
        result[name] = _sha256_file(path)[0]
    return result


def _win_api() -> tuple[Any, Any]:
    if os.name != "nt":
        raise ResourceBudgetError("Windows Job Object is unavailable; the B0 worker refuses to start without an OS memory limit")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreateIoCompletionPort.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD]
    kernel32.CreateIoCompletionPort.restype = wintypes.HANDLE
    kernel32.GetQueuedCompletionStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD]
    kernel32.GetQueuedCompletionStatus.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                           wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                                   ctypes.c_void_p, ctypes.c_size_t,
                                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                        ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                                        ctypes.POINTER(_ProcessInformation)]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    return kernel32, ctypes.WinDLL("kernel32", use_last_error=True)


def _last_error(message: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{message}: {ctypes.FormatError(code).strip()}")


class _NativeChildProcess:
    """Small handle wrapper that retains the suspended primary thread handle."""

    def __init__(self, process_handle: Any, thread_handle: Any, process_id: int,
                 command: list[str], *, assigned_job_at_creation: bool) -> None:
        self.handle = process_handle
        self.thread_handle = thread_handle
        self.pid = int(process_id)
        self.command = command
        self.assigned_job_at_creation = assigned_job_at_creation

    def poll(self) -> int | None:
        kernel32, _ = _win_api()
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(wintypes.HANDLE(int(self.handle)), ctypes.byref(code)):
            raise _last_error("GetExitCodeProcess failed")
        return None if code.value == 259 else int(code.value)

    def wait(self, timeout: float | None = None) -> int:
        kernel32, _ = _win_api()
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, min(0xFFFFFFFE, int(timeout * 1000)))
        result = kernel32.WaitForSingleObject(wintypes.HANDLE(int(self.handle)), milliseconds)
        if result == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.command, timeout)
        if result != WAIT_OBJECT_0:
            raise _last_error("WaitForSingleObject(process) failed")
        code = self.poll()
        if code is None:
            raise RestrictedWorkerError("worker process signaled but still reports STILL_ACTIVE")
        return code

    def kill(self) -> None:
        kernel32, _ = _win_api()
        if not kernel32.TerminateProcess(wintypes.HANDLE(int(self.handle)), TERMINATION_CODE):
            error = ctypes.get_last_error()
            if error != 5:  # ERROR_ACCESS_DENIED is possible after the job already terminated it.
                raise _last_error("TerminateProcess fallback failed")


def _create_suspended_process(command: list[str], *, cwd: Path, environment: Mapping[str, str],
                              job: Any) -> _NativeChildProcess:
    """Create a suspended child with its Job Object attached inside CreateProcess."""
    if not command or any(not isinstance(value, str) for value in command):
        raise RestrictedWorkerError("worker command must be a non-empty string argument list")
    kernel32, _ = _win_api()
    size = ctypes.c_size_t()
    # InitializeProcThreadAttributeList documents this sizing call as a
    # deliberate failure that returns the required byte count.
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if size.value <= 0:
        raise _last_error("could not size the process attribute list")
    attribute_buffer = ctypes.create_string_buffer(size.value)
    attribute_pointer = ctypes.cast(attribute_buffer, ctypes.c_void_p)
    if not kernel32.InitializeProcThreadAttributeList(attribute_pointer, 1, 0, ctypes.byref(size)):
        raise _last_error("InitializeProcThreadAttributeList failed")
    try:
        job_list = (wintypes.HANDLE * 1)(wintypes.HANDLE(int(job)))
        if not kernel32.UpdateProcThreadAttribute(
            attribute_pointer, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST,
            ctypes.cast(job_list, ctypes.c_void_p), ctypes.sizeof(job_list), None, None,
        ):
            raise _last_error("UpdateProcThreadAttribute(JOB_LIST) failed")
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
        startup.StartupInfo.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
        startup.StartupInfo.wShowWindow = 0  # SW_HIDE
        startup.lpAttributeList = attribute_pointer

        env_items: list[str] = []
        for key, value in environment.items():
            if (not isinstance(key, str) or not isinstance(value, str) or not key
                    or "=" in key or "\0" in key or "\0" in value):
                raise RestrictedWorkerError("worker environment contains an invalid variable name/value")
            env_items.append(f"{key}={value}")
        env_block = "\0".join(sorted(env_items, key=lambda item: item.split("=", 1)[0].casefold())) + "\0\0"
        environment_buffer = ctypes.create_unicode_buffer(env_block)
        command_buffer = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        process_info = _ProcessInformation()
        flags = CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
        if not kernel32.CreateProcessW(
            command[0], command_buffer, None, None, False, flags,
            ctypes.cast(environment_buffer, ctypes.c_void_p), str(cwd),
            ctypes.cast(ctypes.byref(startup), ctypes.c_void_p), ctypes.byref(process_info),
        ):
            raise _last_error("CreateProcessW with PROC_THREAD_ATTRIBUTE_JOB_LIST failed")
    finally:
        kernel32.DeleteProcThreadAttributeList(attribute_pointer)

    membership = wintypes.BOOL()
    if not kernel32.IsProcessInJob(process_info.hProcess, wintypes.HANDLE(int(job)), ctypes.byref(membership)) or not membership.value:
        # The primary thread is still suspended.  If verification fails, kill
        # this exact process handle before returning; never leave an unassigned
        # child behind and never fall back to post-create assignment.
        kernel32.TerminateProcess(process_info.hProcess, TERMINATION_CODE)
        kernel32.WaitForSingleObject(process_info.hProcess, 5000)
        kernel32.CloseHandle(process_info.hThread)
        kernel32.CloseHandle(process_info.hProcess)
        raise RestrictedWorkerError("created suspended worker is not in the configured Job Object")
    child = _NativeChildProcess(process_info.hProcess, process_info.hThread,
                                int(process_info.dwProcessId), command,
                                assigned_job_at_creation=True)
    child.is_member_before_resume = True
    return child


def _create_job(memory_limit_bytes: int) -> tuple[Any, int]:
    kernel32, _ = _win_api()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise _last_error("CreateJobObjectW failed")
    port = None
    try:
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = JOB_LIMIT_FLAGS
        info.ProcessMemoryLimit = memory_limit_bytes
        info.JobMemoryLimit = memory_limit_bytes
        if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                ctypes.byref(info), ctypes.sizeof(info)):
            raise _last_error("SetInformationJobObject(ExtendedLimitInformation) failed")
        invalid_handle = ctypes.c_void_p(-1).value
        port = kernel32.CreateIoCompletionPort(wintypes.HANDLE(invalid_handle), None, 0, 1)
        if not port:
            raise _last_error("CreateIOCompletionPort failed")
        association = _AssociateCompletionPort(ctypes.c_void_p(0xB0B0), port)
        if not kernel32.SetInformationJobObject(job, JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION,
                                                ctypes.byref(association), ctypes.sizeof(association)):
            raise _last_error("SetInformationJobObject(CompletionPort) failed")
        return job, port
    except BaseException:
        if port:
            kernel32.CloseHandle(port)
        kernel32.CloseHandle(job)
        raise


def _job_counters(job: Any) -> tuple[int, int, int]:
    kernel32, _ = _win_api()
    extended = _ExtendedLimitInformation()
    returned = wintypes.DWORD()
    if not kernel32.QueryInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                              ctypes.byref(extended), ctypes.sizeof(extended), ctypes.byref(returned)):
        raise _last_error("QueryInformationJobObject(ExtendedLimitInformation) failed")
    accounting = _BasicAccountingInformation()
    if not kernel32.QueryInformationJobObject(job, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                                              ctypes.byref(accounting), ctypes.sizeof(accounting), ctypes.byref(returned)):
        raise _last_error("QueryInformationJobObject(BasicAccountingInformation) failed")
    return int(extended.PeakJobMemoryUsed), int(extended.PeakProcessMemoryUsed), int(accounting.ActiveProcesses)


def _drain_job_events(port: Any) -> list[tuple[int, int]]:
    kernel32, _ = _win_api()
    events: list[tuple[int, int]] = []
    while True:
        message = wintypes.DWORD()
        key = ctypes.c_size_t()
        overlapped = ctypes.c_void_p()
        ok = kernel32.GetQueuedCompletionStatus(port, ctypes.byref(message), ctypes.byref(key),
                                                ctypes.byref(overlapped), 0)
        if not ok:
            error = ctypes.get_last_error()
            if error in (WAIT_TIMEOUT, ERROR_ABANDONED_WAIT_0):
                break
            # A zero timeout with an empty queue returns ERROR_TIMEOUT (258).
            if error == 258:
                break
            raise _last_error("GetQueuedCompletionStatus failed")
        if key.value == 0xB0B0:
            events.append((int(message.value), int(overlapped.value or 0)))
    return events


def _terminate_job(kernel32: Any, job: Any, process: _NativeChildProcess, code: int) -> None:
    kernel32.TerminateJobObject(job, code)
    try:
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=2.0)
        except Exception:
            pass


def _run_contained_command(command: list[str], *, cwd: Path, environment: Mapping[str, str],
                           memory_limit_mib: int, wall_clock_limit_s: float,
                           scratch_directory: Path, output_directory: Path,
                           scratch_budget_bytes: int, output_budget_bytes: int,
                           worker_start_state: _WorkerStartState,
                           cancel_event: threading.Event | None = None,
                           on_created_suspended: Callable[[Mapping[str, Any]], None] | None = None,
                           on_job_processes: Callable[[list[int], int], None] | None = None) -> dict[str, Any]:
    """Start one suspended process under an aggregate non-breakaway Job Object.

    Private entry point used by the fixed B0 worker route and controlled tests.
    Production config cannot supply arbitrary commands or child executables.
    """
    if os.name != "nt":
        raise ResourceBudgetError("Windows Job Object is unavailable; no process was started")
    if not command or any(not isinstance(item, str) for item in command):
        raise RestrictedWorkerError("worker command must be a non-empty argument list")
    kernel32, _ = _win_api()
    memory_bytes = memory_limit_mib * 1024 * 1024
    job, port = _create_job(memory_bytes)
    process: _NativeChildProcess | None = None
    started = time.monotonic()
    memory_messages: list[int] = []
    process_ids: set[int] = set()
    reported_process_ids: set[int] = set()
    peak_scratch_bytes = 0
    peak_output_bytes = 0
    termination_reason: str | None = None
    root_exit_code: int | None = None
    try:
        process = _create_suspended_process(command, cwd=cwd, environment=environment, job=job)
        if process.assigned_job_at_creation is not True or process.is_member_before_resume is not True:
            _terminate_job(kernel32, job, process, TERMINATION_CODE)
            raise RestrictedWorkerError("worker job membership was not established atomically at process creation")
        thread_handle = process.thread_handle
        if thread_handle is None or int(thread_handle) == 0:
            _terminate_job(kernel32, job, process, TERMINATION_CODE)
            raise RestrictedWorkerError("suspended worker thread handle is unavailable")
        process_ids.add(int(process.pid))
        if on_created_suspended is not None:
            on_created_suspended({"process_id": process.pid,
                                  "job_list_assigned_at_create": process.assigned_job_at_creation,
                                  "process_in_job_before_resume": process.is_member_before_resume,
                                  "initial_thread_suspended": True})
        previous_suspend_count = kernel32.ResumeThread(wintypes.HANDLE(int(thread_handle)))
        if previous_suspend_count == 0xFFFFFFFF:
            error = _last_error("ResumeThread failed")
            _terminate_job(kernel32, job, process, TERMINATION_CODE)
            raise RestrictedWorkerError(str(error)) from error
        if previous_suspend_count != 1:
            _terminate_job(kernel32, job, process, TERMINATION_CODE)
            raise RestrictedWorkerError(f"worker primary thread had unexpected suspend count {previous_suspend_count}")
        worker_start_state.record_resume_success(process_id=int(process.pid),
                                                 previous_suspend_count=int(previous_suspend_count))
        _write_no_clobber_bytes(
            scratch_directory / "worker-resumed.json",
            _json_bytes({"format": "triangle-worker-resumed-v2", "resume_succeeded": True,
                         "resume_previous_suspend_count": worker_start_state.resume_previous_suspend_count,
                         "process_id": worker_start_state.process_id,
                         "process_ids": [worker_start_state.process_id], "recorded_at": _now()}),
            output_budget=scratch_budget_bytes, root=scratch_directory,
        )
        kernel32.CloseHandle(wintypes.HANDLE(int(thread_handle)))
        process.thread_handle = None

        while True:
            if cancel_event is not None and cancel_event.is_set():
                termination_reason = "cancelled"
                _terminate_job(kernel32, job, process, TERMINATION_CODE)
                break
            elapsed = time.monotonic() - started
            if elapsed >= wall_clock_limit_s:
                termination_reason = "timed_out"
                _terminate_job(kernel32, job, process, TERMINATION_CODE)
                break
            try:
                scratch_bytes = _tree_bytes(scratch_directory)
                output_bytes = _tree_bytes(output_directory, skip_names={"scratch"})
            except RestrictedWorkerError:
                termination_reason = "resource_rejected"
                _terminate_job(kernel32, job, process, TERMINATION_CODE)
                break
            peak_scratch_bytes = max(peak_scratch_bytes, scratch_bytes)
            peak_output_bytes = max(peak_output_bytes, output_bytes)
            if scratch_bytes > scratch_budget_bytes or output_bytes > output_budget_bytes:
                termination_reason = "resource_rejected"
                _terminate_job(kernel32, job, process, TERMINATION_CODE)
                break
            events = _drain_job_events(port)
            messages = [message for message, _ in events]
            process_ids.update(pid for message, pid in events if message == 6 and pid > 0)
            if JOB_OBJECT_MSG_JOB_MEMORY_LIMIT in messages or JOB_OBJECT_MSG_PROCESS_MEMORY_LIMIT in messages:
                memory_messages.extend(messages)
                termination_reason = "resource_rejected"
                _terminate_job(kernel32, job, process, TERMINATION_CODE)
                break
            counters = _job_counters(job)
            if on_job_processes is not None and process_ids != reported_process_ids:
                on_job_processes(sorted(process_ids), counters[2])
                reported_process_ids = set(process_ids)
            if counters[0] >= memory_bytes or counters[1] >= memory_bytes:
                termination_reason = "resource_rejected"
                _terminate_job(kernel32, job, process, TERMINATION_CODE)
                break
            root_exit = process.poll()
            if root_exit is not None and root_exit_code is None:
                root_exit_code = root_exit
            # Windows Python launchers may start the actual interpreter as a
            # child and exit first.  Wait for the whole job tree to reach zero;
            # the bounded worker receipt is the success proof for the payload.
            if counters[2] == 0:
                break
            time.sleep(0.01)

        if root_exit_code is None:
            root_exit_code = process.poll()
        if root_exit_code is None and termination_reason is None:
            termination_reason = "supervisor_failed"
        # Job memory notifications are drained after termination as well.
        final_events = _drain_job_events(port)
        memory_messages.extend(message for message, _ in final_events)
        process_ids.update(pid for message, pid in final_events if message == 6 and pid > 0)
        peak_job_bytes, peak_process_bytes, active = _job_counters(job)
        if (peak_job_bytes > memory_bytes or peak_process_bytes > memory_bytes) and termination_reason is None:
            termination_reason = "resource_rejected"
        if active != 0:
            _terminate_job(kernel32, job, process, TERMINATION_CODE)
            deadline = time.monotonic() + 3.0
            while active != 0 and time.monotonic() < deadline:
                time.sleep(0.01)
                peak_job_bytes, peak_process_bytes, active = _job_counters(job)
            if active != 0:
                termination_reason = termination_reason or "supervisor_failed"
        elapsed_total = time.monotonic() - started
        return {
            "os_limit_enforced": True,
            "os_mechanism": "Windows Job Object",
            "max_workers": 1,
            "process_memory_limit_bytes": memory_bytes,
            "job_memory_limit_bytes": memory_bytes,
            "wall_clock_limit_s": wall_clock_limit_s,
            "scratch_budget_bytes": scratch_budget_bytes,
            "output_budget_bytes": output_budget_bytes,
            "scratch_peak_bytes": int(peak_scratch_bytes),
            "output_peak_bytes": int(peak_output_bytes),
            "limit_installed_before_resume": True,
            "job_list_assigned_at_create": True,
            "post_create_assignment_window": False,
            "process_in_job_before_resume": True,
            "creation_suspended": True,
            "breakaway_allowed": False,
            "kill_on_job_close": True,
            "job_active_processes_at_exit": int(active),
            "job_peak_commit_bytes": int(peak_job_bytes),
            "process_peak_commit_bytes": int(peak_process_bytes),
            "memory_limit_messages": sorted(set(memory_messages)),
            "termination_reason": termination_reason,
            "root_exit_code": root_exit_code,
            "elapsed_seconds": round(elapsed_total, 6),
            "process_id": int(process.pid),
            "job_process_ids_observed": sorted(process_ids),
            "child_process_policy": CHILD_POLICY,
        }
    except BaseException:
        if process is not None:
            try:
                _terminate_job(kernel32, job, process, TERMINATION_CODE)
            except Exception:
                pass
        raise
    finally:
        # KILL_ON_JOB_CLOSE is the final parent-exit/exception guard.  The
        # completion port is private to this supervisor and not inherited.
        if process is not None:
            if process.thread_handle:
                try:
                    kernel32.CloseHandle(wintypes.HANDLE(int(process.thread_handle)))
                except Exception:
                    pass
            try:
                kernel32.CloseHandle(wintypes.HANDLE(int(process.handle)))
            except Exception:
                pass
        kernel32.CloseHandle(port)
        kernel32.CloseHandle(job)


def _source_component_snapshot(config: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for component in config["source"]["components"]:
        path = _resolve_project_path(root, component["path"], f"source component {component['role']}")
        if not path.exists():
            if component["required"]:
                raise FileNotFoundError(f"required source component is missing: {component['path']}")
            records.append({"role": component["role"], "path": component["path"],
                            "expected_sha256": component["sha256"], "present": False,
                            "current_stat": None, "actual_sha256_checked": False})
            continue
        if path.is_symlink() or not path.is_file():
            raise RestrictedWorkerError(f"source component must be a regular non-symlink file: {path}")
        before = _stat_identity(path)
        actual_hash, actual_bytes = _sha256_file(path, maximum_bytes=MAX_SOURCE_MANIFEST_BYTES)
        after = _stat_identity(path)
        if before != after:
            raise SourceIdentityError(f"source component changed while it was hashed: {component['path']}")
        if actual_hash != component["sha256"]:
            raise SourceIdentityError(f"source component SHA-256 differs: {component['path']}")
        records.append({"role": component["role"], "path": component["path"],
                        "expected_sha256": component["sha256"], "actual_sha256": actual_hash,
                        "bytes": actual_bytes, "present": True, "current_stat": before,
                        "actual_sha256_checked": True})
    return records


def _verify_source_manifest_bindings(manifest: Any, source: dict[str, Any], mesh: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise RestrictedWorkerError("source manifest root must be an object")
    raster_path = source["raster_path"].replace("\\", "/")
    raster_sha = source["sha256"]
    raster_record: dict[str, Any] | None = None
    if manifest.get("raster_path") is not None or manifest.get("raster_sha256") is not None:
        manifest_raster_path = manifest.get("raster_path")
        if (not isinstance(manifest_raster_path, str)
                or manifest_raster_path.replace("\\", "/") != raster_path
                or manifest.get("raster_sha256") != raster_sha):
            raise SourceIdentityError("source manifest raster path/SHA differs from the worker config")
        raster_record = {"path": manifest["raster_path"], "sha256": manifest["raster_sha256"],
                         "bytes": manifest.get("raster_bytes"), "mtime_ns": manifest.get("raster_mtime_ns")}
    else:
        input_files = manifest.get("input_files")
        if not isinstance(input_files, list):
            raise RestrictedWorkerError("source manifest has no supported raster identity record")
        matches = [item for item in input_files if isinstance(item, dict)
                   and isinstance(item.get("path"), str)
                   and item["path"].replace("\\", "/") == raster_path]
        if len(matches) != 1 or matches[0].get("sha256") != raster_sha:
            raise SourceIdentityError("source manifest does not contain one matching raster path/SHA record")
        raster_record = matches[0]
    expected_stat = source.get("expected_stat_identity")
    if expected_stat is not None:
        if raster_record.get("bytes") is not None and raster_record["bytes"] != expected_stat["size_bytes"]:
            raise SourceIdentityError("source manifest raster byte count differs from the frozen stat identity")
        if raster_record.get("mtime_ns") is not None and raster_record["mtime_ns"] != expected_stat["mtime_ns"]:
            raise SourceIdentityError("source manifest raster mtime differs from the frozen stat identity")
    input_files = manifest.get("input_files")
    checked_components = 0
    if isinstance(input_files, list):
        by_path = {item.get("path", "").replace("\\", "/"): item for item in input_files
                   if isinstance(item, dict) and isinstance(item.get("path"), str)}
        for component in source["components"]:
            record = by_path.get(component["path"].replace("\\", "/"))
            if record is not None:
                if record.get("sha256") != component["sha256"]:
                    raise SourceIdentityError(f"source component SHA differs from manifest: {component['path']}")
                checked_components += 1
    if manifest.get("dem_full_sha256") not in (None, raster_sha):
        raise SourceIdentityError("source manifest dem_full_sha256 differs from the worker raster SHA")
    return {"raster_path": raster_path, "raster_sha256": raster_sha,
            "raster_record_sha256_matches": True, "manifest_components_cross_checked": checked_components,
            "mesh_manifest_identity_present": manifest.get("mesh_sha256") == mesh.get("sha256")
                if manifest.get("mesh_sha256") is not None else False}


class _CRSAliasReader:
    def __init__(self, reader: Any, alias: dict[str, Any] | None) -> None:
        self._reader = reader
        self._alias = alias
        self.original_crs: str | None = None
        self.original_metadata: Any = None
        self.evidence: dict[str, Any] | None = None
        self._proved = False

    def _metadata(self, metadata: Any) -> Any:
        from dataclasses import replace
        if self.original_crs is None:
            self.original_crs = metadata.crs
            self.original_metadata = metadata
        if self._alias is None:
            return metadata
        if not self._proved:
            from .preparation_crs import prove_epsg4548_xy_alias
            self.evidence = prove_epsg4548_xy_alias(
                metadata.crs or "", expected_wkt_sha256=self._alias["expected_source_wkt_sha256"]
            )
            if self.evidence["target_crs"] != self._alias["target_crs"]:
                raise RestrictedWorkerError("runtime CRS proof does not match the configured target alias")
            self._proved = True
        return replace(metadata, crs=self._alias["target_crs"])

    def read_metadata(self, source: str | Path) -> Any:
        return self._metadata(self._reader.read_metadata(source))

    def iter_windows_selected(self, source: str | Path, **kwargs: Any) -> Any:
        from dataclasses import replace
        include_window = kwargs.get("include_window")
        if not callable(include_window):
            raise RestrictedWorkerError("raw selected-window predicate is required")
        child_kwargs = dict(kwargs)
        child_kwargs["include_window"] = lambda metadata, window: include_window(self._metadata(metadata), window)
        base_iterator = iter(self._reader.iter_windows_selected(source, **child_kwargs))
        try:
            while True:
                try:
                    item = next(base_iterator)
                except StopIteration:
                    return
                window, raw_chunk = item
                metadata = self._metadata(raw_chunk.metadata)
                aliased_chunk = replace(raw_chunk, metadata=metadata)
                # The wrapped iterator frame must not keep the previous raw tile
                # alive while the base reader allocates the next selected tile.
                del raw_chunk, metadata, item
                yield window, aliased_chunk
                del window, aliased_chunk
        finally:
            close = getattr(base_iterator, "close", None)
            if callable(close):
                close()


def _load_mesh(mesh_path: Path, expected_sha256: str) -> tuple[Any, str, dict[str, int]]:
    before = _stat_identity(mesh_path)
    actual_sha, _ = _sha256_file(mesh_path)
    if actual_sha != expected_sha256:
        raise SourceIdentityError("mesh file SHA-256 differs from its declared input identity")
    try:
        payload = json.loads(mesh_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestrictedWorkerError(f"mesh JSON could not be read: {exc}") from exc
    after = _stat_identity(mesh_path)
    if before != after:
        raise SourceIdentityError("mesh file changed while it was read")
    if not isinstance(payload, dict) or payload.get("format") != "triangle-mesh-xy-json-v1":
        raise RestrictedWorkerError("mesh input format is not triangle-mesh-xy-json-v1")
    expected_keys = {"format", "mesh_id", "coordinate_system", "node_xy", "face_nodes", "face_id",
                     "geometry_tolerance_m", "minimum_face_area_m2"}
    if set(payload) != expected_keys:
        raise RestrictedWorkerError("mesh JSON keys differ from the frozen preparation contract")
    from .triangular_mesh import TriangularMesh
    mesh = TriangularMesh(
        node_xy=payload["node_xy"], face_nodes=payload["face_nodes"], face_id=payload["face_id"],
        coordinate_system=payload["coordinate_system"], mesh_id=payload["mesh_id"],
        geometry_tolerance_m=payload["geometry_tolerance_m"],
        minimum_face_area_m2=payload["minimum_face_area_m2"],
    )
    return mesh, actual_sha, before


def _worker_run_mapping(control: dict[str, Any], config: dict[str, Any], root: Path, *,
                        body_metadata: Mapping[str, Any] | None = None,
                        route_version: str | None = None,
                        body_path_overrides: Mapping[str, str] | None = None,
                        prepared_body_validator: Callable[[Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    source = config["source"]
    source_path = _resolve_project_path(root, source["raster_path"], "source.raster_path")
    if source_path.is_symlink() or not source_path.is_file():
        raise FileNotFoundError(f"source raster is missing or not a regular file: {source_path}")
    source_before = _stat_identity(source_path)
    expected_stat = source.get("expected_stat_identity")
    if expected_stat is not None and source_before != expected_stat:
        raise SourceIdentityError("source raster stat identity differs from its frozen configuration")

    manifest_path = _resolve_project_path(root, source["source_manifest_path"], "source.source_manifest_path")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FileNotFoundError(f"source manifest is missing or not a regular file: {manifest_path}")
    manifest_hash, manifest_bytes = _sha256_file(manifest_path, maximum_bytes=MAX_SOURCE_MANIFEST_BYTES)
    if manifest_hash != source["source_manifest_sha256"]:
        raise SourceIdentityError("source manifest SHA-256 differs from the configured identity")
    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestrictedWorkerError(f"source manifest JSON is malformed: {exc}") from exc
    source_manifest_binding = _verify_source_manifest_bindings(manifest_payload, source, config["mesh"])
    manifest_before = _stat_identity(manifest_path)
    if _sha256_file(manifest_path, maximum_bytes=MAX_SOURCE_MANIFEST_BYTES)[0] != manifest_hash:
        raise SourceIdentityError("source manifest changed while its identity was captured")
    vertical_evidence_path = root / "docs/external_evidence_contract.md"
    vertical_hash_before = _sha256_file(vertical_evidence_path, maximum_bytes=4 * 1024 * 1024)[0]
    if vertical_hash_before != control["vertical_evidence_sha256"]:
        raise SourceIdentityError("vertical relation evidence differs before preparation")
    component_records = _source_component_snapshot(config, root)

    mesh_path = _resolve_project_path(root, config["mesh"]["path"], "mesh.path")
    if mesh_path.is_symlink() or not mesh_path.is_file():
        raise FileNotFoundError(f"mesh input is missing or not a regular file: {mesh_path}")
    mesh, mesh_sha, mesh_stat = _load_mesh(mesh_path, config["mesh"]["sha256"])

    from .dem_contract import GeoTIFFDEMReader
    from .terrain_windowed_mapping import (
        WindowedTriangleMappingConfig, map_triangular_mesh_from_windows, mesh_fingerprint_sha256,
    )
    from .prepared_terrain import write_prepared_face_artifacts

    mapping_config = WindowedTriangleMappingConfig.model_validate(config["mapping_config"])
    reader = _CRSAliasReader(GeoTIFFDEMReader(), config["crs_alias"])
    # Keep all optional GIS/native libraries inside the already constrained
    # worker.  The environment also disables PAM sidecar writes and makes the
    # GeoTIFF's embedded georeference authoritative.
    import rasterio
    with rasterio.Env(GDAL_PAM_ENABLED="NO", GDAL_GEOREF_SOURCES="INTERNAL"):
        terrain, report = map_triangular_mesh_from_windows(
            reader, source_path, mesh, source_sha256=source["sha256"],
            config=mapping_config, config_base=Path(control["config_base"]),
        )
    source_after = _stat_identity(source_path)
    if source_before != source_after:
        raise RestrictedWorkerError("source raster identity changed while metadata/windows were read")
    component_records_after = _source_component_snapshot(config, root)
    manifest_after = _stat_identity(manifest_path)
    manifest_hash_after, _ = _sha256_file(manifest_path, maximum_bytes=MAX_SOURCE_MANIFEST_BYTES)
    if manifest_before != manifest_after or manifest_hash_after != manifest_hash:
        raise SourceIdentityError("source manifest changed while mapping windows were read")
    for before, after in zip(component_records, component_records_after):
        if before != after:
            raise SourceIdentityError(f"source component changed during mapping: {before.get('path')}")
    if reader.original_crs is None:
        raise RestrictedWorkerError("source CRS metadata was not available")

    if reader.evidence is not None:
        report["source_crs"] = reader.original_crs
        report["preparation_coordinate_system_alias"] = reader.evidence
    elif reader.original_crs != mesh.coordinate_system:
        raise RestrictedWorkerError("raw source CRS text and mesh coordinate system differ without a verified alias")

    code_hashes = control["code_hashes"]
    source_crs_wkt_sha = hashlib.sha256(reader.original_crs.encode("utf-8")).hexdigest()
    vertical_hash_after = _sha256_file(vertical_evidence_path, maximum_bytes=4 * 1024 * 1024)[0]
    if vertical_hash_after != control["vertical_evidence_sha256"]:
        raise SourceIdentityError("vertical relation evidence changed during preparation")
    vertical_relation = {
        "evidence_path": "docs/external_evidence_contract.md",
        "evidence_sha256": control["vertical_evidence_sha256"],
        "source_datum": "大沽高程",
        "target_datum": "1985国家高程基准",
        "target_minus_source_m": -1.56,
        "direction": "target = source - 1.56 m",
        "scope": "下游东淀-文安洼区域",
        "applied": False,
        "proves_dem_source_datum": False,
    }
    raw_vertical_unit = reader.original_metadata.vertical_unit
    raw_vertical_datum = reader.original_metadata.vertical_datum
    vertical_unit_source = report["vertical_unit_source"]
    vertical_datum_source = report["vertical_datum_source"]
    verified_vertical_evidence = report.get("source_evidence", {})
    if raw_vertical_unit is not None and vertical_unit_source != "dem_metadata":
        raise RestrictedWorkerError("reported vertical-unit provenance conflicts with non-null raster metadata")
    if raw_vertical_unit is None and vertical_unit_source == "dem_metadata":
        raise RestrictedWorkerError("reported DEM-metadata unit is missing from the raw raster metadata")
    if vertical_unit_source == "config_declaration" and "source_vertical_unit" not in verified_vertical_evidence:
        raise RestrictedWorkerError("declared vertical metre unit lacks verified evidence")
    if raw_vertical_datum is None and vertical_datum_source == "dem_metadata":
        raise RestrictedWorkerError("reported DEM-metadata datum is missing from the raw raster metadata")
    if vertical_datum_source == "config_declaration" and "source_vertical_datum" not in verified_vertical_evidence:
        raise RestrictedWorkerError("declared source datum lacks verified evidence")
    unit_status = ("source_metadata_metre" if vertical_unit_source == "dem_metadata"
                   else "source_declared_metre_with_verified_evidence")
    datum_status = ("source_metadata_datum" if vertical_datum_source == "dem_metadata"
                    else "source_declared_datum_with_verified_evidence" if vertical_datum_source == "config_declaration"
                    else "unknown_source_datum_unattested")
    body = {
        "format": "triangle-prepared-terrain-b0-v1",
        "schema_version": "1.0",
        "run_id": config["run_id"],
        "status": "diagnostic_only",
        "route_version": route_version or ROUTE_VERSION,
        "preparation_only": True,
        "simulation_result": False,
        "production_approved": False,
        "engineering_ready": False,
        "coverage_status": "policy_pending_diagnostic_only" if mapping_config.coverage_policy == "diagnostic_only" else "preparation_policy_check_only",
        "coverage_policy": mapping_config.coverage_policy,
        "coverage_policy_evidence_sha256": mapping_config.coverage_policy_evidence_sha256,
        "coverage_policy_pending": mapping_config.coverage_policy == "diagnostic_only",
        "face_count": mesh.n_face,
        "bindings": {
            "config_sha256": control["config_sha256"],
            "code_sha256": hashlib.sha256(_json_bytes(code_hashes)).hexdigest(),
            "source_sha256": source["sha256"],
            "source_manifest_sha256": manifest_hash,
            "mesh_sha256": mesh_sha,
            "mesh_fingerprint_sha256": mesh_fingerprint_sha256(mesh),
            "terrain_sha256": None,
            "coverage_evidence_sha256": mapping_config.coverage_policy_evidence_sha256,
        },
        "code": {"route_files_sha256": code_hashes},
        "source": {
            "raster_path": source["raster_path"],
            "raster_sha256": source["sha256"],
            "raster_sha_kind": "inherited_from_prior_full_hash_verification; not recomputed in this worker",
            "raster_current_stat_before": source_before,
            "raster_current_stat_after": source_after,
            "raster_stat_protection": "path/file identity and metadata are compared during the read; this does not prove bytes equal the inherited digest",
            "source_manifest_path": source["source_manifest_path"],
            "source_manifest_sha256_actual": manifest_hash,
            "source_manifest_bytes": manifest_bytes,
            "source_manifest_binding": source_manifest_binding,
            "components": component_records,
            "metadata": {
                "shape_yx": report["source_shape_yx"], "pixel_count": report["source_pixel_count"],
                "dtype": reader.original_metadata.dtype, "nodata_value": report["source_nodata_value"],
                "horizontal_unit": report["source_horizontal_unit"], "vertical_unit": report["source_vertical_unit"],
                "vertical_datum": report["source_vertical_datum"], "area_or_point": report["source_area_or_point"],
                "vertical_unit_source": vertical_unit_source,
                "vertical_datum_source": vertical_datum_source,
                "transform": report["source_transform"], "original_wkt": reader.original_crs,
                "original_wkt_sha256": source_crs_wkt_sha,
            },
            "coordinate_system_alias_evidence": reader.evidence,
            "vertical_relation_context": vertical_relation,
            "terrain_source_vertical_datum_status": datum_status,
            "terrain_vertical_unit_status": unit_status,
            "source_vertical_unit_evidence": verified_vertical_evidence.get("source_vertical_unit"),
            "source_vertical_datum_evidence": verified_vertical_evidence.get("source_vertical_datum"),
            "terrain_elevation_adjustment_m": 0.0,
        },
        "mesh": {
            "path": config["mesh"]["path"], "sha256": mesh_sha,
            "stat_identity": mesh_stat, "mesh_id": mesh.mesh_id,
            "coordinate_system": mesh.coordinate_system, "node_count": mesh.n_node,
            "mesh_fingerprint_sha256": mesh_fingerprint_sha256(mesh),
            "face_count": mesh.n_face, "face_id_sequence_sha256": None,
            "geometry_tolerance_m": float(mesh.geometry_tolerance_m),
            "minimum_face_area_m2": float(mesh.minimum_face_area_m2),
        },
        "mapping": {
            "route": "triangle-terrain-windowed-v1",
            "config": mapping_config.model_dump(mode="json"),
            "statistics": report["stats"],
            "area_totals_m2": {"native": report["native_face_area_total_m2"],
                               "covered": report["covered_area_total_m2"], "valid": report["valid_area_total_m2"]},
            "failure_count": len(report.get("failures", [])),
        },
    }
    if body_metadata is not None:
        reserved_body_keys = {"format", "schema_version", "run_id", "status", "preparation_only",
                              "simulation_result", "production_approved", "engineering_ready", "bindings",
                              "source", "mesh", "mapping", "face_count"}
        if reserved_body_keys.intersection(body_metadata):
            raise RestrictedWorkerError("route-specific body metadata may not replace prepared-terrain contract fields")
        body.update(body_metadata)
    if body_path_overrides is not None:
        if set(body_path_overrides) - {"mesh_path", "source_manifest_path"}:
            raise RestrictedWorkerError("route-specific prepared path override is unsupported")
        if "mesh_path" in body_path_overrides:
            body["mesh"]["path"] = body_path_overrides["mesh_path"]
        if "source_manifest_path" in body_path_overrides:
            body["source"]["source_manifest_path"] = body_path_overrides["source_manifest_path"]
    from .prepared_terrain import face_id_sequence_sha256
    body["mesh"]["face_id_sequence_sha256"] = face_id_sequence_sha256(int(item) for item in mesh.face_id)
    body["bindings"]["mesh_face_id_sequence_sha256"] = body["mesh"]["face_id_sequence_sha256"]
    report_status = report["status"]
    if report_status == "failed":
        raise RestrictedWorkerError("terrain mapper rejected the source/mesh: " + "; ".join(report.get("failures", [])[:3]))

    def iter_face_rows() -> Any:
        for index, row in enumerate(report["faces"]):
            yield {
                "face_id": int(row["face_id"]), "face_area_m2": float(row["face_area_m2"]),
                "covered_area_m2": float(row["covered_area_m2"]), "valid_area_m2": float(row["valid_area_m2"]),
                "nodata_area_m2": float(row["nodata_area_m2"]), "nonfinite_area_m2": float(row["nonfinite_area_m2"]),
                "masked_other_area_m2": float(row["masked_other_area_m2"]),
                "ignored_in_raster_area_m2": float(row["ignored_in_raster_area_m2"]),
                "ignored_valid_area_m2": float(row["ignored_valid_area_m2"]),
                "outside_raster_area_m2": float(row["outside_raster_area_m2"]),
                "coverage_ratio": float(row["coverage_ratio"]),
                "elevation_m": None if terrain.elevation_m[index] is None else float(terrain.elevation_m[index]),
                "valid": bool(terrain.valid_mask[index]),
            }

    stage_root = Path(control["attempt_root"])
    resources = parse_worker_resources(config["resources"])
    from .restricted_worker import _tree_bytes
    starting_output_bytes = _tree_bytes(stage_root, skip_names={"scratch"})
    files = write_prepared_face_artifacts(
        stage_root / "artifacts", body=body, face_rows=iter_face_rows(),
        output_budget_bytes=resources.output_budget_bytes, starting_output_bytes=starting_output_bytes,
        body_validator=prepared_body_validator,
    )
    return {"status": "success", "category": "preparation_diagnostic_complete",
            "message": "bounded window mapping and prepared artifact write completed",
            "prepared_files": files}


def _worker_child(control_path: Path) -> int:
    """Entry point called only after the supervisor has resumed a job member."""
    control: dict[str, Any] = {}
    response_path: Path | None = None
    try:
        control = json.loads(control_path.read_text(encoding="utf-8"))
        if not isinstance(control, dict) or set(control) != {"format", "config_path", "config_sha256", "config_base",
                                                              "project_root", "attempt_root", "code_hashes",
                                                              "vertical_evidence_sha256"}:
            raise RestrictedWorkerError("worker control file is malformed")
        if control["format"] != "triangle-preparation-worker-control-v1":
            raise RestrictedWorkerError("worker control format is unsupported")
        config_path = Path(control["config_path"])
        config_bytes = config_path.read_bytes()
        if hashlib.sha256(config_bytes).hexdigest() != control["config_sha256"]:
            raise RestrictedWorkerError("copied worker config differs from its supervisor identity")
        config = validate_route_config_shape(json.loads(config_bytes))
        resources = parse_worker_resources(config["resources"])
        root = Path(control["project_root"]).resolve()
        stage = Path(control["attempt_root"]).resolve()
        pilot_route = config.get("format") == B1_PILOT_FORMAT
        mesh_only_route = config.get("format") == DOMAIN_MESH_ONLY_FORMAT
        current_code = _module_code_hashes(root, pilot_route=pilot_route,
                                           domain_mesh_only_route=mesh_only_route)
        if current_code != control["code_hashes"]:
            raise RestrictedWorkerError("worker route code changed between supervisor check and worker start")
        stage.joinpath("artifacts").mkdir(exist_ok=True)
        if mesh_only_route:
            from .domain_mesh_only import run_domain_mesh_only_worker
            result = run_domain_mesh_only_worker(control, config, root)
        elif pilot_route:
            from .b1_synthetic_pilot import run_pilot_worker
            result = run_pilot_worker(control, config, root)
        else:
            result = _worker_run_mapping(control, config, root)
        response = {"format": "triangle-preparation-worker-result-v1", **result,
                    "worker_exit_code": 0, "finished_at": _now(), "process_id": os.getpid()}
        response_bytes = _json_bytes(response)
        if len(response_bytes) > MAX_RECEIPT_BYTES:
            raise ResourceBudgetError("worker receipt exceeds its fixed 64 KiB diagnostic cap")
        _write_no_clobber_bytes(stage / "artifacts" / "worker-result.json", response_bytes,
                                output_budget=resources.output_budget_bytes,
                                root=stage, reserve_bytes=OUTPUT_RESERVE_BYTES)
        return 0
    except MemoryError as exc:
        category = "resource_rejected"
        code = 41
        message = f"worker allocation failed under its enforced Job Object limit: {type(exc).__name__}"
    except SourceIdentityError as exc:
        category = "source_identity_rejected"
        code = 54
        message = f"{type(exc).__name__}: {exc}"
    except OSError as exc:
        # Output errors from the final artifact directory are write failures;
        # remaining I/O exceptions are treated as source/input read failures.
        stage_value = control.get("attempt_root") if isinstance(control, dict) else None
        if stage_value and str(getattr(exc, "filename", "") or "").startswith(str(stage_value)):
            category, code = "failed_write", 52
        else:
            category, code = "failed_read", 51
        message = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        try:
            from .prepared_terrain import PreparedOutputBudgetExceeded, PreparedTerrainWriteError
            output_budget_error = isinstance(exc, PreparedOutputBudgetExceeded)
            write_error = isinstance(exc, PreparedTerrainWriteError)
        except Exception:
            output_budget_error = False
            write_error = False
        try:
            from .dem_contract import DEMReaderError
            dem_read_error = isinstance(exc, DEMReaderError)
        except Exception:
            dem_read_error = False
        message = str(exc)
        if "raw window provider failed" in message.lower() or "could not read dem" in message.lower():
            dem_read_error = True
        category = ("resource_rejected" if isinstance(exc, ResourceBudgetError) or output_budget_error
                    else "failed_write" if write_error else "failed_read" if dem_read_error else "worker_failed")
        code = {"resource_rejected": 41, "failed_write": 52, "failed_read": 51, "worker_failed": 53}[category]
        message = f"{type(exc).__name__}: {exc}"
    try:
        stage_value = Path(control["attempt_root"])
        response_path = stage_value / "artifacts" / "worker-result.json"
        resources = parse_worker_resources(config["resources"]) if "config" in locals() else None
        if resources is not None:
            response = {"format": "triangle-preparation-worker-result-v1", "status": "failed",
                        "category": category, "message": message[:2048], "finished_at": _now(),
                        "worker_exit_code": code, "process_id": os.getpid()}
            try:
                _write_no_clobber_bytes(response_path, _json_bytes(response),
                                        output_budget=resources.output_budget_bytes,
                                        root=stage_value, reserve_bytes=OUTPUT_RESERVE_BYTES)
            except Exception as receipt_error:
                # Keep the original worker exit/category when the main output
                # receipt itself cannot be committed.  A short bounded
                # scratch-side fallback gives the supervisor the exact I/O
                # failure instead of misreporting the result as a generic
                # missing-receipt failure.
                fallback_path = stage_value / "scratch" / "worker-failure.json"
                fallback = {"format": "triangle-preparation-worker-failure-v1",
                            "category": category, "worker_exit_code": code,
                            "worker_result_write_failed": True,
                            "worker_result_write_error": f"{type(receipt_error).__name__}: {receipt_error}"[:1024],
                            "message": message[:1024], "process_id": os.getpid()}
                fallback_bytes = _json_bytes(fallback)
                if len(fallback_bytes) <= 4096 and _tree_bytes(stage_value / "scratch") + len(fallback_bytes) <= resources.scratch_budget_bytes:
                    temporary = _short_temp_path(fallback_path, prefix=".err-")
                    try:
                        with temporary.open("xb") as stream:
                            stream.write(fallback_bytes)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.link(temporary, fallback_path)
                    finally:
                        temporary.unlink(missing_ok=True)
    except Exception:
        pass
    return code


def _validate_basic_config_for_attempt(config: Any) -> tuple[str, str]:
    if not isinstance(config, dict):
        raise RestrictedWorkerError("config root must be an object")
    run_id = config.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id) is None:
        raise RestrictedWorkerError("run_id must be a safe identifier to reserve diagnostics")
    output_root = config.get("output_root")
    return run_id, _safe_relative(output_root, "output_root")


def _path_length_preflight(project_root: Path, output_root: Path, run_id: str, stage: Path,
                           config: Mapping[str, Any]) -> str | None:
    final = output_root / run_id
    failed = output_root / f"{run_id}.failed"
    candidates = [
        output_root, final, final / "READY", final / "manifest.json", final / "status.json",
        final / "artifacts" / "prepared_faces.jsonl", final / "artifacts" / "prepared_body.json",
        final / "artifacts" / "worker-result.json",
        failed, failed / "manifest.json", failed / "status.json", failed / "artifacts" / "worker-result.json",
        stage, stage / "READY", stage / "READY.pending", stage / "manifest.json", stage / "status.json",
        stage / "scratch" / "worker-config.json", stage / "scratch" / "worker-control.json",
        stage / "scratch" / "worker-created.json", stage / "scratch" / "worker-resumed.json",
        stage / "scratch" / "worker-failure.json",
        stage / "scratch" / "temp", stage / "scratch" / "proj",
        stage / "artifacts" / "prepared_faces.jsonl", stage / "artifacts" / "prepared_body.json",
        stage / "artifacts" / "worker-result.json",
        _short_temp_path(stage / "status.json"), _short_temp_path(stage / "manifest.json"),
        _short_temp_path(stage / "READY.pending"),
        _short_temp_path(stage / "scratch" / "worker-created.json"),
        _short_temp_path(stage / "scratch" / "worker-resumed.json"),
        _short_temp_path(stage / "scratch" / "worker-failure.json", prefix=".err-"),
        _short_temp_path(stage / "scratch" / "worker-failure.json", prefix=".err-"),
        _short_temp_path(stage / "artifacts" / "worker-result.json"),
        _short_temp_path(stage / "artifacts" / "prepared_body.json", prefix=".p-"),
        _short_temp_path(stage / "artifacts" / "prepared_faces.jsonl", prefix=".p-"),
    ]
    if (config.get("format") == B1_PILOT_FORMAT
            and config.get("action") == "g0_gmsh_windowed_mapping"):
        for directory in (final / "artifacts", stage / "artifacts"):
            candidates.extend((directory / "gmsh_mesh.json", directory / "gmsh_mapping_mesh.json"))
        candidates.extend((
            _short_temp_path(stage / "artifacts" / "gmsh_mesh.json"),
            _short_temp_path(stage / "artifacts" / "gmsh_mapping_mesh.json"),
        ))
    if config.get("format") == DOMAIN_MESH_ONLY_FORMAT:
        source_manifest = config.get("source_manifest")
        if isinstance(source_manifest, dict) and isinstance(source_manifest.get("path"), str):
            candidates.append(project_root / source_manifest["path"])
        for directory in (final / "artifacts", stage / "artifacts"):
            candidates.extend((directory / "domain-mesh.json", directory / "domain-source-audit.json",
                              directory / "worker-result.json"))
        candidates.extend((final / "MESH_READY", stage / "MESH_READY.pending",
                           stage / "scratch" / "domain-mesh-generated.json",
                           _short_temp_path(stage / "artifacts" / "domain-mesh.json"),
                           _short_temp_path(stage / "artifacts" / "domain-source-audit.json")))
    try:
        source = config.get("source", {})
        mesh = config.get("mesh", {})
        if isinstance(source, dict):
            for key in ("raster_path", "source_manifest_path"):
                value = source.get(key)
                if isinstance(value, str):
                    candidates.append(_resolve_project_path(project_root, value, f"source.{key}"))
            for item in source.get("components", []) if isinstance(source.get("components", []), list) else []:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    candidates.append(_resolve_project_path(project_root, item["path"], "source component"))
        if isinstance(mesh, dict) and isinstance(mesh.get("path"), str):
            candidates.append(_resolve_project_path(project_root, mesh["path"], "mesh.path"))
    except RestrictedWorkerError as exc:
        return str(exc)
    too_long = [(path, _windows_path_units(path)) for path in candidates if _windows_path_units(path) >= 260]
    if too_long:
        path, size = too_long[0]
        return f"path_rejected: Windows path has {size} UTF-16 code units (limit <260): {path}"
    return None


def _read_worker_created_journal(stage: Path) -> dict[str, Any] | None:
    path = stage / "scratch" / "worker-created.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (not isinstance(value, dict) or value.get("format") != "triangle-worker-created-suspended-v1"
            or type(value.get("process_id")) is not int or value["process_id"] <= 0
            or value.get("job_list_assigned_at_create") is not True
            or value.get("process_in_job_before_resume") is not True
            or value.get("initial_thread_suspended") is not True):
        return None
    return value


def _read_worker_resumed_journal(stage: Path, *, expected_process_id: int | None = None) -> dict[str, Any] | None:
    if type(expected_process_id) is not int or expected_process_id <= 0:
        return None
    path = stage / "scratch" / "worker-resumed.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    process_id = value.get("process_id") if isinstance(value, dict) else None
    process_ids = value.get("process_ids") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("format") != "triangle-worker-resumed-v2"
            or value.get("resume_succeeded") is not True
            or type(value.get("resume_previous_suspend_count")) is not int
            or value["resume_previous_suspend_count"] != 1
            or type(process_id) is not int or process_id <= 0
            or not isinstance(process_ids, list) or not process_ids
            or any(type(item) is not int or item <= 0 for item in process_ids)
            or len(set(process_ids)) != len(process_ids) or process_id not in process_ids
            or not isinstance(value.get("recorded_at"), str)
            or process_id != expected_process_id):
        return None
    return value


def _finish_failure(stage: Path, final_failure: Path, *, run_id: str, status: str, exit_code: int,
                    message: str, worker_start_state: _WorkerStartState, config_sha256: str | None,
                    worker_receipt: Mapping[str, Any] | None, output_budget: int | None) -> WorkerRunResult:
    worker_started = worker_start_state.ever_started
    (stage / "READY").unlink(missing_ok=True)
    (stage / "READY.pending").unlink(missing_ok=True)
    if worker_start_state.mesh_only_route:
        (stage / "MESH_READY").unlink(missing_ok=True)
        (stage / "MESH_READY.pending").unlink(missing_ok=True)
    status_record = {
        "format": ("triangle-domain-mesh-only-status-v1" if worker_start_state.mesh_only_route
                   else "triangle-preparation-worker-status-v1"), "run_id": run_id,
        "status": status, "exit_code": exit_code, "worker_started": worker_started,
        "preparation_only": True, "simulation_result": False, "production_approved": False,
        "ready": False, "latest_published": False, "message": message[:2048], "finished_at": _now(),
    }
    if worker_start_state.mesh_only_route:
        status_record.update({"mesh_only": True, "mesh_ready": False, "engineering_ready": False})
    if worker_receipt is not None:
        status_record["worker"] = dict(worker_receipt)
    created = _read_worker_created_journal(stage)
    if created is not None:
        status_record["worker_created_suspended"] = True
        status_record["worker_pid_created_suspended"] = created["process_id"]
        status_record["job_list_assigned_at_create"] = created["job_list_assigned_at_create"]
        status_record["process_in_job_before_resume"] = created["process_in_job_before_resume"]
    resumed = _read_worker_resumed_journal(
        stage, expected_process_id=None if created is None else created["process_id"])
    if resumed is not None:
        status_record["last_job_process_ids_observed"] = resumed["process_ids"]
    failure_manifest = ({
        "format": "triangle-domain-mesh-only-bundle-v1", "schema_version": "1.0",
        "run_id": run_id, "manifest_status": status, "mesh_only": True, "mesh_ready": False,
        "preparation_only": True, "simulation_result": False, "production_approved": False,
        "engineering_ready": False, "latest_published": False,
        "config_sha256": config_sha256, "message": message[:2048],
        "worker": None if worker_receipt is None else dict(worker_receipt),
    } if worker_start_state.mesh_only_route else {
        "manifest_format": "triangle-prepared-terrain-b0-v1", "schema_version": "1.0",
        "run_id": run_id, "manifest_status": status, "ready": False,
        "preparation_only": True, "simulation_result": False, "production_approved": False,
        "engineering_ready": False, "latest_published": False,
        "config_sha256": config_sha256, "message": message[:2048],
        "worker": None if worker_receipt is None else dict(worker_receipt),
    })
    try:
        if output_budget is None:
            _write_atomic_json(stage / "manifest.json", failure_manifest)
            _write_atomic_json(stage / "status.json", status_record)
        else:
            _write_atomic_json(stage / "manifest.json", failure_manifest,
                               output_budget=output_budget, root=stage, reserve_bytes=16 * 1024)
            _write_atomic_json(stage / "status.json", status_record,
                               output_budget=output_budget, root=stage, reserve_bytes=4 * 1024)
    except Exception:
        # The pending directory is still non-ready; it remains available for
        # forensic recovery and can never be consumed as a prepared bundle.
        pass
    if not final_failure.exists():
        try:
            os.rename(stage, final_failure)
            stage = final_failure
        except OSError:
            pass
    return WorkerRunResult(status, exit_code, run_id, None, stage, worker_started,
                           message[:2048], worker_receipt)


def _publish_mesh_only_stage(
    *, stage: Path, output_root: Path, final_path: Path, failed_path: Path,
    run_id: str, config: Mapping[str, Any], config_sha256: str, project_root: Path,
    config_path: Path,
    resources: WorkerResources, worker_start_state: _WorkerStartState,
    worker_receipt: Mapping[str, Any], worker_result: Mapping[str, Any],
    code_hashes: Mapping[str, str], source_identity: Mapping[str, Any],
    job_started_monotonic: float, stop_reason: Callable[[], str | None],
    before_directory_commit: Callable[[Path], None] | None,
) -> WorkerRunResult:
    """Publish only the distinct MESH_READY diagnostic after Job exit and identity checks."""
    from .domain_mesh_only import (
        DOMAIN_MESH_BUNDLE_FORMAT, DOMAIN_MESH_READY_FORMAT,
        inspect_source_manifest_identity, read_domain_mesh_bundle_identity,
    )

    def fail(status: str, code: int, message: str) -> WorkerRunResult:
        return _finish_failure(stage, failed_path, run_id=run_id, status=status, exit_code=code,
                               message=message, worker_start_state=worker_start_state,
                               config_sha256=config_sha256, worker_receipt=worker_receipt,
                               output_budget=resources.output_budget_bytes)

    stopped = stop_reason()
    if stopped is not None:
        return fail(stopped, 44 if stopped == "cancelled" else 43,
                    f"mesh-only generation stopped before envelope verification: {stopped}")
    if (worker_result.get("route") != "triangle-domain-mesh-only-generate-v1"
            or worker_result.get("mesh_only") is not True
            or worker_result.get("preparation_only") is not True
            or worker_result.get("simulation_result") is not False
            or worker_result.get("production_approved") is not False
            or worker_result.get("engineering_ready") is not False
            or worker_result.get("config_sha256") != config_sha256
            or worker_result.get("source_manifest_sha256") != config["source_manifest"]["sha256"]
            or worker_result.get("route_code_sha256") != dict(code_hashes)
            or worker_result.get("semantic_readback", {}).get("semantic_readback_passed") is not True):
        return fail("worker_failed", 53, "mesh-only worker result does not bind the trusted route/config/source/readback")
    if worker_receipt.get("job_active_processes_at_exit") != 0:
        return fail("orphaned_child", 45, "mesh-only worker exited while a Job child remained active")
    if time.monotonic() - job_started_monotonic > resources.wall_clock_limit_s:
        return fail("timed_out", 43, "mesh-only worker and verification exceeded wall_clock_limit_s")

    # Recheck only identities in the supervisor. Geometry parsing and set
    # audits were completed by the worker while its Job limit was active.
    if _is_reparse_or_symlink(config_path) or not config_path.is_file() or _sha256_file(config_path)[0] != config_sha256:
        raise SourceIdentityError("mesh-only config changed after the restricted worker run")
    source_after = inspect_source_manifest_identity(config, project_root)
    if (source_after["manifest_sha256"] != source_identity["manifest_sha256"]
            or source_after["manifest_stat_identity"] != source_identity["manifest_stat_identity"]
            or source_after["component_file_records"] != source_identity["component_file_records"]):
        raise SourceIdentityError("mesh-only source manifest/component identities changed during the Job run")
    if (worker_result.get("source_manifest_stat_identity") != source_identity["manifest_stat_identity"]
            or worker_result.get("source_components_before") != worker_result.get("source_components_after")
            or worker_result.get("source_components_before") != list(source_identity["component_file_records"].values())):
        raise SourceIdentityError("mesh-only worker source-component receipts differ from the trusted manifest identities")

    artifact_directory = stage / "artifacts"
    expected_artifact_names = {"domain-mesh.json", "domain-source-audit.json", "worker-result.json"}
    actual_artifact_names = {entry.name for entry in artifact_directory.iterdir()}
    if actual_artifact_names != expected_artifact_names or any((artifact_directory / name).is_symlink() for name in expected_artifact_names):
        return fail("worker_failed", 53, "mesh-only worker produced missing or unexpected artifacts")
    mesh_file, audit_file = worker_result.get("mesh_file"), worker_result.get("source_audit_file")
    if (not isinstance(mesh_file, dict) or set(mesh_file) != {"name", "sha256", "bytes"}
            or mesh_file.get("name") != "domain-mesh.json"
            or not isinstance(audit_file, dict) or set(audit_file) != {"name", "sha256", "bytes"}
            or audit_file.get("name") != "domain-source-audit.json"):
        return fail("worker_failed", 53, "mesh-only worker artifact identities are malformed")
    files = {"mesh_file": dict(mesh_file), "source_audit_file": dict(audit_file),
             "worker_result_file": {"name": "worker-result.json", **_file_record(artifact_directory / "worker-result.json")}}
    for key, record in (("mesh_file", mesh_file), ("source_audit_file", audit_file)):
        digest, size = _sha256_file(artifact_directory / record["name"], maximum_bytes=resources.output_budget_bytes,
                                    abort_check=stop_reason)
        if digest != record["sha256"] or size != record["bytes"]:
            return fail("worker_failed", 53, f"mesh-only {key} bytes differ from the worker receipt")
    summary = worker_result.get("mesh_summary")
    if (not isinstance(summary, dict) or type(summary.get("node_count")) is not int
            or type(summary.get("face_count")) is not int or summary["node_count"] < 3
            or summary["face_count"] < 1 or summary["node_count"] > config["mesh_generation"]["maximum_nodes"]
            or summary["face_count"] > config["mesh_generation"]["maximum_faces"]
            or not isinstance(summary.get("mesh_id"), str)):
        return fail("worker_failed", 53, "mesh-only worker mesh summary is outside the trusted caps")
    gmsh = worker_result.get("gmsh")
    if (not isinstance(gmsh, dict) or gmsh.get("native_library_loaded_in_worker") is not True
            or gmsh.get("python_binding_loaded_in_worker") is not True
            or gmsh.get("python_binding_version") != config["expected_gmsh_version"]
            or gmsh.get("native_library_sha256") != config["expected_gmsh_library_sha256"]
            or gmsh.get("worker_pid") not in worker_receipt.get("job_process_ids_observed", [])):
        return fail("worker_failed", 53, "mesh-only Gmsh binding/DLL is not bound to the restricted Job tree")
    if (artifact_directory / "prepared_faces.jsonl").exists() or (artifact_directory / "prepared_body.json").exists():
        return fail("worker_failed", 53, "mesh-only route produced terrain/prepared artifacts")
    if (stage / "READY").exists() or (stage / "READY.pending").exists() or (output_root / "latest").exists():
        return fail("worker_failed", 53, "mesh-only route produced a terrain READY/latest marker")
    if (_tree_bytes(stage, skip_names={"scratch"}) > resources.output_budget_bytes
            or _tree_bytes(stage / "scratch") > resources.scratch_budget_bytes):
        return fail("resource_rejected", 40, "mesh-only output or scratch exceeded its configured budget")
    stopped = stop_reason()
    if stopped is not None:
        return fail(stopped, 44 if stopped == "cancelled" else 43,
                    f"mesh-only publication stopped before writer-owned manifest: {stopped}")

    manifest = {
        "format": DOMAIN_MESH_BUNDLE_FORMAT, "schema_version": "1.0", "run_id": run_id,
        "route": "triangle-domain-mesh-only-generate-v1", "manifest_status": "mesh_generation_diagnostic_complete",
        "mesh_only": True, "preparation_only": True, "simulation_result": False,
        "production_approved": False, "engineering_ready": False, "latest_published": False,
        "output_budget_bytes": resources.output_budget_bytes,
        "scratch_budget_bytes": resources.scratch_budget_bytes,
        "config_sha256": config_sha256,
        "source_manifest_sha256": source_identity["manifest_sha256"],
        "source_manifest": {"path": config["source_manifest"]["path"],
                             "sha256": source_identity["manifest_sha256"],
                             "stat_identity": source_identity["manifest_stat_identity"]},
        "target_coordinate_system": config["target_coordinate_system"],
        "resources": dict(config["resources"]),
        "route_code_sha256": dict(code_hashes), "mesh": dict(summary),
        "mesh_generation": {"quality_report": worker_result.get("quality_report"),
                            "area_audit": worker_result.get("geometry_audit", {}).get("area_audit"),
                            "line_coverage_audit": worker_result.get("geometry_audit", {}).get("line_coverage"),
                            "security_audit": worker_result.get("security_audit"),
                            "gmsh": gmsh, "gmsh_generation_seconds": worker_result.get("gmsh_generation_seconds")},
        "source_audit_file": files["source_audit_file"], "files": files,
        "worker": dict(worker_receipt), "operation_wallclock_limit_s": resources.wall_clock_limit_s,
        "operation_elapsed_seconds": time.monotonic() - job_started_monotonic,
        "mesh_ready": True,
    }
    _clear_success_scratch(stage / "scratch", stage)
    stopped = stop_reason()
    if stopped is not None:
        return fail(stopped, 44 if stopped == "cancelled" else 43,
                    f"mesh-only publication stopped while clearing scratch: {stopped}")
    manifest_bytes = _json_bytes(manifest)
    _write_no_clobber_bytes(stage / "manifest.json", manifest_bytes,
                            output_budget=resources.output_budget_bytes, root=stage, reserve_bytes=16 * 1024)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    _write_atomic_json(stage / "status.json", {
        "format": "triangle-domain-mesh-only-status-v1", "run_id": run_id,
        "status": "mesh_generation_diagnostic_complete", "exit_code": 0,
        "worker_started": worker_start_state.ever_started, "mesh_only": True,
        "preparation_only": True, "simulation_result": False, "production_approved": False,
        "engineering_ready": False, "ready": False, "mesh_ready": True, "latest_published": False,
        "config_sha256": config_sha256, "worker": dict(worker_receipt),
        "operation_wallclock_limit_s": resources.wall_clock_limit_s,
        "operation_elapsed_seconds": time.monotonic() - job_started_monotonic,
        "finished_at": _now(),
    }, output_budget=resources.output_budget_bytes, root=stage, reserve_bytes=8 * 1024)
    _write_no_clobber_bytes(stage / "MESH_READY.pending", _json_bytes({
        "format": DOMAIN_MESH_READY_FORMAT, "manifest_sha256": manifest_sha, "run_id": run_id,
        "mesh_only": True, "simulation_result": False, "production_approved": False,
        "engineering_ready": False,
    }), output_budget=resources.output_budget_bytes, root=stage, reserve_bytes=4 * 1024)
    os.rename(stage / "MESH_READY.pending", stage / "MESH_READY")
    if final_path.exists():
        raise RestrictedWorkerError(f"mesh-only final path appeared during publication; no-clobber refused: {final_path}")
    if before_directory_commit is not None:
        before_directory_commit(stage)
    stopped = stop_reason()
    if stopped is not None:
        return fail(stopped, 44 if stopped == "cancelled" else 43,
                    f"mesh-only generation stopped before diagnostic directory commit: {stopped}")
    if time.monotonic() - job_started_monotonic > resources.wall_clock_limit_s:
        return fail("timed_out", 43, "mesh-only writer-owned publication exceeded wall_clock_limit_s")
    os.rename(stage, final_path)
    stage = final_path
    try:
        identity = read_domain_mesh_bundle_identity(
            stage, expected_config_sha256=config_sha256,
            expected_source_manifest_sha256=source_identity["manifest_sha256"])
    except Exception as exc:
        return fail("worker_failed", 53,
                    f"mesh-only identity reader rejected the committed diagnostic bundle: {exc}")
    if identity["manifest_sha256"] != manifest_sha:
        return fail("worker_failed", 53, "mesh-only identity reader did not verify the supervisor manifest")
    return WorkerRunResult("mesh_generation_diagnostic_complete", 0, run_id, final_path, None,
                           worker_start_state.ever_started,
                           "mesh-only geometry diagnostic published; no terrain or simulation result was produced",
                           worker_receipt)


def run_restricted_preparation(config_path: str | Path, *, project_root: str | Path,
                               output_root_override: str | Path | None = None,
                               cancel_event: threading.Event | None = None,
                               _on_worker_created_suspended: Callable[[Mapping[str, Any]], None] | None = None,
                               _on_worker_job_processes: Callable[[Mapping[str, Any]], None] | None = None,
                               _before_directory_commit: Callable[[Path], None] | None = None) -> WorkerRunResult:
    """Run the one-worker B0 route and publish only after full tree exit/readback."""
    worker_start_state = _WorkerStartState()
    config_candidate = Path(config_path)
    if os.name == "nt" and _windows_path_units(config_candidate.absolute()) >= 260:
        raise RestrictedWorkerError("path_rejected: config path exceeds the supported Windows path limit")
    if _is_reparse_or_symlink(config_candidate):
        raise RestrictedWorkerError("config must not be a symlink or reparse point")
    config_path = config_candidate.resolve()
    project_root = Path(project_root).resolve()
    if config_path.is_symlink() or not config_path.is_file() or config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise RestrictedWorkerError("config must be a regular non-symlink file no larger than 4 MiB")
    config_bytes = config_path.read_bytes()
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    try:
        config = json.loads(config_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestrictedWorkerError(f"config JSON is malformed: {exc}") from exc
    worker_start_state.mesh_only_route = (
        isinstance(config, dict) and config.get("format") == DOMAIN_MESH_ONLY_FORMAT
    )
    run_id, output_relative = _validate_basic_config_for_attempt(config)
    if output_root_override is not None:
        output_candidate = Path(output_root_override)
        if _is_reparse_or_symlink(output_candidate):
            raise RestrictedWorkerError("output_root must not be a symlink or reparse point")
        output_root = output_candidate.resolve()
    else:
        output_root = _resolve_project_path(project_root, output_relative, "output_root")
    if _is_reparse_or_symlink(output_root):
        raise RestrictedWorkerError("output_root must be a non-symlink directory")
    final_path = output_root / run_id
    failed_path = output_root / f"{run_id}.failed"
    stage = output_root / f".p-{uuid.uuid4().hex[:12]}.pending"
    path_error = _path_length_preflight(project_root, output_root, run_id, stage, config)
    if path_error is not None:
        return WorkerRunResult("path_rejected", 55, run_id, None, None, False, path_error, None)
    output_root.mkdir(parents=True, exist_ok=True)
    if not output_root.is_dir():
        raise RestrictedWorkerError("output_root must be a regular directory")
    if final_path.exists() or failed_path.exists():
        raise RestrictedWorkerError(f"run_id already exists; no-clobber refused: {run_id}")
    stage.mkdir(exist_ok=False)
    scratch = stage / "scratch"
    artifacts = stage / "artifacts"
    scratch.mkdir()
    artifacts.mkdir()
    (scratch / "temp").mkdir()
    (scratch / "proj").mkdir()
    try:
        initial_status = {"format": ("triangle-domain-mesh-only-status-v1" if worker_start_state.mesh_only_route
                                      else "triangle-preparation-worker-status-v1"), "run_id": run_id,
                          "status": "running", "worker_started": False, "ready": False,
                          "preparation_only": True, "simulation_result": False,
                          "production_approved": False, "latest_published": False,
                          "config_sha256": config_sha, "started_at": _now()}
        if worker_start_state.mesh_only_route:
            initial_status.update({"mesh_only": True, "mesh_ready": False, "engineering_ready": False})
        _write_atomic_json(stage / "status.json", initial_status)
    except Exception as exc:
        return _finish_failure(stage, failed_path, run_id=run_id, status="supervisor_failed",
                               exit_code=70, message=f"could not initialize bounded run directory: {exc}",
                               worker_start_state=worker_start_state, config_sha256=config_sha, worker_receipt=None,
                               output_budget=None)

    try:
        resources = parse_worker_resources(config.get("resources"))
    except ResourceBudgetError as exc:
        return _finish_failure(stage, failed_path, run_id=run_id, status="resource_rejected",
                               exit_code=40, message=str(exc), worker_start_state=worker_start_state,
                               config_sha256=config_sha, worker_receipt=None, output_budget=None)
    try:
        config = validate_route_config_shape(config)
        # Re-parse the complete config after strict shape validation; the
        # resource object is deliberately mandatory and has no fallback.
        resources = parse_worker_resources(config["resources"])
    except (RestrictedWorkerError, ResourceBudgetError) as exc:
        status = "resource_rejected" if isinstance(exc, ResourceBudgetError) else "config_rejected"
        code = 40 if status == "resource_rejected" else 42
        return _finish_failure(stage, failed_path, run_id=run_id, status=status,
                               exit_code=code, message=str(exc), worker_start_state=worker_start_state,
                               config_sha256=config_sha, worker_receipt=None,
                               output_budget=resources.output_budget_bytes)
    if os.name != "nt":
        return _finish_failure(stage, failed_path, run_id=run_id, status="resource_rejected",
                               exit_code=40, message="Windows Job Object is unavailable; no unbounded fallback is permitted",
                               worker_start_state=worker_start_state, config_sha256=config_sha, worker_receipt=None,
                               output_budget=resources.output_budget_bytes)
    if cancel_event is not None and cancel_event.is_set():
        return _finish_failure(stage, failed_path, run_id=run_id, status="cancelled", exit_code=44,
                               message="cancelled before creating the restricted worker",
                               worker_start_state=worker_start_state, config_sha256=config_sha, worker_receipt=None,
                               output_budget=resources.output_budget_bytes)

    try:
        pilot_route = config.get("format") == B1_PILOT_FORMAT
        mesh_only_route = config.get("format") == DOMAIN_MESH_ONLY_FORMAT
        code_hashes = _module_code_hashes(project_root, pilot_route=pilot_route,
                                          domain_mesh_only_route=mesh_only_route)
        mesh_source_identity: dict[str, Any] | None = None
        if mesh_only_route:
            from .domain_mesh_only import inspect_source_manifest_identity
            # This supervisor preflight parses only the bounded source manifest
            # and file identities. Geometry decoding, union/D−S, Gmsh and all
            # heavy readback remain inside the Job-bound worker.
            mesh_source_identity = inspect_source_manifest_identity(config, project_root)
            vertical_hash = "0" * 64  # not part of this mesh-only input contract
        else:
            source = config["source"]
            raster_path = _resolve_project_path(project_root, source["raster_path"], "source.raster_path")
            source_stat = _stat_identity(raster_path)
            if source.get("expected_stat_identity") is not None and source_stat != source["expected_stat_identity"]:
                raise SourceIdentityError("source raster stat identity differs before worker launch")
            # The contextual relation evidence hash does not attest the DEM datum.
            vertical_path = project_root / "docs/external_evidence_contract.md"
            vertical_hash = _sha256_file(vertical_path)[0]
        scratch_config = scratch / "worker-config.json"
        with scratch_config.open("xb") as stream:
            stream.write(config_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        control = {
            "format": "triangle-preparation-worker-control-v1",
            "config_path": str(scratch_config), "config_sha256": config_sha,
            "config_base": str(config_path.parent), "project_root": str(project_root),
            "attempt_root": str(stage), "code_hashes": code_hashes,
            "vertical_evidence_sha256": vertical_hash,
        }
        control_path = scratch / "worker-control.json"
        control_bytes = _json_bytes(control)
        with control_path.open("xb") as stream:
            stream.write(control_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if _tree_bytes(scratch) > resources.scratch_budget_bytes:
            raise ResourceBudgetError("worker config/control files exceed scratch_budget_bytes")
        environment = dict(os.environ)
        project_path = str(project_root)
        previous_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = project_path + (os.pathsep + previous_pythonpath if previous_pythonpath else "")
        environment["TEMP"] = str(scratch / "temp")
        environment["TMP"] = str(scratch / "temp")
        environment["PROJ_USER_WRITABLE_DIRECTORY"] = str(scratch / "proj")
        environment["GDAL_PAM_ENABLED"] = "NO"
        environment["GDAL_GEOREF_SOURCES"] = "INTERNAL"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["HYDRO_B0_PROJECT_ROOT"] = str(project_root)
        command = [sys.executable, "-c", _WORKER_BOOTSTRAP, str(control_path)]
        _write_atomic_json(stage / "status.json", {
            **initial_status, "status": "running", "worker_started": False,
            "resources": {"job_memory_limit_mib": resources.job_memory_limit_mib,
                          "wall_clock_limit_s": resources.wall_clock_limit_s,
                          "output_budget_bytes": resources.output_budget_bytes,
                          "scratch_budget_bytes": resources.scratch_budget_bytes,
                          "child_process_policy": resources.child_process_policy},
        }, output_budget=resources.output_budget_bytes, root=stage, reserve_bytes=OUTPUT_RESERVE_BYTES)
        job_started_monotonic = time.monotonic()

        def stop_reason() -> str | None:
            if cancel_event is not None and cancel_event.is_set():
                return "cancelled"
            if time.monotonic() - job_started_monotonic >= resources.wall_clock_limit_s:
                return "timed_out"
            return None

        def record_job_processes(process_ids: list[int], active_count: int) -> None:
            if _on_worker_job_processes is not None:
                _on_worker_job_processes({"process_ids": list(process_ids), "active_processes": active_count})

        def record_created_suspended(created: Mapping[str, Any]) -> None:
            _write_no_clobber_bytes(
                scratch / "worker-created.json",
                _json_bytes({"format": "triangle-worker-created-suspended-v1", **dict(created),
                             "recorded_at": _now()}),
                output_budget=resources.scratch_budget_bytes, root=scratch,
            )
            if _on_worker_created_suspended is not None:
                _on_worker_created_suspended(dict(created))

        job_receipt = _run_contained_command(
            command, cwd=project_root, environment=environment,
            memory_limit_mib=resources.job_memory_limit_mib,
            wall_clock_limit_s=resources.wall_clock_limit_s,
            scratch_directory=scratch, output_directory=stage,
            scratch_budget_bytes=resources.scratch_budget_bytes,
            output_budget_bytes=resources.output_budget_bytes,
            worker_start_state=worker_start_state,
            cancel_event=cancel_event,
            on_created_suspended=record_created_suspended,
            on_job_processes=record_job_processes,
        )
        if not worker_start_state.ever_started:
            raise RestrictedWorkerError("worker receipt returned without a successful ResumeThread event")
        stopped = stop_reason()
        if stopped is not None:
            return _finish_failure(stage, failed_path, run_id=run_id, status=stopped,
                                   exit_code=44 if stopped == "cancelled" else 43,
                                   message=f"preparation stopped after worker-tree exit: {stopped}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        code_hashes_after = _module_code_hashes(project_root, pilot_route=pilot_route,
                                                domain_mesh_only_route=mesh_only_route)
        if code_hashes_after != code_hashes:
            raise RestrictedWorkerError("route code changed during the constrained worker run")
        stopped = stop_reason()
        if stopped is not None:
            return _finish_failure(stage, failed_path, run_id=run_id, status=stopped,
                                   exit_code=44 if stopped == "cancelled" else 43,
                                   message=f"preparation stopped during code identity verification: {stopped}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        if job_receipt["termination_reason"] is not None:
            status = job_receipt["termination_reason"]
            code = {"resource_rejected": 40, "timed_out": 43, "cancelled": 44,
                    "orphaned_child": 45, "supervisor_failed": 70}.get(status, 70)
            return _finish_failure(stage, failed_path, run_id=run_id, status=status,
                                   exit_code=code, message=f"worker tree stopped: {status}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        if job_receipt["job_active_processes_at_exit"] != 0:
            return _finish_failure(stage, failed_path, run_id=run_id, status="orphaned_child",
                                   exit_code=45, message="worker exited while a job child remained alive",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        if job_receipt["root_exit_code"] != 0:
            worker_result = _read_worker_result(stage, abort_check=stop_reason)
            if worker_result.get("worker_result_write_failed") is True:
                job_receipt["worker_result_write_failed"] = True
                job_receipt["worker_result_write_error"] = worker_result.get("worker_result_write_error")
                job_receipt["worker_reported_exit_code"] = worker_result.get("worker_exit_code", job_receipt["root_exit_code"])
            exit_category = {41: "resource_rejected", 51: "failed_read", 52: "failed_write",
                             53: "worker_failed", 54: "source_identity_rejected"}.get(job_receipt["root_exit_code"])
            category = exit_category or worker_result.get("category", "worker_failed")
            status = category if category in {"resource_rejected", "failed_read", "failed_write", "source_identity_rejected", "worker_failed"} else "worker_failed"
            code = {"resource_rejected": 40, "failed_read": 51, "failed_write": 52,
                    "worker_failed": 53, "source_identity_rejected": 54}[status]
            return _finish_failure(stage, failed_path, run_id=run_id, status=status,
                                   exit_code=code,
                                   message=(worker_result.get("message", "worker process failed")
                                            + (f"; worker-result receipt write failed: {worker_result.get('worker_result_write_error')}"
                                               if worker_result.get("worker_result_write_failed") else "")),
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        elapsed_wall = job_receipt["elapsed_seconds"]
        if elapsed_wall > resources.wall_clock_limit_s:
            return _finish_failure(stage, failed_path, run_id=run_id, status="timed_out", exit_code=43,
                                   message="supervisor verification exceeded wall_clock_limit_s",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        if _tree_bytes(scratch) > resources.scratch_budget_bytes or _tree_bytes(stage, skip_names={"scratch"}) > resources.output_budget_bytes:
            return _finish_failure(stage, failed_path, run_id=run_id, status="resource_rejected", exit_code=40,
                                   message="worker output or scratch tree exceeded its configured budget",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        stopped = stop_reason()
        if stopped is not None:
            return _finish_failure(stage, failed_path, run_id=run_id, status=stopped,
                                   exit_code=44 if stopped == "cancelled" else 43,
                                   message=f"preparation stopped before reading the worker receipt: {stopped}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        worker_result = _read_worker_result(stage, abort_check=stop_reason)
        stopped = stop_reason()
        if stopped is not None:
            return _finish_failure(stage, failed_path, run_id=run_id, status=stopped,
                                   exit_code=44 if stopped == "cancelled" else 43,
                                   message=f"preparation stopped after reading the worker receipt: {stopped}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        expected_worker_category = ("mesh_generation_diagnostic_complete" if mesh_only_route
                                    else "preparation_diagnostic_complete")
        if worker_result.get("status") != "success" or worker_result.get("category") != expected_worker_category:
            if worker_result.get("worker_result_write_failed") is True:
                job_receipt["worker_result_write_failed"] = True
                job_receipt["worker_result_write_error"] = worker_result.get("worker_result_write_error")
                job_receipt["worker_reported_exit_code"] = worker_result.get("worker_exit_code")
            category = worker_result.get("category", "worker_failed")
            status = category if category in {"resource_rejected", "failed_read", "failed_write", "source_identity_rejected", "worker_failed"} else "worker_failed"
            code = {"resource_rejected": 40, "failed_read": 51, "failed_write": 52,
                    "worker_failed": 53, "source_identity_rejected": 54}[status]
            return _finish_failure(stage, failed_path, run_id=run_id, status=status, exit_code=code,
                                   message=(worker_result.get("message", "worker did not return a successful preparation receipt")
                                            + (f"; worker-result receipt write failed: {worker_result.get('worker_result_write_error')}"
                                               if worker_result.get("worker_result_write_failed") else "")),
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)

        if mesh_only_route:
            if mesh_source_identity is None:
                raise RestrictedWorkerError("mesh-only source identity was not retained by the supervisor")
            return _publish_mesh_only_stage(
                stage=stage, output_root=output_root, final_path=final_path, failed_path=failed_path,
                run_id=run_id, config=config, config_sha256=config_sha, config_path=config_path,
                project_root=project_root,
                resources=resources, worker_start_state=worker_start_state, worker_receipt=job_receipt,
                worker_result=worker_result, code_hashes=code_hashes,
                source_identity=mesh_source_identity, job_started_monotonic=job_started_monotonic,
                stop_reason=stop_reason, before_directory_commit=_before_directory_commit,
            )

        from .prepared_terrain import build_prepared_manifest, read_prepared_terrain
        stopped = stop_reason()
        if stopped is not None:
            return _finish_failure(stage, failed_path, run_id=run_id, status=stopped,
                                   exit_code=44 if stopped == "cancelled" else 43,
                                   message=f"preparation stopped before prepared-file verification: {stopped}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        body_path = artifacts / "prepared_body.json"
        body = json.loads(body_path.read_text(encoding="utf-8"))
        files = body.get("prepared_face_file")
        if not isinstance(files, dict):
            raise RestrictedWorkerError("prepared body lacks the face file identity")
        body_sha, body_size = _sha256_file(body_path, abort_check=stop_reason)
        face_path = artifacts / "prepared_faces.jsonl"
        face_sha, face_size = _sha256_file(face_path, abort_check=stop_reason)
        stopped = stop_reason()
        if stopped is not None:
            return _finish_failure(stage, failed_path, run_id=run_id, status=stopped,
                                   exit_code=44 if stopped == "cancelled" else 43,
                                   message=f"preparation stopped while hashing prepared files: {stopped}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        final_files = {
            "prepared_face_file": {**files, "sha256": face_sha, "bytes": face_size},
            "prepared_body_file": {"name": "prepared_body.json", "sha256": body_sha, "bytes": body_size},
            "worker_result_file": {"name": "worker-result.json", **_file_record(artifacts / "worker-result.json")},
        }
        manifest = build_prepared_manifest(run_id=run_id, prepared_body=body,
                                           files=final_files, worker_receipt=job_receipt)
        manifest_payload = _json_bytes(manifest)
        _write_no_clobber_bytes(stage / "manifest.json", manifest_payload,
                                output_budget=resources.output_budget_bytes,
                                root=stage, reserve_bytes=16 * 1024)
        manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
        pending_ready_payload = _json_bytes({"format": "triangle-prepared-ready-pending-v1",
                                             "manifest_sha256": manifest_sha, "run_id": run_id,
                                             "simulation_result": False, "production_approved": False})
        _write_no_clobber_bytes(stage / "READY.pending", pending_ready_payload,
                                output_budget=resources.output_budget_bytes,
                                root=stage, reserve_bytes=8 * 1024)
        _write_atomic_json(stage / "status.json", {
            "format": "triangle-preparation-worker-status-v1", "run_id": run_id,
            "status": "prepared_validation_pending_atomic_publish", "exit_code": 0,
            "worker_started": worker_start_state.ever_started, "preparation_only": True, "simulation_result": False,
            "production_approved": False, "ready": False, "latest_published": False,
            "config_sha256": config_sha, "worker": job_receipt,
            "operation_wallclock_limit_s": resources.wall_clock_limit_s,
            "operation_elapsed_seconds": time.monotonic() - job_started_monotonic,
            "finished_at": _now(),
        }, output_budget=resources.output_budget_bytes, root=stage, reserve_bytes=0)
        expected_bindings = body.get("bindings", {})
        readback_started = time.monotonic()
        pilot_route = config.get("format") == B1_PILOT_FORMAT
        checked = read_prepared_terrain(
            stage, expected_bindings=expected_bindings, allow_pending_marker=True,
            abort_check=stop_reason,
            pilot_config=config if pilot_route else None,
            project_root=project_root if pilot_route else None,
        )
        if checked.manifest.get("worker", {}).get("job_active_processes_at_exit") != 0:
            raise RestrictedWorkerError("prepared readback did not verify an empty process tree")
        pilot_readback_seconds: float | None = None
        if pilot_route:
            pilot_readback_seconds = time.monotonic() - readback_started
        stopped = stop_reason()
        if stopped is not None:
            return _finish_failure(stage, failed_path, run_id=run_id, status=stopped,
                                   exit_code=44 if stopped == "cancelled" else 43,
                                   message=f"preparation stopped during prepared readback: {stopped}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        _clear_success_scratch(scratch, stage)
        stopped = stop_reason()
        if stopped is not None:
            return _finish_failure(stage, failed_path, run_id=run_id, status=stopped,
                                   exit_code=44 if stopped == "cancelled" else 43,
                                   message=f"preparation stopped while finalizing scratch: {stopped}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        if time.monotonic() - job_started_monotonic > resources.wall_clock_limit_s:
            return _finish_failure(stage, failed_path, run_id=run_id, status="timed_out", exit_code=43,
                                   message="complete worker and prepared readback exceeded wall_clock_limit_s",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        final_status = {
            "format": "triangle-preparation-worker-status-v1", "run_id": run_id,
            "status": "preparation_diagnostic_complete", "exit_code": 0,
            "worker_started": worker_start_state.ever_started, "preparation_only": True, "simulation_result": False,
            "production_approved": False, "ready": True, "latest_published": False,
            "config_sha256": config_sha, "worker": job_receipt,
            "operation_wallclock_limit_s": resources.wall_clock_limit_s,
            "operation_elapsed_seconds": time.monotonic() - job_started_monotonic,
            "finished_at": _now(),
        }
        if pilot_readback_seconds is not None:
            final_status["pilot_readback_seconds"] = pilot_readback_seconds
        _write_atomic_json(stage / "status.json", final_status,
                           output_budget=resources.output_budget_bytes, root=stage, reserve_bytes=8 * 1024)
        if time.monotonic() - job_started_monotonic > resources.wall_clock_limit_s:
            return _finish_failure(stage, failed_path, run_id=run_id, status="timed_out", exit_code=43,
                                   message="atomic publication preparation exceeded wall_clock_limit_s",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        if final_path.exists():
            raise RestrictedWorkerError(f"final run path appeared during publication; no-clobber refused: {final_path}")
        _write_atomic_json(stage / "READY.pending", {
            "format": "triangle-prepared-ready-v1", "manifest_sha256": manifest_sha,
            "run_id": run_id, "simulation_result": False, "production_approved": False,
        }, output_budget=resources.output_budget_bytes, root=stage, reserve_bytes=4 * 1024)
        os.rename(stage / "READY.pending", stage / "READY")
        if _before_directory_commit is not None:
            _before_directory_commit(stage)
        stopped = stop_reason()
        if stopped is not None:
            return _finish_failure(stage, failed_path, run_id=run_id, status=stopped,
                                   exit_code=44 if stopped == "cancelled" else 43,
                                   message=f"preparation stopped before directory commit: {stopped}",
                                   worker_start_state=worker_start_state, config_sha256=config_sha,
                                   worker_receipt=job_receipt, output_budget=resources.output_budget_bytes)
        os.rename(stage, final_path)
        return WorkerRunResult("preparation_diagnostic_complete", 0, run_id, final_path, None,
                               worker_start_state.ever_started,
                               "prepared diagnostic published with an empty restricted job tree", job_receipt)
    except PreparedTerrainStopped as exc:
        return _finish_failure(stage, failed_path, run_id=run_id, status=exc.status,
                               exit_code=44 if exc.status == "cancelled" else 43,
                               message=str(exc), worker_start_state=worker_start_state,
                               config_sha256=config_sha, worker_receipt=locals().get("job_receipt"),
                               output_budget=resources.output_budget_bytes)
    except SupervisorStopped as exc:
        return _finish_failure(stage, failed_path, run_id=run_id, status=exc.status,
                               exit_code=44 if exc.status == "cancelled" else 43,
                               message=str(exc), worker_start_state=worker_start_state,
                               config_sha256=config_sha, worker_receipt=locals().get("job_receipt"),
                               output_budget=resources.output_budget_bytes)
    except SourceIdentityError as exc:
        return _finish_failure(stage, failed_path, run_id=run_id, status="source_identity_rejected", exit_code=54,
                               message=str(exc), worker_start_state=worker_start_state,
                               config_sha256=config_sha, worker_receipt=locals().get("job_receipt"),
                               output_budget=resources.output_budget_bytes)
    except ResourceBudgetError as exc:
        return _finish_failure(stage, failed_path, run_id=run_id, status="resource_rejected", exit_code=40,
                               message=str(exc), worker_start_state=worker_start_state, config_sha256=config_sha,
                               worker_receipt=locals().get("job_receipt"), output_budget=resources.output_budget_bytes)
    except Exception as exc:
        # _run_contained_command owns cleanup for exceptions after process
        # creation; its Job Object is closed with KILL_ON_JOB_CLOSE.  This
        # branch does not run any broad process-kill command.
        return _finish_failure(stage, failed_path, run_id=run_id, status="supervisor_failed", exit_code=70,
                               message=f"{type(exc).__name__}: {exc}",
                               worker_start_state=worker_start_state,
                               config_sha256=config_sha, worker_receipt=locals().get("job_receipt"),
                               output_budget=resources.output_budget_bytes)


def _file_record(path: Path) -> dict[str, Any]:
    digest, size = _sha256_file(path)
    return {"sha256": digest, "bytes": size}


def _read_worker_result(stage: Path, *, abort_check: Callable[[], str | None] | None = None) -> dict[str, Any]:
    if abort_check is not None:
        reason = abort_check()
        if reason is not None:
            raise SupervisorStopped(reason)
    path = stage / "artifacts" / "worker-result.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
        fallback_path = stage / "scratch" / "worker-failure.json"
        if (not fallback_path.is_symlink() and fallback_path.is_file()
                and fallback_path.stat().st_size <= 4096):
            try:
                fallback = json.loads(fallback_path.read_text(encoding="utf-8"))
                if (isinstance(fallback, dict)
                        and fallback.get("format") == "triangle-preparation-worker-failure-v1"
                        and type(fallback.get("worker_exit_code")) is int):
                    return {"status": "failed", "category": fallback.get("category", "worker_failed"),
                            "message": fallback.get("message", "worker failed"),
                            "worker_exit_code": fallback["worker_exit_code"],
                            "worker_result_write_failed": True,
                            "worker_result_write_error": fallback.get("worker_result_write_error")}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        return {"status": "failed", "category": "worker_failed",
                "message": "bounded worker receipt is absent or oversized and no fallback diagnostic was readable",
                "worker_result_write_failed": True,
                "worker_result_write_error": "main worker-result and bounded fallback receipt were unavailable"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "failed", "category": "worker_failed", "message": f"worker receipt is invalid: {exc}"}
    if not isinstance(value, dict) or value.get("format") != "triangle-preparation-worker-result-v1":
        return {"status": "failed", "category": "worker_failed", "message": "worker receipt format is invalid"}
    if abort_check is not None:
        reason = abort_check()
        if reason is not None:
            raise SupervisorStopped(reason)
    return value


def recover_incomplete_attempts(output_root: str | Path) -> list[Path]:
    """Mark abandoned pending attempts as supervisor_lost without READY."""
    root = Path(output_root)
    recovered: list[Path] = []
    if not root.is_dir() or root.is_symlink():
        raise RestrictedWorkerError("recovery output root must be a non-symlink directory")
    for stage in sorted(root.glob(".*.pending")):
        if stage.is_symlink() or not stage.is_dir():
            continue
        (stage / "READY").unlink(missing_ok=True)
        (stage / "READY.pending").unlink(missing_ok=True)
        status_path = stage / "status.json"
        try:
            prior = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
        except Exception:
            prior = {}
        created_info = _read_worker_created_journal(stage) or {}
        resumed_info = _read_worker_resumed_journal(
            stage, expected_process_id=created_info.get("process_id")) or {}
        run_id = prior.get("run_id")
        if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id) is None:
            run_id = stage.name[1:].split(".", 1)[0]
        failed = root / f"{run_id}.failed"
        if failed.exists():
            continue
        record = {"format": "triangle-preparation-worker-status-v1", "run_id": run_id,
                  "status": "supervisor_lost", "exit_code": 71,
                  "worker_started": bool(prior.get("format") == "triangle-preparation-worker-status-v1"
                                          and prior.get("run_id") == run_id
                                          and prior.get("worker_started") is True) or bool(resumed_info),
                  "worker_created_suspended": bool(created_info),
                  "preparation_only": True, "simulation_result": False, "production_approved": False,
                  "ready": False, "latest_published": False,
                  "message": "pending attempt recovered after supervisor process exit; no success marker was present",
                  "finished_at": _now()}
        prior_worker = prior.get("worker") if isinstance(prior.get("worker"), dict) else {}
        pids = (resumed_info.get("process_ids") or prior.get("last_job_process_ids_observed")
                or prior_worker.get("job_process_ids_observed"))
        active = prior.get("last_job_active_processes_observed")
        if isinstance(pids, list):
            record["last_job_process_ids_observed"] = pids
        if type(active) is int:
            record["last_job_active_processes_observed"] = active
        if created_info:
            record["worker_pid_created_suspended"] = created_info.get("process_id")
        try:
            _write_atomic_json(status_path, record)
            _write_atomic_json(stage / "manifest.json", {
                "manifest_format": "triangle-prepared-terrain-b0-v1", "schema_version": "1.0",
                "run_id": run_id, "manifest_status": "supervisor_lost", "ready": False,
                "preparation_only": True, "simulation_result": False, "production_approved": False,
                "engineering_ready": False, "latest_published": False,
            })
            os.rename(stage, failed)
            recovered.append(failed)
        except OSError:
            continue
    return recovered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-config", type=Path, help="Versioned B0 preparation-worker JSON config")
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help="Project root containing bound inputs")
    args = parser.parse_args(argv)
    if args.run_config is None:
        parser.error("--run-config is required")
    cancel = threading.Event()
    old_handlers: dict[int, Any] = {}

    def request_cancel(signum: int, _frame: Any) -> None:
        cancel.set()

    for signal_number in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            old_handlers[signal_number] = signal.signal(signal_number, request_cancel)
        except (ValueError, OSError):
            pass
    try:
        result = run_restricted_preparation(args.run_config, project_root=args.project_root,
                                            cancel_event=cancel)
    finally:
        for signal_number, handler in old_handlers.items():
            try:
                signal.signal(signal_number, handler)
            except (ValueError, OSError):
                pass
    print(json.dumps({"run_id": result.run_id, "status": result.status, "exit_code": result.exit_code,
                      "worker_started": result.worker_started,
                      "bundle_path": None if result.bundle_path is None else str(result.bundle_path),
                      "diagnostic_path": None if result.diagnostic_path is None else str(result.diagnostic_path),
                      "message": result.message}, ensure_ascii=True, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - exercised through an OS Job Object
    raise SystemExit(main())


__all__ = [
    "B1_PILOT_FORMAT", "CHILD_POLICY", "FORMAT", "ROUTE_VERSION", "ResourceBudgetError", "RestrictedWorkerError",
    "SourceIdentityError", "WorkerResources", "WorkerRunResult", "parse_worker_resources", "recover_incomplete_attempts",
    "run_restricted_preparation", "validate_config_shape", "validate_route_config_shape",
]
