"""Solver-independent P3-A business fields on a prepared triangular mesh.

The module binds roughness, initial water state and boundary forcing to stable
face IDs or actual exterior mesh edges. It only prepares and serializes input
data; it never constructs solver arrays or allocates boundary fluxes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from numbers import Integral, Real
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator, model_validator
import yaml

from .config import BoundaryQuantity, InitialConditionQuantity
from .mesh_preparation import (
    MeshPreparationError,
    _validate_projected_metric_crs,
    read_ugrid_preparation,
)
from .triangular_mesh import FaceTerrainField, TriangularMesh, TriangularMeshError


class TriangularBusinessError(ValueError):
    """Raised when a P3-A field cannot be bound safely to a prepared mesh."""


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _validate_relative_path(value: str, name: str) -> str:
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise ValueError(f"{name} must be relative to the business config")
    return value


class _BusinessStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True, validate_assignment=True, allow_inf_nan=False, str_strip_whitespace=True)


class FaceAssignment(_BusinessStrictModel):
    face_id: StrictInt = Field(ge=0, description="Stable external triangular face identifier, dimensionless")
    value: float = Field(description="Field value in the quantity-specific unit")

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite_number(value, "value")


class RoughnessAssignment(_BusinessStrictModel):
    face_id: StrictInt = Field(ge=0, description="Stable external triangular face identifier, dimensionless")
    manning_n: float = Field(gt=0.0, le=1.0, description="Manning n, unit 1 (existing structured-input convention)")


class ConstantRoughnessInput(_BusinessStrictModel):
    type: Literal["constant"]
    manning_n: float = Field(gt=0.0, le=1.0, description="Manning n, unit 1 (existing structured-input convention)")
    source: str = Field(min_length=1, description="Value provenance")


class FaceRoughnessInput(_BusinessStrictModel):
    type: Literal["face_values"]
    assignments: list[RoughnessAssignment] = Field(min_length=1)
    source: str = Field(min_length=1, description="Value provenance")

    @model_validator(mode="after")
    def unique_ids(self) -> "FaceRoughnessInput":
        ids = [item.face_id for item in self.assignments]
        if len(ids) != len(set(ids)):
            raise ValueError("roughness assignments must not repeat face_id")
        return self


RoughnessInput = Annotated[ConstantRoughnessInput | FaceRoughnessInput, Field(discriminator="type")]


class ConstantInitialInput(_BusinessStrictModel):
    type: Literal["constant"]
    quantity: Literal["depth", "level"]
    value_m: float = Field(description="Water depth or absolute water level, m")
    vertical_datum: str | None = Field(default=None, description="Absolute level inherits known terrain datum when omitted; a supplied datum must match it")
    source: str = Field(min_length=1, description="Value provenance")

    @field_validator("value_m")
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite_number(value, "value_m")

    @model_validator(mode="after")
    def check_quantity(self) -> "ConstantInitialInput":
        if self.quantity == "depth" and self.value_m < 0.0:
            raise ValueError("depth value_m must be greater than or equal to 0 m")
        if self.vertical_datum is not None and not self.vertical_datum.strip():
            raise ValueError("vertical_datum must be a non-empty string or null")
        return self


class FaceInitialInput(_BusinessStrictModel):
    type: Literal["face_values"]
    quantity: Literal["depth", "level"]
    assignments: list[FaceAssignment] = Field(min_length=1)
    vertical_datum: str | None = Field(default=None, description="Absolute level inherits known terrain datum when omitted; a supplied datum must match it")
    source: str = Field(min_length=1, description="Value provenance")

    @model_validator(mode="after")
    def check_quantity_and_ids(self) -> "FaceInitialInput":
        ids = [item.face_id for item in self.assignments]
        if len(ids) != len(set(ids)):
            raise ValueError("initial-condition assignments must not repeat face_id")
        if self.quantity == "depth" and any(item.value < 0.0 for item in self.assignments):
            raise ValueError("depth assignment values must be greater than or equal to 0 m")
        if self.vertical_datum is not None and not self.vertical_datum.strip():
            raise ValueError("vertical_datum must be a non-empty string or null")
        return self


InitialInput = Annotated[ConstantInitialInput | FaceInitialInput, Field(discriminator="type")]


class ConstantBoundaryInput(_BusinessStrictModel):
    type: Literal["constant"]
    group_name: str = Field(min_length=1, description="Existing exterior boundary group name")
    quantity: Literal["discharge", "water_level"]
    value: float = Field(description="Total group discharge (m3/s) or absolute water level (m)")
    vertical_datum: str | None = Field(default=None, description="Required for water_level and matched to terrain")
    source: str = Field(min_length=1, description="Boundary provenance")

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite_number(value, "value")

    @model_validator(mode="after")
    def datum_semantics(self) -> "ConstantBoundaryInput":
        if self.quantity == "water_level" and (self.vertical_datum is None or not self.vertical_datum.strip()):
            raise ValueError("vertical_datum is required for a water_level boundary")
        if self.quantity == "discharge" and self.vertical_datum is not None:
            raise ValueError("vertical_datum is only valid for water_level boundaries")
        if self.quantity == "discharge" and self.value < 0.0:
            raise ValueError("discharge value must be greater than or equal to 0 m3/s")
        return self


class TimeSeriesBoundaryInput(_BusinessStrictModel):
    type: Literal["timeseries"]
    group_name: str = Field(min_length=1, description="Existing exterior boundary group name")
    quantity: Literal["discharge", "water_level"]
    time_s: list[float] = Field(min_length=1, description="Strictly increasing elapsed seconds")
    values: list[float] = Field(min_length=1, description="Total group discharge (m3/s) or water level (m)")
    vertical_datum: str | None = Field(default=None, description="Required for water_level and matched to terrain")
    source: str = Field(min_length=1, description="Boundary provenance")

    @field_validator("time_s", "values")
    @classmethod
    def finite_series(cls, values: list[float], info: Any) -> list[float]:
        if any(not math.isfinite(value) for value in values):
            raise ValueError(f"{info.field_name} values must be finite")
        return values

    @model_validator(mode="after")
    def validate_series(self) -> "TimeSeriesBoundaryInput":
        if len(self.time_s) != len(self.values):
            raise ValueError("time_s and values must have the same non-zero length")
        if any(value < 0.0 for value in self.time_s):
            raise ValueError("time_s values must be greater than or equal to 0 s")
        if any(right <= left for left, right in zip(self.time_s, self.time_s[1:])):
            raise ValueError("time_s must be strictly increasing")
        if self.quantity == "discharge" and any(value < 0.0 for value in self.values):
            raise ValueError("boundary values must be greater than or equal to 0")
        if self.quantity == "water_level" and (self.vertical_datum is None or not self.vertical_datum.strip()):
            raise ValueError("vertical_datum is required for a water_level boundary")
        if self.quantity == "discharge" and self.vertical_datum is not None:
            raise ValueError("vertical_datum is only valid for water_level boundaries")
        return self


BoundaryInput = Annotated[ConstantBoundaryInput | TimeSeriesBoundaryInput, Field(discriminator="type")]


class TriangularBusinessConfig(_BusinessStrictModel):
    """Independent YAML schema for P3-A face and edge business inputs."""

    version: StrictInt = Field(default=1, ge=1, le=1, description="Business preparation schema version")
    mesh_file: str = Field(min_length=1, description="Relative path to an accepted triangular UGRID artifact")
    output_file: str = Field(min_length=1, description="Relative path to the preparation-only JSON business package")
    roughness: RoughnessInput
    initial_condition: InitialInput
    boundary_bindings: list[BoundaryInput] = Field(default_factory=list)

    @field_validator("mesh_file", "output_file")
    @classmethod
    def relative_paths(cls, value: str, info: Any) -> str:
        return _validate_relative_path(value, info.field_name)

    @field_validator("mesh_file", "output_file")
    @classmethod
    def nonempty_paths(cls, value: str, info: Any) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty relative path")
        return value

    @model_validator(mode="after")
    def unique_boundary_groups(self) -> "TriangularBusinessConfig":
        names = [binding.group_name for binding in self.boundary_bindings]
        if len(names) != len(set(names)):
            raise ValueError("boundary_bindings must not repeat a group_name")
        return self


@dataclass(frozen=True)
class TriangularRoughnessField:
    """Manning n values aligned with mesh.face_id; missing assignments stay invalid."""

    mesh_id: str
    topology_fingerprint: str
    face_id: Sequence[int]
    manning_n: Sequence[float | None]
    valid_mask: Sequence[bool]
    source: str
    assignment_mode: Literal["constant", "face_values"]
    quantity: str = "manning_n"
    unit: str = "1"

    def __post_init__(self) -> None:
        ids = tuple(self.face_id)
        values = tuple(self.manning_n)
        valid = tuple(self.valid_mask)
        if self.quantity != "manning_n" or self.unit != "1":
            raise TriangularBusinessError("roughness quantity and unit must be manning_n and the existing dimensionless convention (unit 1)")
        if self.assignment_mode not in {"constant", "face_values"}:
            raise TriangularBusinessError("roughness assignment_mode must be constant or face_values")
        if not isinstance(self.source, str) or not self.source.strip():
            raise TriangularBusinessError("roughness source must be a non-empty provenance string")
        _validate_field_identity(self.mesh_id, self.topology_fingerprint, ids, values, valid)
        for index, (value, is_valid) in enumerate(zip(values, valid)):
            if is_valid:
                number = _business_number(value, f"manning_n[{index}]")
                if not 0.0 < number <= 1.0:
                    raise TriangularBusinessError(f"manning_n[{index}] must be in the range (0, 1]")
            elif value is not None:
                raise TriangularBusinessError(f"manning_n[{index}] must be null when valid_mask is false")
        if self.assignment_mode == "constant":
            if not all(valid):
                raise TriangularBusinessError("constant roughness must be valid on every mesh face")
            if any(value != values[0] for value in values[1:]):
                raise TriangularBusinessError("constant roughness values must be identical on every face")
        object.__setattr__(self, "face_id", ids)
        object.__setattr__(self, "manning_n", values)
        object.__setattr__(self, "valid_mask", valid)
        object.__setattr__(self, "source", self.source.strip())

    def validate_for(self, mesh: TriangularMesh) -> "TriangularRoughnessField":
        _validate_identity_for_mesh(self.mesh_id, self.topology_fingerprint, self.face_id, mesh)
        return self

    @classmethod
    def from_constant(cls, mesh: TriangularMesh, manning_n: float, *, source: str) -> "TriangularRoughnessField":
        value = _business_number(manning_n, "manning_n")
        if not 0.0 < value <= 1.0:
            raise TriangularBusinessError("manning_n must be in the range (0, 1]")
        return cls(mesh.mesh_id or "", topology_fingerprint(mesh), tuple(mesh.face_id), tuple(value for _ in range(mesh.n_face)), tuple(True for _ in range(mesh.n_face)), source, "constant")

    @classmethod
    def from_assignments(cls, mesh: TriangularMesh, assignments: Sequence[tuple[int, float]], *, source: str) -> "TriangularRoughnessField":
        values = _map_face_assignments(mesh, assignments, name="roughness", value_attribute=1)
        return cls(mesh.mesh_id or "", topology_fingerprint(mesh), tuple(mesh.face_id), values[0], values[1], source, "face_values")


@dataclass(frozen=True)
class TriangularInitialConditionField:
    """Depth or absolute water-level values aligned with stable triangular face IDs."""

    mesh_id: str
    topology_fingerprint: str
    face_id: Sequence[int]
    quantity: InitialConditionQuantity | str
    values_m: Sequence[float | None]
    valid_mask: Sequence[bool]
    source: str
    assignment_mode: Literal["constant", "face_values"]
    vertical_datum: str | None = None
    vertical_datum_source: Literal["terrain_metadata", "config_declaration"] | None = None
    source_terrain_vertical_datum: str | None = None
    unit: str = "m"

    def __post_init__(self) -> None:
        try:
            quantity = self.quantity if isinstance(self.quantity, InitialConditionQuantity) else InitialConditionQuantity(self.quantity)
        except ValueError as exc:
            raise TriangularBusinessError("initial-condition quantity must be depth or level") from exc
        ids, values, valid = tuple(self.face_id), tuple(self.values_m), tuple(self.valid_mask)
        if self.unit != "m":
            raise TriangularBusinessError("initial-condition values_m unit must be m")
        if self.assignment_mode not in {"constant", "face_values"}:
            raise TriangularBusinessError("initial-condition assignment_mode must be constant or face_values")
        if not isinstance(self.source, str) or not self.source.strip():
            raise TriangularBusinessError("initial-condition source must be a non-empty provenance string")
        _validate_field_identity(self.mesh_id, self.topology_fingerprint, ids, values, valid)
        for index, (value, is_valid) in enumerate(zip(values, valid)):
            if is_valid:
                number = _business_number(value, f"values_m[{index}]")
                if quantity is InitialConditionQuantity.DEPTH and number < 0.0:
                    raise TriangularBusinessError(f"depth values_m[{index}] must be greater than or equal to 0 m")
            elif value is not None:
                raise TriangularBusinessError(f"values_m[{index}] must be null when valid_mask is false")
        if self.assignment_mode == "constant":
            valid_values = [value for value, is_valid in zip(values, valid) if is_valid]
            if not valid_values:
                raise TriangularBusinessError("constant initial condition must be valid on at least one terrain face")
            if any(value != valid_values[0] for value in valid_values[1:]):
                raise TriangularBusinessError("constant initial-condition values_m must be identical on valid faces")
        if quantity is InitialConditionQuantity.LEVEL:
            if not isinstance(self.vertical_datum, str) or not self.vertical_datum.strip():
                raise TriangularBusinessError("absolute level requires an explicit vertical_datum")
            if self.vertical_datum_source not in {"terrain_metadata", "config_declaration"}:
                raise TriangularBusinessError("absolute level requires vertical_datum_source provenance")
        if self.vertical_datum is not None:
            if not isinstance(self.vertical_datum, str) or not self.vertical_datum.strip():
                raise TriangularBusinessError("vertical_datum must be a non-empty string or None")
            if self.vertical_datum_source not in {"terrain_metadata", "config_declaration"}:
                raise TriangularBusinessError("vertical_datum requires terrain-metadata or config-declaration provenance")
        elif self.vertical_datum_source is not None:
            raise TriangularBusinessError("vertical_datum_source cannot be set when vertical_datum is missing")
        if self.source_terrain_vertical_datum is not None and (not isinstance(self.source_terrain_vertical_datum, str) or not self.source_terrain_vertical_datum.strip()):
            raise TriangularBusinessError("source_terrain_vertical_datum must be a non-empty string or None")
        elif self.vertical_datum is not None and (not isinstance(self.vertical_datum, str) or not self.vertical_datum.strip()):
            raise TriangularBusinessError("vertical_datum must be a non-empty string or None")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "face_id", ids)
        object.__setattr__(self, "values_m", values)
        object.__setattr__(self, "valid_mask", valid)
        object.__setattr__(self, "source", self.source.strip())
        if isinstance(self.vertical_datum, str):
            object.__setattr__(self, "vertical_datum", self.vertical_datum.strip())

    def validate_for(self, mesh: TriangularMesh, terrain: FaceTerrainField) -> "TriangularInitialConditionField":
        _validate_identity_for_mesh(self.mesh_id, self.topology_fingerprint, self.face_id, mesh)
        terrain.validate_for(mesh)
        if self.assignment_mode == "constant" and tuple(self.valid_mask) != tuple(terrain.valid_mask):
            raise TriangularBusinessError("constant initial-condition valid_mask must match terrain valid_mask")
        if self.source_terrain_vertical_datum != terrain.vertical_datum:
            raise TriangularBusinessError("source_terrain_vertical_datum does not match linked terrain metadata")
        for index, valid in enumerate(self.valid_mask):
            if valid and not terrain.valid_mask[index]:
                raise TriangularBusinessError(f"initial-condition face_id {self.face_id[index]} is valid while terrain is invalid")
        if self.quantity is InitialConditionQuantity.LEVEL:
            if terrain.vertical_datum is None:
                raise TriangularBusinessError("absolute water level requires known terrain vertical_datum")
            if self.vertical_datum != terrain.vertical_datum:
                raise TriangularBusinessError("initial-condition vertical_datum does not match terrain vertical_datum")
        elif self.vertical_datum is not None and terrain.vertical_datum is not None and self.vertical_datum != terrain.vertical_datum:
            raise TriangularBusinessError("initial-condition vertical_datum does not match terrain vertical_datum")
        return self

    @classmethod
    def from_constant(
        cls,
        mesh: TriangularMesh,
        terrain: FaceTerrainField,
        quantity: InitialConditionQuantity | str,
        value_m: float,
        *,
        source: str,
        vertical_datum: str | None = None,
    ) -> "TriangularInitialConditionField":
        terrain.validate_for(mesh)
        selected = quantity if isinstance(quantity, InitialConditionQuantity) else InitialConditionQuantity(quantity)
        value = _business_number(value_m, "value_m")
        if selected is InitialConditionQuantity.DEPTH and value < 0.0:
            raise TriangularBusinessError("depth value_m must be greater than or equal to 0 m")
        resolved_datum, datum_source = _resolve_initial_datum(selected, terrain, vertical_datum)
        valid = tuple(bool(item) for item in terrain.valid_mask)
        values = tuple(value if item else None for item in valid)
        return cls(mesh.mesh_id or "", topology_fingerprint(mesh), tuple(mesh.face_id), selected, values, valid, source, "constant", resolved_datum, datum_source, terrain.vertical_datum)

    @classmethod
    def from_assignments(
        cls,
        mesh: TriangularMesh,
        terrain: FaceTerrainField,
        quantity: InitialConditionQuantity | str,
        assignments: Sequence[tuple[int, float]],
        *,
        source: str,
        vertical_datum: str | None = None,
    ) -> "TriangularInitialConditionField":
        terrain.validate_for(mesh)
        selected = quantity if isinstance(quantity, InitialConditionQuantity) else InitialConditionQuantity(quantity)
        resolved_datum, datum_source = _resolve_initial_datum(selected, terrain, vertical_datum)
        values, valid = _map_face_assignments(mesh, assignments, name="initial-condition", value_attribute=1)
        for index, is_valid in enumerate(valid):
            if is_valid and not terrain.valid_mask[index]:
                raise TriangularBusinessError(f"initial-condition assignment targets face_id {mesh.face_id[index]} with invalid terrain")
            if is_valid and selected is InitialConditionQuantity.DEPTH and float(values[index]) < 0.0:
                raise TriangularBusinessError(f"depth assignment for face_id {mesh.face_id[index]} must be greater than or equal to 0 m")
        return cls(mesh.mesh_id or "", topology_fingerprint(mesh), tuple(mesh.face_id), selected, values, valid, source, "face_values", resolved_datum, datum_source, terrain.vertical_datum)


@dataclass(frozen=True)
class TriangularBoundaryBinding:
    """A group-level boundary value or time series bound to actual exterior edges."""

    mesh_id: str
    topology_fingerprint: str
    group_name: str
    quantity: BoundaryQuantity | str
    mode: Literal["constant", "timeseries"]
    edge_ids: Sequence[int]
    edge_nodes: Sequence[Sequence[int]]
    values: Sequence[float]
    time_s: Sequence[float] | None
    unit: str
    interpretation: str
    source: str
    vertical_datum: str | None = None

    def __post_init__(self) -> None:
        try:
            quantity = self.quantity if isinstance(self.quantity, BoundaryQuantity) else BoundaryQuantity(self.quantity)
        except ValueError as exc:
            raise TriangularBusinessError("boundary quantity must be discharge or water_level") from exc
        edge_ids = tuple(self.edge_ids)
        edge_nodes = tuple(tuple(pair) for pair in self.edge_nodes)
        values = tuple(_business_number(value, "boundary value") for value in self.values)
        times = None if self.time_s is None else tuple(_business_number(value, "time_s") for value in self.time_s)
        if not isinstance(self.group_name, str) or not self.group_name.strip():
            raise TriangularBusinessError("boundary group_name must be non-empty")
        if self.mode not in {"constant", "timeseries"}:
            raise TriangularBusinessError("boundary mode must be constant or timeseries")
        if not edge_ids or len(edge_ids) != len(edge_nodes) or len(set(edge_ids)) != len(edge_ids):
            raise TriangularBusinessError("boundary edge_ids and edge_nodes must be non-empty, aligned, and unique")
        if any(isinstance(edge_id, bool) or not isinstance(edge_id, Integral) or edge_id < 0 for edge_id in edge_ids):
            raise TriangularBusinessError("boundary edge_ids must be non-negative integers")
        for edge_index, pair in enumerate(edge_nodes):
            if len(pair) != 2 or any(isinstance(node, bool) or not isinstance(node, Integral) for node in pair) or pair[0] == pair[1]:
                raise TriangularBusinessError(f"boundary edge_nodes[{edge_index}] must be a two-node edge")
        if not values:
            raise TriangularBusinessError("boundary values must be non-empty")
        if quantity is BoundaryQuantity.DISCHARGE:
            if any(value < 0.0 for value in values):
                raise TriangularBusinessError("discharge values must be greater than or equal to 0 m3/s")
            if self.unit != "m3/s" or self.interpretation != "total_group_discharge":
                raise TriangularBusinessError("discharge must be stored as total_group_discharge in m3/s")
            if self.vertical_datum is not None:
                raise TriangularBusinessError("discharge boundaries do not accept vertical_datum")
        else:
            if self.unit != "m" or self.interpretation != "absolute_water_level":
                raise TriangularBusinessError("water_level must be stored as absolute_water_level in m")
            if not isinstance(self.vertical_datum, str) or not self.vertical_datum.strip():
                raise TriangularBusinessError("water_level boundary requires a non-empty vertical_datum")
        if not isinstance(self.source, str) or not self.source.strip():
            raise TriangularBusinessError("boundary source must be a non-empty provenance string")
        if self.mode == "constant":
            if len(values) != 1 or times is not None:
                raise TriangularBusinessError("constant boundary requires one value and no time_s array")
        else:
            if times is None or len(times) != len(values):
                raise TriangularBusinessError("timeseries boundary requires equal non-empty time_s and values arrays")
            if any(time < 0.0 for time in times) or any(right <= left for left, right in zip(times, times[1:])):
                raise TriangularBusinessError("boundary time_s must be non-negative and strictly increasing")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "group_name", self.group_name.strip())
        object.__setattr__(self, "edge_ids", edge_ids)
        object.__setattr__(self, "edge_nodes", edge_nodes)
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "time_s", times)
        object.__setattr__(self, "source", self.source.strip())
        if isinstance(self.vertical_datum, str):
            object.__setattr__(self, "vertical_datum", self.vertical_datum.strip())

    def validate_for(self, mesh: TriangularMesh, terrain: FaceTerrainField) -> "TriangularBoundaryBinding":
        _validate_identity_for_mesh(self.mesh_id, self.topology_fingerprint, tuple(mesh.face_id), mesh)
        terrain.validate_for(mesh)
        if self.group_name in mesh.constraint_groups:
            raise TriangularBusinessError(f"'{self.group_name}' is an internal constraint group and cannot receive boundary forcing")
        if self.group_name not in mesh.boundary_groups:
            raise TriangularBusinessError(f"unknown exterior boundary group '{self.group_name}'")
        expected_ids = tuple(mesh.boundary_groups[self.group_name])
        expected_nodes = tuple(mesh.edge_nodes[index] for index in expected_ids)
        if tuple(self.edge_ids) != expected_ids or tuple(tuple(pair) for pair in self.edge_nodes) != expected_nodes:
            raise TriangularBusinessError(f"boundary binding '{self.group_name}' does not match actual mesh boundary edges")
        if self.quantity is BoundaryQuantity.WATER_LEVEL:
            if terrain.vertical_datum is None:
                raise TriangularBusinessError("water_level boundary requires known terrain vertical_datum")
            if self.vertical_datum != terrain.vertical_datum:
                raise TriangularBusinessError("water_level boundary vertical_datum does not match terrain vertical_datum")
        return self


@dataclass(frozen=True)
class TriangularBusinessPackage:
    mesh: TriangularMesh
    terrain: FaceTerrainField
    roughness: TriangularRoughnessField
    initial_condition: TriangularInitialConditionField
    boundary_bindings: Sequence[TriangularBoundaryBinding]
    preparation_only: bool = True

    def __post_init__(self) -> None:
        if self.preparation_only is not True:
            raise TriangularBusinessError("business package must be marked preparation_only")
        self.terrain.validate_for(self.mesh)
        self.roughness.validate_for(self.mesh)
        self.initial_condition.validate_for(self.mesh, self.terrain)
        bindings = tuple(self.boundary_bindings)
        names: set[str] = set()
        assigned_edges: set[int] = set()
        for binding in bindings:
            binding.validate_for(self.mesh, self.terrain)
            if binding.group_name in names:
                raise TriangularBusinessError(f"boundary group '{binding.group_name}' is bound more than once")
            names.add(binding.group_name)
            overlap = assigned_edges.intersection(binding.edge_ids)
            if overlap:
                raise TriangularBusinessError(f"exterior mesh edge IDs are bound more than once: {sorted(overlap)}")
            assigned_edges.update(binding.edge_ids)
        object.__setattr__(self, "boundary_bindings", bindings)


@dataclass(frozen=True)
class TriangularBusinessResult:
    package: TriangularBusinessPackage
    artifact_path: Path
    report: Mapping[str, Any]


def _business_number(value: Any, name: str) -> float:
    try:
        return _finite_number(value, name)
    except ValueError as exc:
        raise TriangularBusinessError(str(exc)) from exc


def topology_fingerprint(mesh: TriangularMesh) -> str:
    """Hash actual coordinates, connectivity, stable IDs and named edge groups."""
    def groups_json(groups: Mapping[str, Sequence[int]]) -> dict[str, list[list[int]]]:
        return {name: [list(mesh.edge_nodes[index]) for index in indices] for name, indices in sorted(groups.items())}
    payload = {
        "mesh_id": mesh.mesh_id,
        "coordinate_system": mesh.coordinate_system,
        "node_xy": mesh.node_xy,
        "face_nodes": mesh.face_nodes,
        "face_id": mesh.face_id,
        "edge_nodes": mesh.edge_nodes,
        "boundary_groups": groups_json(mesh.boundary_groups),
        "constraint_groups": groups_json(mesh.constraint_groups),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def terrain_fingerprint(mesh: TriangularMesh, terrain: FaceTerrainField) -> str:
    """Hash normalized terrain contents and provenance against actual face order."""
    terrain.validate_for(mesh)
    payload = {
        "mesh_id": mesh.mesh_id,
        "topology_fingerprint": topology_fingerprint(mesh),
        "face_id": mesh.face_id,
        "coordinate_system": terrain.coordinate_system,
        "elevation_m": tuple(None if value is None else float(value) for value in terrain.elevation_m),
        "valid_mask": terrain.valid_mask,
        "covered_area_m2": tuple(float(value) for value in terrain.covered_area_m2),
        "valid_area_m2": tuple(float(value) for value in terrain.valid_area_m2),
        "coverage_ratio": tuple(float(value) for value in terrain.coverage_ratio),
        "vertical_unit": terrain.vertical_unit,
        "vertical_unit_source": terrain.vertical_unit_source,
        "source_vertical_unit": terrain.source_vertical_unit,
        "vertical_datum": terrain.vertical_datum,
        "vertical_datum_source": terrain.vertical_datum_source,
        "source_vertical_datum": terrain.source_vertical_datum,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_field_identity(mesh_id: Any, fingerprint: Any, face_ids: tuple[Any, ...], values: tuple[Any, ...], valid: tuple[Any, ...]) -> None:
    if not isinstance(mesh_id, str) or not mesh_id.strip():
        raise TriangularBusinessError("field mesh_id must be non-empty")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        raise TriangularBusinessError("topology_fingerprint must be a lowercase SHA-256 hex digest")
    if not face_ids or len(face_ids) != len(values) or len(face_ids) != len(valid):
        raise TriangularBusinessError("face_id, values, and valid_mask must have the same non-zero face length")
    if any(isinstance(face_id, bool) or not isinstance(face_id, Integral) or face_id < 0 for face_id in face_ids):
        raise TriangularBusinessError("face_id values must be non-negative integers")
    if len(set(face_ids)) != len(face_ids):
        raise TriangularBusinessError("face_id values must be unique")
    if any(not isinstance(item, bool) for item in valid):
        raise TriangularBusinessError("valid_mask values must be boolean")


def _validate_identity_for_mesh(mesh_id: str, fingerprint: str, face_ids: Sequence[int], mesh: TriangularMesh) -> None:
    if mesh_id != mesh.mesh_id:
        raise TriangularBusinessError(f"field mesh_id '{mesh_id}' does not match mesh '{mesh.mesh_id}'")
    expected_fingerprint = topology_fingerprint(mesh)
    if fingerprint != expected_fingerprint:
        raise TriangularBusinessError("field topology_fingerprint does not match mesh geometry/topology")
    if tuple(face_ids) != tuple(mesh.face_id):
        raise TriangularBusinessError("field face_id ordering does not match mesh stable face IDs")


def _map_face_assignments(
    mesh: TriangularMesh,
    assignments: Sequence[tuple[int, float]],
    *,
    name: str,
    value_attribute: int,
) -> tuple[tuple[float | None, ...], tuple[bool, ...]]:
    by_id: dict[int, float] = {}
    known = set(mesh.face_id)
    for assignment in assignments:
        if len(assignment) != 2:
            raise TriangularBusinessError(f"{name} assignments must be (face_id, value) pairs")
        face_id, raw_value = assignment[0], assignment[value_attribute]
        if isinstance(face_id, bool) or not isinstance(face_id, Integral) or face_id < 0:
            raise TriangularBusinessError(f"{name} assignment face_id must be a non-negative integer")
        face_id = int(face_id)
        if face_id not in known:
            raise TriangularBusinessError(f"{name} assignment refers to unknown face_id {face_id}")
        if face_id in by_id:
            raise TriangularBusinessError(f"{name} assignments repeat face_id {face_id}")
        by_id[face_id] = _business_number(raw_value, f"{name} assignment value")
    if not by_id:
        raise TriangularBusinessError(f"{name} assignments must contain at least one face")
    values = tuple(by_id.get(face_id) for face_id in mesh.face_id)
    valid = tuple(value is not None for value in values)
    return values, valid


def _resolve_initial_datum(
    quantity: InitialConditionQuantity,
    terrain: FaceTerrainField,
    declared_datum: str | None,
) -> tuple[str | None, Literal["terrain_metadata", "config_declaration"] | None]:
    terrain_datum = terrain.vertical_datum
    if declared_datum is not None and not declared_datum.strip():
        raise TriangularBusinessError("vertical_datum must be a non-empty string when provided")
    if quantity is InitialConditionQuantity.LEVEL:
        if terrain_datum is None:
            raise TriangularBusinessError("absolute water level requires known terrain vertical_datum")
        if declared_datum is None:
            # Match existing InitialConditionField semantics: inherit the
            # explicitly recorded terrain datum when no separate label is supplied.
            return terrain_datum, "terrain_metadata"
        if declared_datum.strip() != terrain_datum:
            raise TriangularBusinessError("initial-condition vertical_datum does not match terrain vertical_datum")
        return declared_datum.strip(), "config_declaration"
    if declared_datum is not None and terrain_datum is not None and declared_datum.strip() != terrain_datum:
        raise TriangularBusinessError("initial-condition vertical_datum does not match terrain vertical_datum")
    if declared_datum is not None:
        return declared_datum.strip(), "config_declaration"
    if terrain_datum is not None:
        return terrain_datum, "terrain_metadata"
    return None, None


def _build_roughness(mesh: TriangularMesh, config: ConstantRoughnessInput | FaceRoughnessInput) -> TriangularRoughnessField:
    if isinstance(config, ConstantRoughnessInput):
        return TriangularRoughnessField.from_constant(mesh, config.manning_n, source=config.source)
    return TriangularRoughnessField.from_assignments(
        mesh, tuple((item.face_id, item.manning_n) for item in config.assignments), source=config.source
    )


def _build_initial_condition(
    mesh: TriangularMesh,
    terrain: FaceTerrainField,
    config: ConstantInitialInput | FaceInitialInput,
) -> TriangularInitialConditionField:
    if isinstance(config, ConstantInitialInput):
        return TriangularInitialConditionField.from_constant(
            mesh, terrain, config.quantity, config.value_m,
            source=config.source, vertical_datum=config.vertical_datum,
        )
    return TriangularInitialConditionField.from_assignments(
        mesh, terrain, config.quantity,
        tuple((item.face_id, item.value) for item in config.assignments),
        source=config.source, vertical_datum=config.vertical_datum,
    )


def _build_boundary_binding(
    mesh: TriangularMesh,
    terrain: FaceTerrainField,
    config: ConstantBoundaryInput | TimeSeriesBoundaryInput,
) -> TriangularBoundaryBinding:
    group_name = config.group_name
    if group_name in mesh.constraint_groups:
        raise TriangularBusinessError(f"'{group_name}' is an internal constraint group and cannot receive boundary forcing")
    if group_name not in mesh.boundary_groups:
        raise TriangularBusinessError(f"unknown exterior boundary group '{group_name}'")
    if config.quantity == "water_level":
        if terrain.vertical_datum is None:
            raise TriangularBusinessError("water_level boundary requires known terrain vertical_datum")
        if config.vertical_datum != terrain.vertical_datum:
            raise TriangularBusinessError("water_level boundary vertical_datum must match terrain vertical_datum")
        quantity = BoundaryQuantity.WATER_LEVEL
        unit = "m"
        interpretation = "absolute_water_level"
    else:
        quantity = BoundaryQuantity.DISCHARGE
        unit = "m3/s"
        interpretation = "total_group_discharge"
    edge_ids = tuple(mesh.boundary_groups[group_name])
    edge_nodes = tuple(mesh.edge_nodes[index] for index in edge_ids)
    is_series = isinstance(config, TimeSeriesBoundaryInput)
    return TriangularBoundaryBinding(
        mesh.mesh_id or "", topology_fingerprint(mesh), group_name, quantity,
        "timeseries" if is_series else "constant", edge_ids, edge_nodes,
        tuple(config.values if is_series else (config.value,)),
        tuple(config.time_s) if is_series else None,
        unit, interpretation, config.source, config.vertical_datum,
    )


def load_triangular_business_config(path: str | Path) -> TriangularBusinessConfig:
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TriangularBusinessError(f"Business preparation config not found: {source}") from exc
    except yaml.YAMLError as exc:
        raise TriangularBusinessError(f"Invalid business preparation YAML '{source}': {exc}") from exc
    if not isinstance(raw, dict):
        raise TriangularBusinessError("business preparation config must be a YAML mapping")
    try:
        return TriangularBusinessConfig.model_validate(raw)
    except ValidationError as exc:
        details = "\n".join(f"- {'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors())
        raise TriangularBusinessError(f"Invalid triangular business config:\n{details}") from exc


def _field_values_payload(values: Sequence[float | None], valid: Sequence[bool]) -> tuple[list[float | None], list[bool]]:
    return [None if value is None else float(value) for value in values], [bool(value) for value in valid]


def write_triangular_business_package(
    path: str | Path,
    package: TriangularBusinessPackage,
    *,
    mesh_artifact: str | Path,
) -> Path:
    """Write a linked business input package; no solver or edge-flux allocation is performed."""
    target = Path(path).resolve()
    mesh_path = Path(mesh_artifact).resolve()
    if target == mesh_path:
        raise TriangularBusinessError("business output_file must not overwrite the mesh/terrain UGRID artifact")
    try:
        linked_mesh, linked_terrain = read_ugrid_preparation(mesh_path)
    except (MeshPreparationError, TriangularMeshError) as exc:
        raise TriangularBusinessError(f"linked mesh preparation artifact is invalid: {exc}") from exc
    if topology_fingerprint(package.mesh) != topology_fingerprint(linked_mesh):
        raise TriangularBusinessError("business package mesh topology does not match linked mesh artifact")
    if terrain_fingerprint(package.mesh, package.terrain) != terrain_fingerprint(linked_mesh, linked_terrain):
        raise TriangularBusinessError("business package terrain contents/provenance do not match linked mesh artifact")
    relative_mesh = Path(os.path.relpath(mesh_path, target.parent)).as_posix()
    rough_values, rough_valid = _field_values_payload(package.roughness.manning_n, package.roughness.valid_mask)
    initial_values, initial_valid = _field_values_payload(package.initial_condition.values_m, package.initial_condition.valid_mask)
    payload = {
        "format": "triangular-business-preparation-v2",
        "product_type": "triangular_business_input_preparation",
        "preparation_only": True,
        "simulation_result": False,
        "mesh_artifact": relative_mesh,
        "mesh_id": package.mesh.mesh_id,
        "topology_fingerprint": topology_fingerprint(package.mesh),
        "terrain_fingerprint": terrain_fingerprint(package.mesh, package.terrain),
        "coordinate_system": package.mesh.coordinate_system,
        "face_id": list(package.mesh.face_id),
        "terrain": {
            "vertical_unit": package.terrain.vertical_unit,
            "vertical_unit_source": package.terrain.vertical_unit_source,
            "source_vertical_unit": package.terrain.source_vertical_unit,
            "vertical_datum": package.terrain.vertical_datum,
            "vertical_datum_source": package.terrain.vertical_datum_source,
            "source_vertical_datum": package.terrain.source_vertical_datum,
        },
        "roughness": {
            "quantity": package.roughness.quantity,
            "unit": package.roughness.unit,
            "assignment_mode": package.roughness.assignment_mode,
            "face_id": list(package.roughness.face_id),
            "values": rough_values,
            "valid_mask": rough_valid,
            "source": package.roughness.source,
        },
        "initial_condition": {
            "quantity": package.initial_condition.quantity.value,
            "unit": package.initial_condition.unit,
            "assignment_mode": package.initial_condition.assignment_mode,
            "face_id": list(package.initial_condition.face_id),
            "values_m": initial_values,
            "valid_mask": initial_valid,
            "vertical_datum": package.initial_condition.vertical_datum,
            "vertical_datum_source": package.initial_condition.vertical_datum_source,
            "source_terrain_vertical_datum": package.initial_condition.source_terrain_vertical_datum,
            "source": package.initial_condition.source,
        },
        "boundary_bindings": [
            {
                "mesh_id": binding.mesh_id,
                "topology_fingerprint": binding.topology_fingerprint,
                "group_name": binding.group_name,
                "quantity": binding.quantity.value,
                "mode": binding.mode,
                "edge_ids": list(binding.edge_ids),
                "edge_nodes": [list(pair) for pair in binding.edge_nodes],
                "values": list(binding.values),
                "time_s": None if binding.time_s is None else list(binding.time_s),
                "unit": binding.unit,
                "interpretation": binding.interpretation,
                "vertical_datum": binding.vertical_datum,
                "source": binding.source,
            }
            for binding in package.boundary_bindings
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        raise TriangularBusinessError(f"Could not write triangular business package '{target}': {exc}") from exc
    return target


def prepare_triangular_business(config_path: str | Path) -> TriangularBusinessResult:
    """Load a P2 UGRID artifact, prepare P3-A fields, and write one linked JSON package."""
    config_path = Path(config_path).resolve()
    config = load_triangular_business_config(config_path)
    mesh_path = (config_path.parent / config.mesh_file).resolve()
    output_path = (config_path.parent / config.output_file).resolve()
    if output_path in {config_path, mesh_path}:
        raise TriangularBusinessError("output_file must not overwrite the config or referenced mesh artifact")
    mesh, terrain = read_ugrid_preparation(mesh_path)
    roughness = _build_roughness(mesh, config.roughness)
    initial = _build_initial_condition(mesh, terrain, config.initial_condition)
    boundaries = tuple(_build_boundary_binding(mesh, terrain, item) for item in config.boundary_bindings)
    package = TriangularBusinessPackage(mesh, terrain, roughness, initial, boundaries)
    artifact = write_triangular_business_package(output_path, package, mesh_artifact=mesh_path)
    report = {
        "preparation_only": True,
        "simulation_result": False,
        "mesh_id": mesh.mesh_id,
        "topology_fingerprint": topology_fingerprint(mesh),
        "face_count": mesh.n_face,
        "roughness_valid_faces": sum(roughness.valid_mask),
        "initial_condition_valid_faces": sum(initial.valid_mask),
        "boundary_bindings": len(boundaries),
        "boundary_edges_bound": sum(len(binding.edge_ids) for binding in boundaries),
        "artifact": str(artifact),
    }
    return TriangularBusinessResult(package, artifact, report)


def read_triangular_business_package(path: str | Path) -> TriangularBusinessPackage:
    """Read and strictly revalidate one P3-A JSON package against its linked UGRID mesh."""
    source = Path(path).resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TriangularBusinessError(f"Triangular business package not found: {source}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TriangularBusinessError(f"Invalid triangular business package JSON '{source}': {exc}") from exc
    if not isinstance(data, dict):
        raise TriangularBusinessError("triangular business package root must be an object")
    if data.get("format") == "triangular-business-preparation-v1":
        raise TriangularBusinessError("legacy v1 business package has no terrain_fingerprint; regenerate it with the current preparation command")
    expected_root_keys = {
        "format", "product_type", "preparation_only", "simulation_result", "mesh_artifact",
        "mesh_id", "topology_fingerprint", "terrain_fingerprint", "coordinate_system", "face_id", "terrain",
        "roughness", "initial_condition", "boundary_bindings",
    }
    if set(data) != expected_root_keys:
        missing = expected_root_keys - set(data)
        extra = set(data) - expected_root_keys
        raise TriangularBusinessError(f"business package keys mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    if data["format"] != "triangular-business-preparation-v2" or data["product_type"] != "triangular_business_input_preparation":
        raise TriangularBusinessError("unsupported triangular business package format/product_type")
    if data["preparation_only"] is not True or data["simulation_result"] is not False:
        raise TriangularBusinessError("business package must be preparation_only and not a simulation result")
    mesh_ref = data["mesh_artifact"]
    if not isinstance(mesh_ref, str) or not mesh_ref.strip() or Path(mesh_ref).is_absolute() or PureWindowsPath(mesh_ref).is_absolute() or PureWindowsPath(mesh_ref).drive:
        raise TriangularBusinessError("mesh_artifact must be a non-empty relative path")
    mesh_path = (source.parent / mesh_ref).resolve()
    try:
        mesh, terrain = read_ugrid_preparation(mesh_path)
    except (MeshPreparationError, TriangularMeshError) as exc:
        raise TriangularBusinessError(f"linked mesh preparation artifact is invalid: {exc}") from exc
    fingerprint = topology_fingerprint(mesh)
    if data["mesh_id"] != mesh.mesh_id or data["topology_fingerprint"] != fingerprint:
        raise TriangularBusinessError("business package mesh_id/topology_fingerprint does not match linked mesh")
    if data["coordinate_system"] != mesh.coordinate_system:
        raise TriangularBusinessError("business package coordinate_system does not match linked mesh")
    if data["terrain_fingerprint"] != terrain_fingerprint(mesh, terrain):
        raise TriangularBusinessError("business package terrain_fingerprint does not match linked UGRID terrain contents/provenance")
    if not isinstance(data["face_id"], list) or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in data["face_id"]):
        raise TriangularBusinessError("business package face_id values must be non-negative integers")
    if tuple(data["face_id"]) != tuple(mesh.face_id):
        raise TriangularBusinessError("business package face_id list does not match linked mesh ordering")
    expected_terrain = {
        "vertical_unit": terrain.vertical_unit,
        "vertical_unit_source": terrain.vertical_unit_source,
        "source_vertical_unit": terrain.source_vertical_unit,
        "vertical_datum": terrain.vertical_datum,
        "vertical_datum_source": terrain.vertical_datum_source,
        "source_vertical_datum": terrain.source_vertical_datum,
    }
    if data["terrain"] != expected_terrain:
        raise TriangularBusinessError("business package terrain unit/datum provenance does not match linked UGRID terrain")

    rough = data["roughness"]
    if not isinstance(rough, dict) or set(rough) != {"quantity", "unit", "assignment_mode", "face_id", "values", "valid_mask", "source"}:
        raise TriangularBusinessError("business package roughness keys do not match the v2 schema")
    roughness = TriangularRoughnessField(
        mesh.mesh_id or "", fingerprint, tuple(rough["face_id"]), tuple(rough["values"]),
        tuple(rough["valid_mask"]), rough["source"], rough["assignment_mode"], rough["quantity"], rough["unit"],
    )
    roughness.validate_for(mesh)

    initial_data = data["initial_condition"]
    initial_keys = {"quantity", "unit", "assignment_mode", "face_id", "values_m", "valid_mask", "vertical_datum", "vertical_datum_source", "source_terrain_vertical_datum", "source"}
    if not isinstance(initial_data, dict) or set(initial_data) != initial_keys:
        raise TriangularBusinessError("business package initial_condition keys do not match the v2 schema")
    initial = TriangularInitialConditionField(
        mesh.mesh_id or "", fingerprint, tuple(initial_data["face_id"]), initial_data["quantity"],
        tuple(initial_data["values_m"]), tuple(initial_data["valid_mask"]), initial_data["source"],
        initial_data["assignment_mode"], initial_data["vertical_datum"], initial_data["vertical_datum_source"],
        initial_data["source_terrain_vertical_datum"], initial_data["unit"],
    )
    initial.validate_for(mesh, terrain)

    boundary_values = data["boundary_bindings"]
    if not isinstance(boundary_values, list):
        raise TriangularBusinessError("boundary_bindings must be a JSON array")
    bindings: list[TriangularBoundaryBinding] = []
    binding_keys = {
        "mesh_id", "topology_fingerprint", "group_name", "quantity", "mode", "edge_ids",
        "edge_nodes", "values", "time_s", "unit", "interpretation", "vertical_datum", "source",
    }
    for index, item in enumerate(boundary_values):
        if not isinstance(item, dict) or set(item) != binding_keys:
            raise TriangularBusinessError(f"boundary_bindings[{index}] keys do not match the v2 schema")
        binding = TriangularBoundaryBinding(
            item["mesh_id"], item["topology_fingerprint"], item["group_name"], item["quantity"],
            item["mode"], tuple(item["edge_ids"]), tuple(tuple(pair) for pair in item["edge_nodes"]),
            tuple(item["values"]), None if item["time_s"] is None else tuple(item["time_s"]),
            item["unit"], item["interpretation"], item["source"], item["vertical_datum"],
        )
        binding.validate_for(mesh, terrain)
        bindings.append(binding)
    return TriangularBusinessPackage(mesh, terrain, roughness, initial, tuple(bindings))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare triangular roughness, initial conditions, and boundary references; does not run a solver.")
    parser.add_argument("config", help="YAML triangular business-preparation config")
    args = parser.parse_args(argv)
    try:
        result = prepare_triangular_business(args.config)
    except (TriangularBusinessError, MeshPreparationError, TriangularMeshError) as exc:
        parser.error(str(exc))
    print(json.dumps(result.report, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "BoundaryInput", "ConstantBoundaryInput", "ConstantInitialInput", "ConstantRoughnessInput",
    "FaceAssignment", "FaceInitialInput", "FaceRoughnessInput", "TimeSeriesBoundaryInput",
    "TriangularBoundaryBinding", "TriangularBusinessConfig", "TriangularBusinessError",
    "TriangularBusinessPackage", "TriangularBusinessResult", "TriangularInitialConditionField",
    "TriangularRoughnessField", "load_triangular_business_config", "main",
    "prepare_triangular_business", "read_triangular_business_package", "terrain_fingerprint", "topology_fingerprint",
    "write_triangular_business_package",
]


if __name__ == "__main__":  # pragma: no cover - exercised as a module entry point
    raise SystemExit(main())
