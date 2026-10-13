from __future__ import annotations
from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Sequence
from .config import OutputVariable
from .gate_dispatch import GateDispatchInput
class OutputError(ValueError): pass
_UNITS={"water_depth":"m","water_level":"m","velocity_x":"m/s","velocity_y":"m/s","velocity_magnitude":"m/s","discharge":"m3/s","maximum_depth":"m","maximum_velocity":"m/s","arrival_time":"s","froude_number":"1"}
@dataclass(frozen=True)
class OutputField:
    variable: OutputVariable | str
    values: Sequence
    nx:int; ny:int; dx:float; dy:float; xmin:float; ymin:float
    times: Sequence[Real] | None = None
    coordinate_system: str | None = None
    vertical_datum: str | None = None
    valid_mask: Sequence | None = None
    nodata_mask: Sequence | None = None
    def __post_init__(self):
        name=self.variable.value if isinstance(self.variable,OutputVariable) else str(self.variable)
        if name not in _UNITS: raise OutputError("unsupported output variable")
        object.__setattr__(self,"variable",OutputVariable(name))
        shape=self._shape(self.values)
        if not isinstance(self.nx,int) or self.nx<=0 or not isinstance(self.ny,int) or self.ny<=0: raise OutputError("nx and ny must be positive integers")
        if not isfinite(float(self.dx)) or self.dx<=0 or not isfinite(float(self.dy)) or self.dy<=0: raise OutputError("dx and dy must be finite and positive")
        spatial=(self.ny,self.nx)
        if shape not in (spatial,) and not (len(shape)==3 and shape[1:]==spatial): raise OutputError("values shape must be (ny,nx) or (nt,ny,nx)")
        if len(shape)==3 and self.times is None: raise OutputError("times are required for a time-series field")
        for row in self._iter_values(self.values):
            for v in row:
                if isinstance(v,bool) or not isinstance(v,Real) or not isfinite(float(v)): raise OutputError("output values must be finite numbers")
        if self.times is not None:
            ts=tuple(float(t) for t in self.times)
            if len(shape)!=3 or len(ts)!=shape[0] or any(not isfinite(t) for t in ts) or any(b<=a for a,b in zip(ts,ts[1:])): raise OutputError("times must be finite, strictly increasing, and match time dimension")
            object.__setattr__(self,"times",ts)
        if self.variable is OutputVariable.WATER_LEVEL and (not self.vertical_datum or not self.vertical_datum.strip()): raise OutputError("water_level requires vertical_datum")
        for mask in (self.valid_mask, self.nodata_mask):
            if mask is not None:
                if self._shape(mask) != spatial: raise OutputError("mask shape must be (ny,nx)")
                if any(not isinstance(v, bool) for row in mask for v in row): raise OutputError("masks must contain booleans")
        if self.valid_mask is not None and self.nodata_mask is not None and any(a and b for ra, rb in zip(self.valid_mask, self.nodata_mask) for a, b in zip(ra, rb)): raise OutputError("valid_mask and nodata_mask cannot overlap")
    @staticmethod
    def _shape(v):
        try:
            a=len(v); b=len(v[0])
            if b and hasattr(v[0][0], "__len__"): return (a,b,len(v[0][0]))
            return (a,b)
        except Exception: raise OutputError("values must be a rectangular array")

    @staticmethod
    def _iter_values(v):
        return v if not hasattr(v[0][0],'__len__') else [row for plane in v for row in plane]
    @property
    def unit(self): return _UNITS[self.variable.value]
    @property
    def shape(self): return self._shape(self.values)
@dataclass
class SimulationOutput:
    fields:list[OutputField]
    format:str="netcdf"
    metadata:dict|None=None
    gate_dispatch:GateDispatchInput|None=None
    def add_field(self, field): self.fields.append(field)
    def validate(self):
        if self.gate_dispatch is not None and not isinstance(self.gate_dispatch, GateDispatchInput): raise OutputError("gate_dispatch must be GateDispatchInput or None")
        if self.format not in {"netcdf","geotiff","csv"}: raise OutputError("unsupported output format")
        if len({f.variable for f in self.fields}) != len(self.fields): raise OutputError("duplicate output variable")
        return self
ResultDataset=SimulationOutput
__all__=["OutputError","OutputField","SimulationOutput","ResultDataset"]
