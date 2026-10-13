"""Prepared-grid and forcing readers independent of solver implementation."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import SimulationConfig
from .workflow_config import WorkflowError


@dataclass(frozen=True)
class PreparedWorkflowGrid:
    elevation: np.ndarray
    active_mask: np.ndarray
    combined_mask: np.ndarray
    inlet_mask: np.ndarray
    conveyance_mask: np.ndarray
    zones: np.ndarray
    x: np.ndarray
    y: np.ndarray
    coordinate_system: str
    vertical_datum: str
    provenance: str


def load_prepared_workflow_grid(path: Path, config: SimulationConfig) -> PreparedWorkflowGrid:
    shape = (config.domain.ny, config.domain.nx)
    if min(shape) < 2:
        raise WorkflowError("workflow domain needs at least 2 cells on each axis for nc2web export")
    if not path.is_file():
        raise WorkflowError(f"prepared_grid not found: {path}; provide the prepared NPZ before calculation")
    required = ("elevation", "active_mask", "combined_mask", "inlet_mask", "conveyance_mask", "zones", "x", "y",
                "coordinate_system", "vertical_datum", "elevation_unit", "provenance")
    with np.load(path, allow_pickle=False) as archive:
        for key in required:
            if key not in archive:
                raise WorkflowError(f"prepared_grid.{key} is required")
        arrays = {key: np.asarray(archive[key]).copy() for key in required}
    def text(key):
        value = arrays[key]
        if value.ndim != 0 or value.dtype.kind != 'U' or not str(value.item()).strip():
            raise WorkflowError(f"prepared_grid.{key} must be a non-empty scalar string")
        return str(value.item()).strip()
    if text('elevation_unit') != 'm':
        raise WorkflowError("prepared_grid.elevation_unit must be m")
    crs = text('coordinate_system')
    if crs != config.model.coordinate_system or crs != 'EPSG:4548':
        raise WorkflowError("prepared_grid.coordinate_system must match case CRS and nc2web EPSG:4548")
    datum = text('vertical_datum')
    if datum.lower() in {'unknown','none','not_confirmed'}:
        raise WorkflowError('prepared_grid.vertical_datum must be explicitly declared, not unknown')
    target = getattr(config.model, 'target_vertical_datum', None)
    if target is not None and target != datum:
        raise WorkflowError("prepared_grid.vertical_datum must match the configured target datum; no conversion is performed")
    z = arrays['elevation']
    if z.shape != shape or z.dtype.kind not in 'fiu' or not np.isfinite(z).all():
        raise WorkflowError("prepared_grid.elevation must be a finite numeric array matching (ny,nx); NoData is not filled")
    masks = {}
    for key in ('active_mask', 'combined_mask', 'inlet_mask', 'conveyance_mask'):
        values = arrays[key]
        if values.shape != shape or values.dtype.kind not in 'biu' or not np.isin(values, [0, 1]).all():
            raise WorkflowError(f"prepared_grid.{key} must be a boolean/0-1 array matching (ny,nx)")
        masks[key] = values.astype(bool)
    if not masks['active_mask'].any() or not masks['inlet_mask'].any():
        raise WorkflowError("prepared_grid.active_mask and inlet_mask must each contain at least one cell")
    if any(np.any(masks[key] & ~masks['combined_mask']) for key in ('active_mask', 'inlet_mask', 'conveyance_mask')):
        raise WorkflowError("prepared_grid active/inlet/conveyance masks must lie inside combined_mask")
    zones = arrays['zones']
    if zones.shape != shape or zones.dtype.kind != 'U' or not np.isin(zones, ['none','qingbei','qingnan','wenanwa']).all():
        raise WorkflowError("prepared_grid.zones must match (ny,nx) and contain none/qingbei/qingnan/wenanwa")
    if not np.array_equal(zones == 'qingbei', masks['active_mask']) or not np.array_equal(zones != 'none', masks['combined_mask']):
        raise WorkflowError("prepared_grid.zones must agree with active_mask and combined_mask")
    for key, expected in (
        ('x', config.domain.xmin + (np.arange(config.domain.nx) + .5) * config.domain.dx),
        ('y', config.domain.ymin + (np.arange(config.domain.ny) + .5) * config.domain.dy),
    ):
        values = arrays[key]
        if values.shape != expected.shape or values.dtype.kind not in 'fiu' or not np.isfinite(values).all() or not np.allclose(values, expected, rtol=0, atol=1e-6):
            raise WorkflowError(f"prepared_grid.{key} must match the configured cell-center coordinates in m")
    return PreparedWorkflowGrid(z.astype(float), **masks, zones=zones, x=arrays['x'], y=arrays['y'],
                                coordinate_system=crs, vertical_datum=datum, provenance=text('provenance'))


@dataclass(frozen=True)
class WorkflowForcing:
    time_s: np.ndarray
    inflow_m3s: np.ndarray
    outflow_m3s: np.ndarray
    rainfall_mm_per_step: np.ndarray
    xihe_component_m3s: np.ndarray | None


def load_workflow_forcing(path: Path, config: SimulationConfig) -> WorkflowForcing:
    required = ('time_s','inflow_m3s','outflow_m3s','rainfall_mm_per_step')
    if not path.is_file():
        raise WorkflowError(f"forcing.file not found: {path}")
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        if len(set(headers)) != len(headers) or any(key not in headers for key in required):
            raise WorkflowError(f"forcing CSV requires unique headers including {', '.join(required)}")
        components = ('duliu_component_m3s','xihe_component_m3s')
        if set(headers) - set(required + components):
            raise WorkflowError("forcing CSV has unsupported columns; use the four required columns and optional discharge components")
        if any(key in headers for key in components) and not all(key in headers for key in components):
            raise WorkflowError("forcing CSV requires both duliu_component_m3s and xihe_component_m3s when components are provided")
        names = required + (components if all(key in headers for key in components) else ())
        rows = []
        for row_number, row in enumerate(reader, 2):
            if None in row:
                raise WorkflowError(f"forcing row {row_number} contains more values than its header")
            values = []
            for key in names:
                try:
                    value = float(row[key])
                except (TypeError, ValueError) as exc:
                    raise WorkflowError(f"forcing row {row_number}.{key} must be numeric") from exc
                if not np.isfinite(value) or value < 0:
                    raise WorkflowError(f"forcing row {row_number}.{key} must be finite and non-negative")
                values.append(value)
            rows.append(values)
    if len(rows) < 2:
        raise WorkflowError("forcing CSV must contain at least two samples")
    data = np.asarray(rows, dtype=float)
    times = data[:,0]
    interval = float(config.output.interval)
    if (times[0] != config.model.start_time or times[-1] != config.model.end_time
            or np.any(np.diff(times) <= 0) or not np.allclose(np.diff(times), interval, rtol=0, atol=1e-8)):
        raise WorkflowError("forcing.time_s must strictly increase at output.interval and cover model.start_time through end_time exactly")
    if not config.source.rainfall.enabled and np.any(data[:,3] > 0):
        raise WorkflowError("forcing.rainfall_mm_per_step must be zero when case rainfall is disabled")
    if data.shape[1] > 4 and not np.allclose(data[:,4] + data[:,5], data[:,2], rtol=1e-9, atol=1e-8):
        raise WorkflowError("forcing discharge components must sum to outflow_m3s")
    return WorkflowForcing(times, data[:,1], data[:,2], data[:,3], data[:,5] if data.shape[1] > 4 else None)
