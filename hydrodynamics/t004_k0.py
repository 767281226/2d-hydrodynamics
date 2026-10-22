"""Bounded T004-K0 single-edge discharge closure and triangular CFL diagnostics.

This module is deliberately isolated from solver and run code. It certifies a
single, fixed-direction discharge ghost state and reports the corresponding
binary64 HLL flux. The triangle/domain helpers only check supplied stage states
against a CFL bound; they never advance or modify a state.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
import struct
from typing import Literal, Sequence

from .config import BoundaryQuantity, InitialConditionQuantity
from .triangular_business import (
    TriangularBoundaryBinding,
    TriangularBusinessPackage,
    TriangularBusinessError,
    topology_fingerprint,
)
from .triangular_mesh import TriangularMesh


GRAVITY_M_S2 = 9.81
_G = Fraction.from_float(GRAVITY_M_S2)
_SQRT_BITS = 256
_ROOT_DERIVATIVE_SUBDIVISIONS = 256
_DEFAULT_MAX_BISECTIONS = 128
_MAX_BISECTIONS = 512


class K0Error(ValueError):
    """Input, proof, physical-identity, or diagnostic failure with a stable code."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, order=True)
class _I:
    lo: Fraction
    hi: Fraction

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            raise ArithmeticError("invalid interval endpoints")

    @classmethod
    def point(cls, value: Fraction | int) -> "_I":
        item = Fraction(value)
        return cls(item, item)

    def __add__(self, other: "_I | Fraction | int") -> "_I":
        rhs = _interval(other)
        return _I(self.lo + rhs.lo, self.hi + rhs.hi)

    __radd__ = __add__

    def __neg__(self) -> "_I":
        return _I(-self.hi, -self.lo)

    def __sub__(self, other: "_I | Fraction | int") -> "_I":
        return self + (-_interval(other))

    def __rsub__(self, other: "_I | Fraction | int") -> "_I":
        return _interval(other) - self

    def __mul__(self, other: "_I | Fraction | int") -> "_I":
        rhs = _interval(other)
        products = (self.lo * rhs.lo, self.lo * rhs.hi, self.hi * rhs.lo, self.hi * rhs.hi)
        return _I(min(products), max(products))

    __rmul__ = __mul__

    def __truediv__(self, other: "_I | Fraction | int") -> "_I":
        rhs = _interval(other)
        if rhs.lo <= 0 <= rhs.hi:
            raise ArithmeticError("interval divisor contains zero")
        reciprocals = (Fraction(1, 1) / rhs.lo, Fraction(1, 1) / rhs.hi)
        return self * _I(min(reciprocals), max(reciprocals))

    def square(self) -> "_I":
        if self.lo <= 0 <= self.hi:
            return _I(Fraction(0), max(self.lo * self.lo, self.hi * self.hi))
        values = self.lo * self.lo, self.hi * self.hi
        return _I(min(values), max(values))

    def abs_upper(self) -> Fraction:
        return max(abs(self.lo), abs(self.hi))


def _interval(value: _I | Fraction | int) -> _I:
    return value if isinstance(value, _I) else _I.point(Fraction(value))


def _minimum(values: Sequence[_I]) -> _I:
    return _I(min(value.lo for value in values), min(value.hi for value in values))


def _maximum(values: Sequence[_I]) -> _I:
    return _I(max(value.lo for value in values), max(value.hi for value in values))


def _sqrt_interval(value: _I, bits: int = _SQRT_BITS) -> _I:
    if value.lo < 0:
        raise ArithmeticError("square-root interval must be non-negative")

    def bounds(number: Fraction) -> tuple[Fraction, Fraction]:
        scaled_floor = (number.numerator << (2 * bits)) // number.denominator
        root = math.isqrt(scaled_floor)
        lower = Fraction(root, 1 << bits)
        exact = root * root * number.denominator == number.numerator << (2 * bits)
        return lower, lower if exact else Fraction(root + 1, 1 << bits)

    lower, _ = bounds(value.lo)
    _, upper = bounds(value.hi)
    return _I(lower, upper)


@dataclass(frozen=True)
class _D:
    value: _I
    derivative: _I

    @classmethod
    def constant(cls, value: _I | Fraction | int) -> "_D":
        return cls(_interval(value), _I.point(0))

    def __add__(self, other: "_D | _I | Fraction | int") -> "_D":
        rhs = _dual(other)
        return _D(self.value + rhs.value, self.derivative + rhs.derivative)

    __radd__ = __add__

    def __neg__(self) -> "_D":
        return _D(-self.value, -self.derivative)

    def __sub__(self, other: "_D | _I | Fraction | int") -> "_D":
        return self + (-_dual(other))

    def __rsub__(self, other: "_D | _I | Fraction | int") -> "_D":
        return _dual(other) - self

    def __mul__(self, other: "_D | _I | Fraction | int") -> "_D":
        rhs = _dual(other)
        return _D(self.value * rhs.value, self.derivative * rhs.value + self.value * rhs.derivative)

    __rmul__ = __mul__

    def __truediv__(self, other: "_D | _I | Fraction | int") -> "_D":
        rhs = _dual(other)
        derivative = (self.derivative * rhs.value - self.value * rhs.derivative) / rhs.value.square()
        return _D(self.value / rhs.value, derivative)


def _dual(value: _D | _I | Fraction | int) -> _D:
    return value if isinstance(value, _D) else _D.constant(value)


def _fraction(value: int | float, name: str, *, positive: bool = False) -> tuple[float, Fraction]:
    if type(value) not in (int, float):
        raise K0Error("D0_POLICY_INPUT_INVALID", f"{name} must be an int or float, excluding bool")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise K0Error("D0_POLICY_INPUT_INVALID", f"{name} must be a finite binary64 number") from exc
    if not math.isfinite(number) or (positive and number <= 0.0):
        constraint = "finite and greater than zero" if positive else "finite"
        raise K0Error("D0_POLICY_INPUT_INVALID", f"{name} must be {constraint}")
    return number, Fraction.from_float(number)


def _strict_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise K0Error("D0_POLICY_INPUT_INVALID", f"{name} must be a strict int; bool and integral floats are rejected")
    if not minimum <= value <= maximum:
        raise K0Error("D0_POLICY_INPUT_INVALID", f"{name} must be in [{minimum}, {maximum}]")
    return value


def root_precision_gate(
    *,
    root_exists_bracketed: bool,
    strict_endpoint_signs: bool,
    equal_state_identity: bool = False,
    bracket_width_m_s: Fraction,
    target_width_m_s: Fraction,
    steps_used: int,
    max_bisections: int,
) -> dict[str, object]:
    """Apply the precision/resource admission rule to an already formed bracket."""
    if any(type(flag) is not bool for flag in (root_exists_bracketed, strict_endpoint_signs, equal_state_identity)):
        raise K0Error("D0_POLICY_INPUT_INVALID", "root proof flags must be strict bool values")
    if type(bracket_width_m_s) is not Fraction or type(target_width_m_s) is not Fraction:
        raise K0Error("D0_POLICY_INPUT_INVALID", "root bracket and target widths must be exact Fraction values")
    steps = _strict_int(steps_used, "steps_used", 0, _MAX_BISECTIONS)
    cap = _strict_int(max_bisections, "max_bisections", 1, _MAX_BISECTIONS)
    if bracket_width_m_s < 0 or target_width_m_s <= 0:
        raise K0Error("D0_POLICY_INPUT_INVALID", "root bracket width must be non-negative and target width positive")
    precision_met = bracket_width_m_s <= target_width_m_s
    accepted = root_exists_bracketed and (strict_endpoint_signs or equal_state_identity) and precision_met and steps <= cap
    return {
        "root_exists_bracketed": root_exists_bracketed,
        "strict_endpoint_signs": strict_endpoint_signs,
        "equal_state_identity": equal_state_identity,
        "precision_goal_met": precision_met,
        "steps_used": steps,
        "max_bisections": cap,
        "accepted": accepted,
        "error": None if accepted else "D0_HLL_Q_ROOT_NOT_CERTIFIED",
        "root_certificate": "admissible" if accepted else None,
    }


def _float_bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def _fp(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class K0State:
    """Fully wet face state: depth in m and global velocity components in m/s."""

    h_m: float
    u_x_m_s: float
    u_y_m_s: float

    def __post_init__(self) -> None:
        for name in ("h_m", "u_x_m_s", "u_y_m_s"):
            number, _ = _fraction(getattr(self, name), name, positive=(name == "h_m"))
            object.__setattr__(self, name, number)


@dataclass(frozen=True)
class K0PhysicalEdge:
    """Re-derived identity and outward geometry of one actual P2 exterior edge."""

    mesh_id: str
    topology_fingerprint: str
    edge_index: int
    node_pair: tuple[int, int]
    owner_face_index: int
    owner_face_id: int
    boundary_group: str
    normal_x: float
    normal_y: float
    length_m: float
    provenance: Literal["P2_EXTERIOR"] = "P2_EXTERIOR"


@dataclass(frozen=True)
class K0BoundaryRequest:
    """One-edge discharge request prepared from an actual business binding."""

    package: TriangularBusinessPackage
    binding: TriangularBoundaryBinding
    edge: K0PhysicalEdge
    owner_state: K0State
    stage_index: int
    direction: Literal["inflow", "outflow"]
    q_target_m2_s: Fraction
    group_discharge_m3_s: Fraction
    inflow_tangential_velocity_m_s: float | None
    max_bisections: int


@dataclass(frozen=True)
class K0RootCertificate:
    """Exact-rational root enclosure and uniqueness/precision proof summary."""

    c_g_interval_m_s: tuple[Fraction, Fraction]
    proof_type: Literal["strict_endpoint_signs_and_negative_derivative", "equal_state_identity_and_negative_derivative"]
    endpoint_residual_intervals_m2_s: tuple[tuple[Fraction, Fraction], tuple[Fraction, Fraction]] | None
    derivative_intervals_dF_dcg_m: tuple[tuple[Fraction, Fraction], ...]
    m_F_inverse_lower_m: Fraction
    M_F_forward_upper_m: Fraction
    target_half_ulp_m_s: Fraction
    steps_used: int


@dataclass(frozen=True)
class K0HllFlux:
    """Actual binary64 HLL flux in outward normal/tangent and global axes."""

    mass_m2_s: float
    momentum_normal_m3_s2: float
    momentum_tangential_m3_s2: float
    momentum_x_m3_s2: float
    momentum_y_m3_s2: float
    wave_speed_left_m_s: float
    wave_speed_right_m_s: float


@dataclass(frozen=True)
class K0SyntheticHllDiagnostic:
    """Local Riemann counterexample output; it has no P2 boundary identity."""

    provenance: Literal["SYNTHETIC_INTERFACE"]
    direction: Literal["inflow", "outflow"]
    hll_flux: K0HllFlux
    direction_matches_actual_flux: bool
    boundary_applied: Literal[False] = False
    solver_completed: Literal[False] = False


@dataclass(frozen=True)
class K0UncertaintyBudget:
    """R4-style absolute Q error budget; all bounds are exact Fractions internally."""

    actual_abs_q_residual_m2_s: Fraction
    E_input_m2_s: Fraction
    E_root_m2_s: Fraction
    E_ghost_m2_s: Fraction
    E_eval_m2_s: Fraction
    E_q_m2_s: Fraction
    tau_edge_m2_s: Fraction
    actual_abs_group_residual_m3_s: Fraction
    tau_group_m3_s: Fraction
    gamma32_A_KD_m2_s: Fraction


@dataclass(frozen=True)
class K0BoundaryDiagnostic:
    """Diagnostic-only output. It cannot be committed as a boundary or solver result."""

    edge: K0PhysicalEdge
    owner_state: K0State
    owner_normal_velocity_m_s: Fraction
    owner_discharge_m2_s: Fraction
    outgoing_characteristic_interval_m_s: tuple[Fraction, Fraction]
    ghost_state: K0State
    ghost_state_id: str
    direction: Literal["inflow", "outflow"]
    q_requested_m2_s: Fraction
    group_discharge_requested_signed_m3_s: Fraction
    group_discharge_actual_signed_m3_s: Fraction
    root: K0RootCertificate
    continuous_trace: dict[str, object]
    hll_flux: K0HllFlux
    uncertainty: K0UncertaintyBudget
    input_fingerprint: str
    calculation_fingerprint: str
    root_consumer_fingerprint: str
    uncertainty_consumer_fingerprint: str
    flux_consumer_fingerprint: str
    stage_index: int
    boundary_applied: Literal[False] = False
    solver_completed: Literal[False] = False
    result_artifacts_written: Literal[False] = False


@dataclass(frozen=True)
class K0CflEdgeInput:
    """Actual right neighbor or explicit ghost for one owner-edge stage state."""

    edge_index: int
    right_state: K0State
    neighbor_face_id: int | None = None
    ghost_state_id: str | None = None
    boundary_request: K0BoundaryRequest | None = None


@dataclass(frozen=True)
class K0CellStageInput:
    """One cell state plus all three actual per-edge right states for one stage."""

    face_id: int
    owner_state: K0State
    edges: Sequence[K0CflEdgeInput]


@dataclass(frozen=True)
class K0StageDomainInput:
    """Supplied complete domain state for stage 0 or stage 1; no update is performed."""

    stage_index: int
    cells: Sequence[K0CellStageInput]


@dataclass(frozen=True)
class K0CflResult:
    """Read-only CFL decision from supplied stage 0/1 states."""

    accepted: bool
    dt_s: Fraction
    safe_dt_limit_s: Fraction
    stage_limits_s: tuple[Fraction, Fraction]
    limiting_stage_index: int
    limiting_face_id: int
    per_stage_cell_limits_s: tuple[tuple[int, Fraction], ...]
    per_edge_alpha_upper_m_s: tuple[tuple[int, int, int, Fraction], ...]
    cfl_number: Fraction
    input_fingerprints: tuple[str, ...]
    calculation_fingerprints: tuple[str, ...]
    state_advanced: Literal[False] = False


def _edge_normal(mesh: TriangularMesh, edge_index: int, owner_face: int) -> tuple[float, float]:
    a, b = mesh.edge_nodes[edge_index]
    face = mesh.face_nodes[owner_face]
    directed: tuple[int, int] | None = None
    for first, second in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
        if {first, second} == {a, b}:
            directed = first, second
            break
    if directed is None:
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "edge nodes do not occur in the owner face")
    x0, y0 = mesh.node_xy[directed[0]]
    x1, y1 = mesh.node_xy[directed[1]]
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if not math.isfinite(length) or length <= 0.0:
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "edge length is non-positive or non-finite")
    nx, ny = dy / length, -dx / length
    midpoint_x, midpoint_y = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    cx, cy = mesh.face_centroid[owner_face]
    if (midpoint_x - cx) * nx + (midpoint_y - cy) * ny <= 0.0:
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "computed normal is not outward from the owner face")
    return nx, ny


def _derived_edge(mesh: TriangularMesh, edge_index: int, group_name: str) -> K0PhysicalEdge:
    owner_face = mesh.edge_faces[edge_index][0]
    nx, ny = _edge_normal(mesh, edge_index, owner_face)
    return K0PhysicalEdge(
        mesh_id=mesh.mesh_id or "",
        topology_fingerprint=topology_fingerprint(mesh),
        edge_index=edge_index,
        node_pair=tuple(mesh.edge_nodes[edge_index]),
        owner_face_index=owner_face,
        owner_face_id=int(mesh.face_id[owner_face]),
        boundary_group=group_name,
        normal_x=nx,
        normal_y=ny,
        length_m=float(mesh.edge_length[edge_index]),
    )


def resolve_p2_closed_wall_edge(package: TriangularBusinessPackage, edge_index: int) -> K0PhysicalEdge:
    """Resolve an unbound exterior edge for an explicit closed-wall CFL state."""
    if not isinstance(package, TriangularBusinessPackage):
        raise K0Error("D0_POLICY_INPUT_INVALID", "package must be a validated TriangularBusinessPackage")
    mesh = package.mesh
    if type(edge_index) is not int or not 0 <= edge_index < mesh.n_edge:
        raise K0Error("D0_POLICY_INPUT_INVALID", "edge_index must be a strict int naming an actual mesh edge")
    if mesh.edge_faces[edge_index][1] != -1:
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "closed-wall helper requires an actual exterior edge")
    if any(edge_index in binding.edge_ids for binding in package.boundary_bindings):
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "bound edges require a certified boundary ghost, not a wall helper")
    protected = edge_index in set(package.security_exclusion_boundary_edge_ids)
    group = "security_exclusion_boundary" if protected else "unbound_closed_wall"
    return _derived_edge(mesh, edge_index, group)


def _validate_physical_edge(package: TriangularBusinessPackage, binding: TriangularBoundaryBinding,
                            edge_index: int) -> K0PhysicalEdge:
    mesh = package.mesh
    try:
        binding.validate_for(mesh, package.terrain)
    except TriangularBusinessError as exc:
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", str(exc)) from exc
    if type(edge_index) is not int or not 0 <= edge_index < mesh.n_edge:
        raise K0Error("D0_POLICY_INPUT_INVALID", "edge_index must be a strict int naming an actual mesh edge")
    if edge_index not in tuple(binding.edge_ids):
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "selected edge is not in the binding's actual edge_ids")
    if mesh.edge_faces[edge_index][1] != -1:
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "internal or constraint edges cannot receive K0 discharge forcing")
    actual_nodes = tuple(mesh.edge_nodes[edge_index])
    if actual_nodes not in tuple(tuple(pair) for pair in binding.edge_nodes):
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "selected node pair differs from the binding's actual edge_nodes")
    security_edges = set(package.security_exclusion_boundary_edge_ids)
    if set(binding.edge_ids).intersection(security_edges) or edge_index in security_edges:
        raise K0Error("D0_SECURITY_EXCLUSION_EDGE_FORBIDDEN", "authenticated P2 security-exclusion edges remain closed walls, independent of group alias")
    if not any(candidate is binding for candidate in package.boundary_bindings):
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "binding is not one of this validated business package's bindings")
    return _derived_edge(mesh, edge_index, binding.group_name)


def _validate_owner_state(package: TriangularBusinessPackage, edge: K0PhysicalEdge,
                          state: K0State, stage_index: int) -> tuple[float, Fraction, float, Fraction, float, Fraction]:
    stage = _strict_int(stage_index, "stage_index", 0, 1)
    mesh = package.mesh
    if (edge.mesh_id != mesh.mesh_id or edge.topology_fingerprint != topology_fingerprint(mesh)
            or edge.edge_index >= mesh.n_edge
            or tuple(mesh.edge_nodes[edge.edge_index]) != edge.node_pair
            or mesh.edge_faces[edge.edge_index] != (edge.owner_face_index, -1)
            or int(mesh.face_id[edge.owner_face_index]) != edge.owner_face_id):
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "edge identity no longer matches the active P2 mesh")
    if not package.terrain.valid_mask[edge.owner_face_index] or package.terrain.elevation_m[edge.owner_face_index] is None:
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "selected owner face has no valid flat-bed terrain sample")
    h, hf = _fraction(state.h_m, "owner h_m", positive=True)
    ux, uxf = _fraction(state.u_x_m_s, "owner u_x_m_s")
    uy, uyf = _fraction(state.u_y_m_s, "owner u_y_m_s")
    if not 0.8 <= h <= 1.0:
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "owner depth must be in [0.8, 1.0] m")
    nx, ny = edge.normal_x, edge.normal_y
    un = ux * nx + uy * ny
    if ux * (-ny) + uy * nx != 0.0:
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "this K0 closure batch requires zero owner tangential velocity")
    if not math.isfinite(un) or abs(un) > 1.2:
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "owner outward normal velocity must satisfy |u_n| <= 1.2 m/s")
    ci = math.sqrt(GRAVITY_M_S2 * h)
    if abs(un) >= ci:
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "owner state must be strictly subcritical")
    if stage == 0:
        initial = package.initial_condition
        raw = initial.values_m[edge.owner_face_index]
        if raw is None or not initial.valid_mask[edge.owner_face_index]:
            raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "stage-0 owner has no explicit initial condition")
        if initial.quantity is InitialConditionQuantity.DEPTH:
            expected_h = float(raw)
        else:
            elevation = package.terrain.elevation_m[edge.owner_face_index]
            if elevation is None:
                raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "stage-0 level cannot be compared without terrain")
            expected_h = float(raw) - float(elevation)
        if _float_bits(expected_h) != _float_bits(h):
            raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "stage-0 depth bits must match the linked business initial condition")
    return h, hf, ux, uxf, uy, uyf


def _validate_owner_direction(un: Fraction, direction: str, edge: K0PhysicalEdge,
                              stage_index: int, un_binary64: float) -> None:
    expected = "positive" if direction == "outflow" else "negative"
    matches_exact = (un > 0) if direction == "outflow" else (un < 0)
    matches_binary64 = (un_binary64 > 0.0) if direction == "outflow" else (un_binary64 < 0.0)
    if direction not in ("inflow", "outflow") or not matches_exact or not matches_binary64:
        raise K0Error(
            "D0_HLL_Q_OWNER_DIRECTION_INVALID",
            f"owner u_n must be strictly {expected} in exact-input and binary64 projections for {direction}; edge={edge.edge_index}, stage={stage_index}, exact={un}, binary64={un_binary64}",
        )


def prepare_p2_edge_request(
    package: TriangularBusinessPackage,
    binding: TriangularBoundaryBinding,
    edge_index: int,
    owner_state: K0State,
    *,
    stage_index: int,
    direction: Literal["inflow", "outflow"],
    inflow_tangential_velocity_m_s: float | None = None,
    max_bisections: int = _DEFAULT_MAX_BISECTIONS,
) -> K0BoundaryRequest:
    """Validate one actual P2 boundary edge and derive its signed edge q from total Q.

    This API accepts only a single-edge, constant, non-zero group discharge.
    The sign comes from the required direction field; the existing boundary
    binding remains a non-negative total-group Q value.
    """
    if not isinstance(package, TriangularBusinessPackage):
        raise K0Error("D0_POLICY_INPUT_INVALID", "package must be a validated TriangularBusinessPackage")
    if not isinstance(binding, TriangularBoundaryBinding):
        raise K0Error("D0_POLICY_INPUT_INVALID", "binding must be a TriangularBoundaryBinding")
    if not isinstance(owner_state, K0State):
        raise K0Error("D0_POLICY_INPUT_INVALID", "owner_state must be a K0State")
    if direction not in ("inflow", "outflow"):
        raise K0Error("D0_POLICY_INPUT_INVALID", "direction must be exactly inflow or outflow")
    stage = _strict_int(stage_index, "stage_index", 0, 1)
    cap = _strict_int(max_bisections, "max_bisections", 1, _MAX_BISECTIONS)
    edge = _validate_physical_edge(package, binding, edge_index)
    if getattr(binding.quantity, "value", binding.quantity) != BoundaryQuantity.DISCHARGE.value:
        raise K0Error("D0_POLICY_INPUT_INVALID", "K0 supports constant discharge bindings only")
    if binding.mode != "constant" or len(binding.values) != 1 or binding.time_s is not None:
        raise K0Error("D0_POLICY_INPUT_INVALID", "K0 requires one constant total-group discharge value")
    if len(binding.edge_ids) != 1 or tuple(binding.edge_ids) != (edge_index,):
        raise K0Error("D0_K0_SINGLE_EDGE_GROUP_REQUIRED", "this K0 release certifies exactly one physical edge per discharge binding")
    if not any(candidate is binding for candidate in package.boundary_bindings):
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "binding is not registered in the validated package")
    _, _, ux, _, uy, _ = _validate_owner_state(package, edge, owner_state, stage)
    if direction == "inflow":
        if inflow_tangential_velocity_m_s is None:
            raise K0Error("D0_POLICY_INPUT_INVALID", "inflow requires an explicit tangential velocity in m/s")
        tangent, tangent_fraction = _fraction(inflow_tangential_velocity_m_s, "inflow_tangential_velocity_m_s")
        if tangent != 0.0:
            raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "this K0 closure batch requires explicit zero inflow tangential velocity")
    else:
        if inflow_tangential_velocity_m_s is not None:
            raise K0Error("D0_POLICY_INPUT_INVALID", "outflow tangential velocity is taken from the owner state; an override is not accepted")
        tangent, tangent_fraction = 0.0, Fraction(0)
    if direction == "outflow":
        tangent = ux * (-edge.normal_y) + uy * edge.normal_x
        tangent_fraction = Fraction.from_float(tangent)
    Q_float, Q = _fraction(binding.values[0], "group discharge Q")
    if Q_float <= 0.0:
        raise K0Error("D0_HLL_Q_INPUT_DIRECTION_INVALID", "zero discharge has no fixed nonzero direction")
    length = Fraction.from_float(edge.length_m)
    q = Q / length
    if abs(q) > Fraction(6, 5):
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "edge q must satisfy |q_n| <= 1.2 m2/s")
    if direction == "inflow":
        q = -q
    # Verify the owner normal bound and subcritical condition against the exact
    # binary64 geometry/state bits consumed by the root solver.
    un = owner_state.u_x_m_s * edge.normal_x + owner_state.u_y_m_s * edge.normal_y
    un_exact = Fraction.from_float(owner_state.u_x_m_s) * Fraction.from_float(edge.normal_x) + Fraction.from_float(owner_state.u_y_m_s) * Fraction.from_float(edge.normal_y)
    _validate_owner_direction(un_exact, direction, edge, stage, un)
    ci = math.sqrt(GRAVITY_M_S2 * owner_state.h_m)
    if abs(un) > 1.2 or abs(un) >= ci:
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "owner normal state is outside the bounded strict-subcritical envelope")
    return K0BoundaryRequest(package, binding, edge, owner_state, stage, direction, q, Q,
                            tangent if direction == "inflow" else None, cap)


def _branch_interval(direction: str, j: _I) -> tuple[_I, _I]:
    if direction == "inflow":
        return j / 2, j
    return j / 3, j / 2


def _hll_mass_interval(cg: _I, *, hi: Fraction, ui: Fraction, g: Fraction,
                       ci: _I, j: _I) -> _I:
    hg = cg.square() / g
    ug = j - 2 * cg
    sl = _minimum((_I.point(0), _I.point(ui) - ci, ug - _sqrt_interval(hg * g)))
    sr = _maximum((_I.point(0), _I.point(ui) + ci, ug + _sqrt_interval(hg * g)))
    fl, fr = _I.point(hi * ui), hg * ug
    return (sr * fl - sl * fr + sl * sr * (hg - hi)) / (sr - sl)


def _hll_mass_branch_dual(c: _I, *, branch: Literal["lower", "upper"],
                          g: _I, h: _I, qi: _I, ci: _I, j: _I) -> _D:
    cd = _D(c, _I.point(1))
    gd, hd, qd, cid, jd = map(_D.constant, (g, h, qi, ci, j))
    if branch == "lower":
        a, b = jd - 3 * cid, jd - cd
        numerator = b * (qd - a * hd) + a * cd * cd * cd / gd
        denominator = 3 * cid - cd
    else:
        a, b = jd - 3 * cd, jd - cid
        numerator = b * (qd - a * hd) + a * (cd * cd * (2 * cd - cid)) / gd
        denominator = 3 * cd - cid
    return numerator / denominator


def _derivative_certificate(c_low: Fraction, c_high: Fraction, *, g: Fraction,
                            h: Fraction, ui: Fraction, ci: _I, j: _I,
                            subdivisions: int = _ROOT_DERIVATIVE_SUBDIVISIONS) -> tuple[tuple[tuple[Fraction, Fraction], ...], Fraction, Fraction]:
    qi = _I.point(h * ui)
    gi, h_interval = _I.point(g), _I.point(h)
    pieces: list[tuple[str, Fraction, Fraction]] = []
    if c_low < min(c_high, ci.hi):
        pieces.append(("lower", c_low, min(c_high, ci.hi)))
    if max(c_low, ci.lo) < c_high:
        pieces.append(("upper", max(c_low, ci.lo), c_high))
    if not pieces:
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "root interval does not intersect a certifiable HLL branch")
    slope_intervals: list[_I] = []
    for branch, start, end in pieces:
        slope_lo: Fraction | None = None
        slope_hi: Fraction | None = None
        for index in range(subdivisions):
            c0 = start + (end - start) * Fraction(index, subdivisions)
            c1 = start + (end - start) * Fraction(index + 1, subdivisions)
            slope = _hll_mass_branch_dual(_I(c0, c1), branch=branch, g=gi, h=h_interval,
                                         qi=qi, ci=ci, j=j).derivative
            slope_lo = slope.lo if slope_lo is None else min(slope_lo, slope.lo)
            slope_hi = slope.hi if slope_hi is None else max(slope_hi, slope.hi)
        if slope_lo is None or slope_hi is None:
            raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "empty derivative partition")
        slope_intervals.append(_I(slope_lo, slope_hi))
    if any(slope.hi >= 0 for slope in slope_intervals):
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "strictly negative HLL-Q derivative was not certified on every branch")
    m_f = -max(slope.hi for slope in slope_intervals)
    M_f = max(-slope.lo for slope in slope_intervals)
    if m_f <= 0 or M_f < m_f:
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "HLL-Q derivative conditioning bounds are invalid")
    return tuple((item.lo, item.hi) for item in slope_intervals), m_f, M_f


def _root_certificate(request: K0BoundaryRequest, *, h: Fraction, ui: Fraction) -> K0RootCertificate:
    q = request.q_target_m2_s
    ci = _sqrt_interval(_I.point(_G * h))
    j = _I.point(ui) + 2 * ci
    if j.lo <= 0:
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "outgoing characteristic J+ must be strictly positive")
    branch_low, branch_high = _branch_interval(request.direction, j)
    if branch_low.lo <= 0 or branch_low.hi >= branch_high.lo:
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "requested direction has no positive strict-subcritical ghost interval")
    derivative, m_f, M_f = _derivative_certificate(
        branch_low.lo, branch_high.hi, g=_G, h=h, ui=ui, ci=ci, j=j
    )
    identity = q == h * ui
    if identity:
        root = ci
        steps = 0
        proof: Literal["strict_endpoint_signs_and_negative_derivative", "equal_state_identity_and_negative_derivative"] = "equal_state_identity_and_negative_derivative"
        residuals = None
    else:
        lo, hi = branch_low.hi, branch_high.lo
        if lo >= hi:
            raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "rounding removed the open root-search interval")
        f_lo = _hll_mass_interval(_I.point(lo), hi=h, ui=ui, g=_G, ci=ci, j=j) - q
        f_hi = _hll_mass_interval(_I.point(hi), hi=h, ui=ui, g=_G, ci=ci, j=j) - q
        if f_lo.lo <= 0 or f_hi.hi >= 0:
            raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "no strict positive-to-negative HLL-Q endpoint signs were certified")
        steps = 0
        for _ in range(request.max_bisections):
            midpoint = (lo + hi) / 2
            residual = _hll_mass_interval(_I.point(midpoint), hi=h, ui=ui, g=_G, ci=ci, j=j) - q
            if residual.lo > 0:
                lo = midpoint
            elif residual.hi < 0:
                hi = midpoint
            else:
                raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "interval residual sign became ambiguous before the precision target")
            steps += 1
            c_approx = float((lo + hi) / 2)
            half_ulp = Fraction.from_float(math.ulp(c_approx)) / 2
            if hi - lo <= half_ulp:
                break
        root = _I(lo, hi)
        end_lo = _hll_mass_interval(_I.point(lo), hi=h, ui=ui, g=_G, ci=ci, j=j) - q
        end_hi = _hll_mass_interval(_I.point(hi), hi=h, ui=ui, g=_G, ci=ci, j=j) - q
        gate = root_precision_gate(
            root_exists_bracketed=True,
            strict_endpoint_signs=end_lo.lo > 0 and end_hi.hi < 0,
            bracket_width_m_s=hi - lo,
            target_width_m_s=Fraction.from_float(math.ulp(float((lo + hi) / 2))) / 2,
            steps_used=steps,
            max_bisections=request.max_bisections,
        )
        if not gate["accepted"]:
            raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "root bracket exists but does not meet the frozen half-ULP precision/resource rule")
        proof = "strict_endpoint_signs_and_negative_derivative"
        residuals = ((end_lo.lo, end_lo.hi), (end_hi.lo, end_hi.hi))
    c_mid = float((root.lo + root.hi) / 2)
    target = Fraction.from_float(math.ulp(c_mid)) / 2
    # Equal-state identity uses an outward sqrt enclosure, and must satisfy
    # the same frozen precision rule even though it uses no bisection steps.
    gate = root_precision_gate(
        root_exists_bracketed=True,
        strict_endpoint_signs=residuals is not None,
        equal_state_identity=proof == "equal_state_identity_and_negative_derivative",
        bracket_width_m_s=root.hi - root.lo,
        target_width_m_s=target,
        steps_used=steps,
        max_bisections=request.max_bisections,
    )
    if not gate["accepted"]:
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "root bracket exists but does not meet the frozen half-ULP precision/resource rule")
    return K0RootCertificate(c_g_interval_m_s=(root.lo, root.hi), proof_type=proof,
                             endpoint_residual_intervals_m2_s=residuals,
                             derivative_intervals_dF_dcg_m=derivative,
                             m_F_inverse_lower_m=m_f, M_F_forward_upper_m=M_f,
                             target_half_ulp_m_s=target, steps_used=steps)


def _trace_interval(cg: _I, *, h: Fraction, ui: Fraction, q: Fraction) -> _I:
    ci = _sqrt_interval(_I.point(_G * h))
    j = _I.point(ui) + 2 * ci
    return cg.square() * (j - 2 * cg) / _G - q


def _continuous_trace_diagnostic(request: K0BoundaryRequest, h: Fraction, ui: Fraction) -> dict[str, object]:
    ci = _sqrt_interval(_I.point(_G * h))
    j = _I.point(ui) + 2 * ci
    low, high = _branch_interval(request.direction, j)
    if request.q_target_m2_s == h * ui:
        root = ci
        steps = 0
    else:
        lo, hi = low.hi, high.lo
        f0 = _trace_interval(_I.point(lo), h=h, ui=ui, q=request.q_target_m2_s)
        f1 = _trace_interval(_I.point(hi), h=h, ui=ui, q=request.q_target_m2_s)
        if lo >= hi or f0.lo <= 0 or f1.hi >= 0:
            return {"status": "NO_CERTIFIED_CONTINUOUS_TRACE_ROOT", "q_target_m2_s": str(request.q_target_m2_s)}
        steps = 0
        for _ in range(request.max_bisections):
            mid = (lo + hi) / 2
            fmid = _trace_interval(_I.point(mid), h=h, ui=ui, q=request.q_target_m2_s)
            if fmid.lo > 0:
                lo = mid
            elif fmid.hi < 0:
                hi = mid
            else:
                return {"status": "TRACE_ROOT_NOT_CERTIFIED", "q_target_m2_s": str(request.q_target_m2_s)}
            steps += 1
            if hi - lo <= Fraction.from_float(math.ulp(float((lo + hi) / 2))) / 2:
                break
        root = _I(lo, hi)
        if root.hi - root.lo > Fraction.from_float(math.ulp(float((root.lo + root.hi) / 2))) / 2:
            return {"status": "TRACE_ROOT_NOT_CERTIFIED", "q_target_m2_s": str(request.q_target_m2_s)}
    cg = float((root.lo + root.hi) / 2)
    hi = float(h)
    ui_float = float(ui)
    ci_float = math.sqrt(GRAVITY_M_S2 * hi)
    j_float = ui_float + 2.0 * ci_float
    hg = (cg * cg) / GRAVITY_M_S2
    ug = j_float - 2.0 * cg
    if ((request.direction == "inflow" and not ug < 0.0)
            or (request.direction == "outflow" and not ug > 0.0)):
        raise K0Error("D0_HLL_Q_TRACE_DIRECTION_INVALID", f"continuous trace u_n is zero or opposes request; edge={request.edge.edge_index}, stage={request.stage_index}")
    mass_trace = hg * ug
    nx, ny = request.edge.normal_x, request.edge.normal_y
    tx, ty = -ny, nx
    if request.direction == "inflow":
        ut = float(request.inflow_tangential_velocity_m_s)
    else:
        ut = request.owner_state.u_x_m_s * tx + request.owner_state.u_y_m_s * ty
    trace_ghost = K0State(hg, ug * nx + ut * tx, ug * ny + ut * ty)
    trace_flux = _hll_flux_float(request.owner_state, trace_ghost, nx, ny)
    outgoing = _I.point(ui) + 2 * ci
    return {
        "status": "CERTIFIED_CONTINUOUS_TRACE_ROOT",
        "proof": "direct characteristic discharge equation h_g*u_n,g=q with outward rational interval bisection",
        "outgoing_J_plus_interval_m_s": [str(outgoing.lo), str(outgoing.hi)],
        "c_g_interval_m_s": [str(root.lo), str(root.hi)],
        "h_g_m": hg,
        "u_n_g_m_s": ug,
        "trace_discharge_m2_s": mass_trace,
        "trace_discharge_residual_m2_s": mass_trace - float(request.q_target_m2_s),
        "continuous_trace_HLL_mass_flux_m2_s": trace_flux.mass_m2_s,
        "HLL_flux_is_not_overwritten_by_trace_discharge": True,
        "steps_used": steps,
    }


def _global_ghost_state(request: K0BoundaryRequest, root: K0RootCertificate,
                        h: Fraction, ui: Fraction) -> tuple[K0State, float, float, float]:
    cg = float((root.c_g_interval_m_s[0] + root.c_g_interval_m_s[1]) / 2)
    ci = math.sqrt(GRAVITY_M_S2 * request.owner_state.h_m)
    j = (request.owner_state.u_x_m_s * request.edge.normal_x + request.owner_state.u_y_m_s * request.edge.normal_y) + 2.0 * ci
    un_g = j - 2.0 * cg
    hg = (cg * cg) / GRAVITY_M_S2
    nx, ny = request.edge.normal_x, request.edge.normal_y
    tx, ty = -ny, nx
    if request.direction == "inflow":
        ut_g = float(request.inflow_tangential_velocity_m_s)
    else:
        ut_g = request.owner_state.u_x_m_s * tx + request.owner_state.u_y_m_s * ty
    ux_g = un_g * nx + ut_g * tx
    uy_g = un_g * ny + ut_g * ty
    if not all(math.isfinite(value) for value in (cg, hg, un_g, ut_g, ux_g, uy_g)) or hg <= 0.0:
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "certified root did not construct a finite, fully-wet binary64 ghost")
    ghost = K0State(hg, ux_g, uy_g)
    cghost = math.sqrt(GRAVITY_M_S2 * ghost.h_m)
    actual_un = ghost.u_x_m_s * nx + ghost.u_y_m_s * ny
    if abs(actual_un) >= cghost:
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "rounded ghost state is not strictly subcritical")
    if ((request.direction == "inflow" and not actual_un < 0.0)
            or (request.direction == "outflow" and not actual_un > 0.0)):
        raise K0Error("D0_HLL_Q_GHOST_DIRECTION_INVALID", f"binary64 ghost u_n is zero or opposes request; edge={request.edge.edge_index}, stage={request.stage_index}")
    return ghost, actual_un, ghost.u_x_m_s * tx + ghost.u_y_m_s * ty, cg


def _hll_flux_float(left: K0State, right: K0State, nx: float, ny: float) -> K0HllFlux:
    tx, ty = -ny, nx
    un_l = left.u_x_m_s * nx + left.u_y_m_s * ny
    ut_l = left.u_x_m_s * tx + left.u_y_m_s * ty
    un_r = right.u_x_m_s * nx + right.u_y_m_s * ny
    ut_r = right.u_x_m_s * tx + right.u_y_m_s * ty
    c_l = math.sqrt(GRAVITY_M_S2 * left.h_m)
    c_r = math.sqrt(GRAVITY_M_S2 * right.h_m)
    sl = min(0.0, un_l - c_l, un_r - c_r)
    sr = max(0.0, un_l + c_l, un_r + c_r)
    if not all(math.isfinite(value) for value in (sl, sr)) or sr <= sl:
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "HLL wave-speed denominator is not strictly positive")

    def flux(ul: float, ur: float, fl: float, fr: float) -> float:
        return (sr * fl - sl * fr + sl * sr * (ur - ul)) / (sr - sl)

    mass = flux(left.h_m, right.h_m, left.h_m * un_l, right.h_m * un_r)
    mom_n = flux(left.h_m * un_l, right.h_m * un_r,
                 left.h_m * un_l * un_l + 0.5 * GRAVITY_M_S2 * left.h_m * left.h_m,
                 right.h_m * un_r * un_r + 0.5 * GRAVITY_M_S2 * right.h_m * right.h_m)
    mom_t = flux(left.h_m * ut_l, right.h_m * ut_r, left.h_m * un_l * ut_l, right.h_m * un_r * ut_r)
    mx, my = mom_n * nx + mom_t * tx, mom_n * ny + mom_t * ty
    values = (mass, mom_n, mom_t, mx, my, sl, sr)
    if not all(math.isfinite(value) for value in values):
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "actual binary64 HLL flux is non-finite")
    return K0HllFlux(*values)


def diagnose_synthetic_hll_interface(
    left: K0State,
    right: K0State,
    normal_x: float,
    normal_y: float,
    *,
    direction: Literal["inflow", "outflow"],
) -> K0SyntheticHllDiagnostic:
    """Evaluate a full-vector HLL flux on a labeled synthetic interface only."""
    if not isinstance(left, K0State) or not isinstance(right, K0State):
        raise K0Error("D0_POLICY_INPUT_INVALID", "synthetic interface states must be K0State values")
    nx, _ = _fraction(normal_x, "normal_x")
    ny, _ = _fraction(normal_y, "normal_y")
    if abs(math.hypot(nx, ny) - 1.0) > 1e-12:
        raise K0Error("D0_POLICY_INPUT_INVALID", "synthetic interface normal must be unit length within 1e-12")
    if direction not in ("inflow", "outflow"):
        raise K0Error("D0_POLICY_INPUT_INVALID", "direction must be exactly inflow or outflow")
    flux = _hll_flux_float(left, right, nx, ny)
    matches = flux.mass_m2_s < 0.0 if direction == "inflow" else flux.mass_m2_s > 0.0
    return K0SyntheticHllDiagnostic("SYNTHETIC_INTERFACE", direction, flux, matches)


def _exact_projected_state(state: K0State, nx: float, ny: float) -> tuple[Fraction, Fraction, Fraction]:
    h = Fraction.from_float(state.h_m)
    ux, uy = Fraction.from_float(state.u_x_m_s), Fraction.from_float(state.u_y_m_s)
    nxf, nyf = Fraction.from_float(nx), Fraction.from_float(ny)
    txf, tyf = -nyf, nxf
    return h, ux * nxf + uy * nyf, ux * txf + uy * tyf


def _hll_mass_exact_interval(left: tuple[Fraction, Fraction, Fraction],
                             right: tuple[Fraction | _I, Fraction | _I, Fraction | _I],
                             g: Fraction = _G) -> tuple[_I, Fraction, Fraction]:
    hl, unl, _ = left
    hr, unr = _interval(right[0]), _interval(right[1])
    ci = _sqrt_interval(_I.point(g * hl))
    cr = _sqrt_interval(hr * g)
    sl = _minimum((_I.point(0), _I.point(unl) - ci, unr - cr))
    sr = _maximum((_I.point(0), _I.point(unl) + ci, unr + cr))
    fl = _I.point(hl * unl)
    fr = hr * unr
    mass = (sr * fl - sl * fr + sl * sr * (hr - hl)) / (sr - sl)
    numerator_terms = (sr * fl, -sl * fr, sl * sr * (hr - hl))
    scale = sum(term.abs_upper() for term in numerator_terms)
    denominator_lower = (sr - sl).lo
    if denominator_lower <= 0:
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "exact HLL interval has no positive denominator lower bound")
    return mass, scale, denominator_lower


def _uncertainty_budget(request: K0BoundaryRequest, root: K0RootCertificate,
                        ghost: K0State, flux: K0HllFlux, ui: Fraction) -> K0UncertaintyBudget:
    h = Fraction.from_float(request.owner_state.h_m)
    left = _exact_projected_state(request.owner_state, request.edge.normal_x, request.edge.normal_y)
    actual_right = _exact_projected_state(ghost, request.edge.normal_x, request.edge.normal_y)
    actual_interval, scale, denominator_lower = _hll_mass_exact_interval(left, actual_right)
    reported = Fraction.from_float(flux.mass_m2_s)
    e_eval = max(abs(reported - actual_interval.lo), abs(reported - actual_interval.hi))
    c_bits = float((root.c_g_interval_m_s[0] + root.c_g_interval_m_s[1]) / 2)
    c_exact = Fraction.from_float(c_bits)
    ideal_h = _I.point(c_exact * c_exact) / _G
    ci = _sqrt_interval(_I.point(_G * h))
    j = _I.point(ui) + 2 * ci
    ideal_un = j - 2 * c_exact
    if request.direction == "inflow":
        ut = Fraction.from_float(float(request.inflow_tangential_velocity_m_s))
    else:
        ut = left[2]
    ideal_h_state = ideal_h
    # Root certification parameterizes the ghost by its normal speed directly.
    # Comparing against that exact state makes E_ghost include rotation and
    # binary64 global-component reconstruction error as well as h/u rounding.
    ideal_interval, _, _ = _hll_mass_exact_interval(left, (ideal_h_state, ideal_un, _I.point(ut)))
    e_ghost = max(abs(actual_interval.lo - ideal_interval.hi), abs(actual_interval.hi - ideal_interval.lo))
    c_error = max(abs(c_exact - root.c_g_interval_m_s[0]), abs(c_exact - root.c_g_interval_m_s[1]))
    e_root = root.M_F_forward_upper_m * c_error
    q = request.q_target_m2_s
    residual = abs(reported - q)
    tau = e_root + e_ghost + e_eval
    edge_length = Fraction.from_float(request.edge.length_m)
    requested_group = request.group_discharge_m3_s if request.direction == "outflow" else -request.group_discharge_m3_s
    actual_group = edge_length * reported
    group_residual = abs(actual_group - requested_group)
    group_tau = edge_length * tau
    # Standard first-order gamma bound for the bounded operation path. Direct
    # interval subtraction above is the operative E_eval; this is retained as
    # an independent scale sanity bound.
    eps = Fraction.from_float(math.ulp(1.0)) / 2
    gamma32 = (32 * eps) / (1 - 32 * eps)
    gamma_sanity = gamma32 * scale / denominator_lower
    if e_eval > gamma_sanity:
        raise K0Error("D0_HLL_Q_ROOT_NOT_CERTIFIED", "independent HLL arithmetic enclosure exceeds its gamma-32 scale bound")
    if residual > tau:
        raise K0Error("BOUNDARY_HLL_Q_RESIDUAL_EXCEEDED", "actual HLL mass flux is outside the independently accumulated Q error budget")
    if group_residual > group_tau:
        raise K0Error("BOUNDARY_HLL_Q_RESIDUAL_EXCEEDED", "single-edge total-Q residual exceeds the length-weighted group error budget")
    return K0UncertaintyBudget(residual, Fraction(0), e_root, e_ghost, e_eval,
                               Fraction(0), tau, group_residual, group_tau, gamma_sanity)


def diagnose_single_edge(request: K0BoundaryRequest) -> K0BoundaryDiagnostic:
    """Certify one discharge ghost, compute its actual vector HLL flux, and return diagnostics only."""
    if not isinstance(request, K0BoundaryRequest):
        raise K0Error("D0_POLICY_INPUT_INVALID", "request must be prepared by prepare_p2_edge_request")
    stage = _strict_int(request.stage_index, "stage_index", 0, 1)
    cap = _strict_int(request.max_bisections, "max_bisections", 1, _MAX_BISECTIONS)
    if type(request.q_target_m2_s) is not Fraction:
        raise K0Error("D0_POLICY_INPUT_INVALID", "q_target_m2_s must be an exact Fraction derived by the P2 adapter")
    if type(request.group_discharge_m3_s) is not Fraction:
        raise K0Error("D0_POLICY_INPUT_INVALID", "group_discharge_m3_s must retain the exact binary64 Q input")
    if Fraction.from_float(float(request.binding.values[0])) != request.group_discharge_m3_s:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "exact total Q differs from the actual binding value")
    edge = _validate_physical_edge(request.package, request.binding, request.edge.edge_index)
    if edge != request.edge:
        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "prepared edge identity differs from current mesh geometry")
    if (request.binding.mode != "constant" or len(request.binding.edge_ids) != 1
            or getattr(request.binding.quantity, "value", request.binding.quantity) != BoundaryQuantity.DISCHARGE.value):
        raise K0Error("D0_K0_SINGLE_EDGE_GROUP_REQUIRED", "K0 request must retain its one-edge constant-discharge source binding")
    h_float, h, ux_float, ux, uy_float, uy = _validate_owner_state(
        request.package, edge, request.owner_state, request.stage_index
    )
    nx, ny = edge.normal_x, edge.normal_y
    ui_float = ux_float * nx + uy_float * ny
    ui = ux * Fraction.from_float(nx) + uy * Fraction.from_float(ny)
    _validate_owner_direction(ui, request.direction, edge, stage, ui_float)
    if request.direction == "inflow" and request.inflow_tangential_velocity_m_s is None:
        raise K0Error("D0_POLICY_INPUT_INVALID", "inflow tangential velocity must be explicit")
    if request.direction == "inflow":
        tangent, _ = _fraction(request.inflow_tangential_velocity_m_s, "inflow_tangential_velocity_m_s")
        if tangent != 0.0:
            raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "this K0 closure batch requires explicit zero inflow tangential velocity")
    if request.direction == "outflow" and request.inflow_tangential_velocity_m_s is not None:
        raise K0Error("D0_POLICY_INPUT_INVALID", "outflow tangent must come from the owner state")
    if request.direction not in ("inflow", "outflow") or request.q_target_m2_s == 0:
        raise K0Error("D0_HLL_Q_INPUT_DIRECTION_INVALID", "Q direction must be fixed and nonzero")
    if (request.direction == "inflow") != (request.q_target_m2_s < 0):
        raise K0Error("D0_HLL_Q_INPUT_DIRECTION_INVALID", "requested q sign does not match inflow/outflow direction")
    if abs(request.q_target_m2_s) > Fraction(6, 5):
        raise K0Error("D0_HLL_Q_ROOT_DOMAIN_UNCERTIFIED", "|q_n| must be at most 1.2 m2/s")
    expected_q = request.group_discharge_m3_s / Fraction.from_float(edge.length_m)
    if request.direction == "inflow":
        expected_q = -expected_q
    if expected_q != request.q_target_m2_s or request.max_bisections != cap or request.stage_index != stage:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "prepared q/stage/resource fields differ from their validated P2 source values")
    root = _root_certificate(request, h=h, ui=ui)
    ghost, un_g, ut_g, c_float = _global_ghost_state(request, root, h, ui)
    flux = _hll_flux_float(request.owner_state, ghost, nx, ny)
    if request.direction == "inflow" and not flux.mass_m2_s < 0.0:
        raise K0Error("BOUNDARY_ACTUAL_HLL_DIRECTION_MISMATCH", "actual outward HLL mass flux is not strictly negative for inflow")
    if request.direction == "outflow" and not flux.mass_m2_s > 0.0:
        raise K0Error("BOUNDARY_ACTUAL_HLL_DIRECTION_MISMATCH", "actual outward HLL mass flux is not strictly positive for outflow")
    budget = _uncertainty_budget(request, root, ghost, flux, ui)
    fingerprint = _request_input_fingerprint(request, edge)
    calculated = {
        "input_fingerprint": fingerprint,
        "root_interval": [str(root.c_g_interval_m_s[0]), str(root.c_g_interval_m_s[1])],
        "c_g_binary64_bits": _float_bits(c_float),
        "ghost_bits": [_float_bits(ghost.h_m), _float_bits(ghost.u_x_m_s), _float_bits(ghost.u_y_m_s)],
        "hll_bits": [_float_bits(value) for value in (flux.mass_m2_s, flux.momentum_normal_m3_s2,
                                                       flux.momentum_tangential_m3_s2,
                                                       flux.momentum_x_m3_s2, flux.momentum_y_m3_s2)],
    }
    calculation_fingerprint = _fp(calculated)
    trace = _continuous_trace_diagnostic(request, h, ui)
    ci = _sqrt_interval(_I.point(_G * h))
    outgoing = _I.point(ui) + 2 * ci
    return K0BoundaryDiagnostic(
        edge=edge,
        owner_state=request.owner_state,
        owner_normal_velocity_m_s=ui,
        owner_discharge_m2_s=h * ui,
        outgoing_characteristic_interval_m_s=(outgoing.lo, outgoing.hi),
        ghost_state=ghost,
        ghost_state_id=f"k0:{edge.mesh_id}:{edge.edge_index}:{request.stage_index}:{fingerprint[:16]}",
        direction=request.direction,
        q_requested_m2_s=request.q_target_m2_s,
        group_discharge_requested_signed_m3_s=(request.group_discharge_m3_s if request.direction == "outflow" else -request.group_discharge_m3_s),
        group_discharge_actual_signed_m3_s=(Fraction.from_float(edge.length_m) * Fraction.from_float(flux.mass_m2_s)),
        root=root,
        continuous_trace=trace,
        hll_flux=flux,
        uncertainty=budget,
        input_fingerprint=fingerprint,
        calculation_fingerprint=calculation_fingerprint,
        root_consumer_fingerprint=calculation_fingerprint,
        uncertainty_consumer_fingerprint=calculation_fingerprint,
        flux_consumer_fingerprint=calculation_fingerprint,
        stage_index=request.stage_index,
    )


def _state_bits(state: K0State) -> tuple[str, str, str]:
    return _float_bits(state.h_m), _float_bits(state.u_x_m_s), _float_bits(state.u_y_m_s)


def _diagnostic_calculation_fingerprint(diagnostic: K0BoundaryDiagnostic) -> str:
    root_lo, root_hi = diagnostic.root.c_g_interval_m_s
    values = (diagnostic.hll_flux.mass_m2_s, diagnostic.hll_flux.momentum_normal_m3_s2,
              diagnostic.hll_flux.momentum_tangential_m3_s2, diagnostic.hll_flux.momentum_x_m3_s2,
              diagnostic.hll_flux.momentum_y_m3_s2)
    payload = {
        "input_fingerprint": diagnostic.input_fingerprint,
        "root_interval": [str(root_lo), str(root_hi)],
        "c_g_binary64_bits": _float_bits(float((root_lo + root_hi) / 2)),
        "ghost_bits": list(_state_bits(diagnostic.ghost_state)),
        "hll_bits": [_float_bits(value) for value in values],
    }
    return _fp(payload)


def _request_input_fingerprint(request: K0BoundaryRequest, edge: K0PhysicalEdge) -> str:
    """Recompute a canonical identity from the normalized live package request."""
    package, binding = request.package, request.binding
    face = edge.owner_face_index
    terrain = package.terrain
    initial = package.initial_condition
    binding_quantity = getattr(binding.quantity, "value", binding.quantity)
    initial_quantity = getattr(initial.quantity, "value", initial.quantity)
    payload = {
        "package": {
            "mesh_id": package.mesh.mesh_id,
            "topology_fingerprint": topology_fingerprint(package.mesh),
            "security_exclusion_boundary_edge_ids": list(package.security_exclusion_boundary_edge_ids),
            "preparation_only": package.preparation_only,
        },
        "edge": {
            "edge_index": edge.edge_index,
            "node_pair": list(edge.node_pair),
            "owner_face_index": edge.owner_face_index,
            "owner_face_id": edge.owner_face_id,
            "boundary_group": edge.boundary_group,
            "normal_bits": [_float_bits(edge.normal_x), _float_bits(edge.normal_y)],
            "length_bits": _float_bits(edge.length_m),
        },
        "binding": {
            "mesh_id": binding.mesh_id,
            "topology_fingerprint": binding.topology_fingerprint,
            "group_name": binding.group_name,
            "quantity": binding_quantity,
            "mode": binding.mode,
            "edge_ids": [int(value) for value in binding.edge_ids],
            "edge_nodes": [[int(node) for node in pair] for pair in binding.edge_nodes],
            "value_bits": [_float_bits(float(value)) for value in binding.values],
            "time_bits": None if binding.time_s is None else [_float_bits(float(value)) for value in binding.time_s],
            "unit": binding.unit,
            "interpretation": binding.interpretation,
            "source": binding.source,
            "vertical_datum": binding.vertical_datum,
        },
        "terrain_owner_sample": {
            "mesh_id": terrain.mesh_id,
            "coordinate_system": terrain.coordinate_system,
            "elevation_bits": None if terrain.elevation_m[face] is None else _float_bits(float(terrain.elevation_m[face])),
            "valid_mask": terrain.valid_mask[face],
            "covered_area_bits": _float_bits(float(terrain.covered_area_m2[face])),
            "valid_area_bits": _float_bits(float(terrain.valid_area_m2[face])),
            "coverage_ratio_bits": _float_bits(float(terrain.coverage_ratio[face])),
            "vertical_datum": terrain.vertical_datum,
            "vertical_unit": terrain.vertical_unit,
            "vertical_unit_source": terrain.vertical_unit_source,
            "vertical_datum_source": terrain.vertical_datum_source,
            "source_vertical_unit": terrain.source_vertical_unit,
            "source_vertical_datum": terrain.source_vertical_datum,
        },
        "initial_owner_sample": {
            "mesh_id": initial.mesh_id,
            "topology_fingerprint": initial.topology_fingerprint,
            "quantity": initial_quantity,
            "value_bits": None if initial.values_m[face] is None else _float_bits(float(initial.values_m[face])),
            "valid_mask": initial.valid_mask[face],
            "source": initial.source,
            "assignment_mode": initial.assignment_mode,
            "vertical_datum": initial.vertical_datum,
            "vertical_datum_source": initial.vertical_datum_source,
            "source_terrain_vertical_datum": initial.source_terrain_vertical_datum,
            "unit": initial.unit,
        },
        "owner_state_bits": [_float_bits(request.owner_state.h_m),
                             _float_bits(request.owner_state.u_x_m_s),
                             _float_bits(request.owner_state.u_y_m_s)],
        "direction": request.direction,
        "q_target_exact": f"{request.q_target_m2_s.numerator}/{request.q_target_m2_s.denominator}",
        "group_Q_exact": f"{request.group_discharge_m3_s.numerator}/{request.group_discharge_m3_s.denominator}",
        "inflow_tangential_velocity_bits": None if request.inflow_tangential_velocity_m_s is None
        else _float_bits(float(request.inflow_tangential_velocity_m_s)),
        "gravity_bits": _float_bits(GRAVITY_M_S2),
        "stage_index": request.stage_index,
        "max_bisections": request.max_bisections,
    }
    return _fp(payload)


def _validate_current_boundary_request(
    request: K0BoundaryRequest,
    package: TriangularBusinessPackage,
    binding: TriangularBoundaryBinding,
    edge: K0PhysicalEdge,
    stage_index: int,
    owner: K0State,
) -> str:
    """Bind a CFL consumer to the current package and the request that made its ghost."""
    if not isinstance(request, K0BoundaryRequest) or request.package is not package:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "CFL boundary request was not prepared from the current package object")
    if type(request.q_target_m2_s) is not Fraction or type(request.group_discharge_m3_s) is not Fraction:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "CFL request q and total Q must retain exact Fraction inputs")
    if request.binding is not binding:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "CFL boundary request was not prepared from the current binding object")
    if request.edge != edge or _strict_int(request.stage_index, "stage_index", 0, 1) != stage_index:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "CFL request edge/stage identity differs from current input")
    cap = _strict_int(request.max_bisections, "max_bisections", 1, _MAX_BISECTIONS)
    if _state_bits(request.owner_state) != _state_bits(owner):
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "CFL request owner bits differ from the current stage owner")
    _, _, ux, _, uy, _ = _validate_owner_state(package, edge, owner, stage_index)
    un_binary64 = ux * edge.normal_x + uy * edge.normal_y
    ui = ux * Fraction.from_float(edge.normal_x) + uy * Fraction.from_float(edge.normal_y)
    _validate_owner_direction(ui, request.direction, edge, stage_index, un_binary64)
    if request.direction not in ("inflow", "outflow"):
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "CFL request direction is invalid")
    if request.direction == "inflow":
        tangent, _ = _fraction(request.inflow_tangential_velocity_m_s, "inflow_tangential_velocity_m_s")
        if tangent != 0.0:
            raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "CFL request has unsupported nonzero inflow tangent")
    elif request.inflow_tangential_velocity_m_s is not None:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "outflow request contains an inflow tangent override")
    if binding.mode != "constant" or len(binding.values) != 1 or len(binding.edge_ids) != 1:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "current binding is outside the one-edge constant-discharge K0 scope")
    if getattr(binding.quantity, "value", binding.quantity) != BoundaryQuantity.DISCHARGE.value:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "current binding is not a discharge")
    Q = Fraction.from_float(float(binding.values[0]))
    if Q <= 0 or request.group_discharge_m3_s != Q:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "current total Q differs from the certified request")
    expected_q = Q / Fraction.from_float(edge.length_m)
    if request.direction == "inflow":
        expected_q = -expected_q
    if abs(expected_q) > Fraction(6, 5) or request.q_target_m2_s != expected_q:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "current Q/edge length/direction differs from certified q")
    if binding.edge_ids != (edge.edge_index,):
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "current binding edge set differs from the certified single edge")
    if not any(candidate is binding for candidate in package.boundary_bindings):
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "binding object is not the current package's registered binding")
    if cap != request.max_bisections:
        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "resource cap changed after request construction")
    return _request_input_fingerprint(request, edge)


def make_closed_wall_ghost(edge: K0PhysicalEdge, owner: K0State) -> tuple[K0State, str]:
    """Construct the mirrored velocity used only for an unbound exterior wall."""
    if not isinstance(edge, K0PhysicalEdge) or not isinstance(owner, K0State):
        raise K0Error("D0_POLICY_INPUT_INVALID", "wall construction requires a K0PhysicalEdge and K0State")
    nx, ny = edge.normal_x, edge.normal_y
    tx, ty = -ny, nx
    un = owner.u_x_m_s * nx + owner.u_y_m_s * ny
    ut = owner.u_x_m_s * tx + owner.u_y_m_s * ty
    ghost = K0State(owner.h_m, -un * nx + ut * tx, -un * ny + ut * ty)
    return ghost, f"closed-wall:{edge.mesh_id}:{edge.edge_index}"


def _alpha_upper(left: K0State, right: K0State, nx: float, ny: float) -> Fraction:
    nxf, nyf = Fraction.from_float(nx), Fraction.from_float(ny)
    un_l = Fraction.from_float(left.u_x_m_s) * nxf + Fraction.from_float(left.u_y_m_s) * nyf
    un_r = Fraction.from_float(right.u_x_m_s) * nxf + Fraction.from_float(right.u_y_m_s) * nyf
    c_l = _sqrt_interval(_I.point(_G * Fraction.from_float(left.h_m)))
    c_r = _sqrt_interval(_I.point(_G * Fraction.from_float(right.h_m)))
    alpha_l = abs(un_l) + c_l.hi
    alpha_r = abs(un_r) + c_r.hi
    return max(alpha_l, alpha_r)


def _wall_state_matches(edge: K0PhysicalEdge, owner: K0State, ghost: K0State) -> bool:
    expected, _ = make_closed_wall_ghost(edge, owner)
    return _state_bits(expected) == _state_bits(ghost)


def _validate_stage_domain(package: TriangularBusinessPackage, stage: K0StageDomainInput,
                           diagnostics: Sequence[K0BoundaryDiagnostic]) -> tuple[
                               dict[int, Fraction], dict[tuple[int, int], Fraction], dict[int, str], dict[int, str]
                           ]:
    if not isinstance(package, TriangularBusinessPackage) or not isinstance(stage, K0StageDomainInput):
        raise K0Error("D0_POLICY_INPUT_INVALID", "CFL validation requires a business package and K0StageDomainInput")
    if not isinstance(stage.cells, (tuple, list)):
        raise K0Error("D0_POLICY_INPUT_INVALID", "stage cells must be a sequence")
    if not isinstance(diagnostics, (tuple, list)):
        raise K0Error("D0_POLICY_INPUT_INVALID", "stage boundary diagnostics must be a sequence")
    mesh = package.mesh
    stage_index = _strict_int(stage.stage_index, "stage_index", 0, 1)
    cell_map: dict[int, K0CellStageInput] = {}
    for cell in stage.cells:
        if not isinstance(cell, K0CellStageInput) or not isinstance(cell.owner_state, K0State) or not isinstance(cell.edges, (tuple, list)):
            raise K0Error("D0_POLICY_INPUT_INVALID", "each stage cell needs a K0State and an edge sequence")
        if type(cell.face_id) is not int or cell.face_id not in mesh.face_id:
            raise K0Error("D0_POLICY_INPUT_INVALID", "stage cell face_id must be an actual stable mesh face ID")
        if cell.face_id in cell_map:
            raise K0Error("D0_POLICY_INPUT_INVALID", "stage domain contains a duplicate face_id")
        cell_map[cell.face_id] = cell
    expected_face_ids = set(int(face_id) for face_id in mesh.face_id)
    if set(cell_map) != expected_face_ids:
        raise K0Error("D0_POLICY_INPUT_INVALID", "each stage must supply exactly one state for every mesh face")
    diagnostic_map: dict[int, K0BoundaryDiagnostic] = {}
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, K0BoundaryDiagnostic):
            raise K0Error("D0_POLICY_INPUT_INVALID", "boundary diagnostics must be K0BoundaryDiagnostic values")
        if _strict_int(diagnostic.stage_index, "stage_index", 0, 1) != stage_index:
            raise K0Error("D0_POLICY_INPUT_INVALID", "boundary diagnostic stage_index does not match its CFL stage")
        edge_index = diagnostic.edge.edge_index
        if edge_index in diagnostic_map:
            raise K0Error("D0_POLICY_INPUT_INVALID", "duplicate boundary diagnostic for one edge and stage")
        diagnostic_map[edge_index] = diagnostic
    binding_by_edge: dict[int, TriangularBoundaryBinding] = {}
    for binding in package.boundary_bindings:
        for edge_id in binding.edge_ids:
            binding_by_edge[int(edge_id)] = binding
    consumed_diagnostics: set[int] = set()
    limits: dict[int, Fraction] = {}
    edge_alphas: dict[tuple[int, int], Fraction] = {}
    fingerprints: dict[int, str] = {}
    calculation_fingerprints: dict[int, str] = {}
    for face_index, face_id_raw in enumerate(mesh.face_id):
        face_id = int(face_id_raw)
        cell = cell_map[face_id]
        edges_by_id: dict[int, K0CflEdgeInput] = {}
        for item in cell.edges:
            if not isinstance(item, K0CflEdgeInput) or not isinstance(item.right_state, K0State):
                raise K0Error("D0_POLICY_INPUT_INVALID", "each edge needs a K0CflEdgeInput with a K0State right state")
            edge_id = _strict_int(item.edge_index, "edge_index", 0, mesh.n_edge - 1)
            if edge_id in edges_by_id:
                raise K0Error("D0_POLICY_INPUT_INVALID", "triangle stage repeats an edge identity")
            edges_by_id[edge_id] = item
        expected_edge_ids = set(int(value) for value in mesh.face_edges[face_index])
        if set(edges_by_id) != expected_edge_ids or len(edges_by_id) != 3:
            raise K0Error("D0_POLICY_INPUT_INVALID", "every triangle stage must provide exactly its three actual mesh edges")
        area = Fraction.from_float(float(mesh.face_area[face_index]))
        denominator = Fraction(0)
        for edge_id in mesh.face_edges[face_index]:
            edge_id = int(edge_id)
            item = edges_by_id[edge_id]
            left_state, right = cell.owner_state, item.right_state
            neighbor_local = next((face for face in mesh.edge_faces[edge_id] if face != face_index), -1)
            if neighbor_local >= 0:
                neighbor_id = int(mesh.face_id[neighbor_local])
                if item.boundary_request is not None:
                    raise K0Error("D0_POLICY_INPUT_INVALID", "internal edges cannot carry a boundary request")
                if type(item.neighbor_face_id) is not int:
                    raise K0Error("D0_POLICY_INPUT_INVALID", "internal neighbor_face_id must be a strict int")
                if item.neighbor_face_id != neighbor_id or item.ghost_state_id is not None:
                    raise K0Error("D0_POLICY_INPUT_INVALID", "internal edge requires its actual neighbor face_id and no ghost ID")
                if _state_bits(right) != _state_bits(cell_map[neighbor_id].owner_state):
                    raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "internal right state bits must equal the actual neighbor's stage owner state")
            else:
                if item.neighbor_face_id is not None or not isinstance(item.ghost_state_id, str) or not item.ghost_state_id:
                    raise K0Error("D0_POLICY_INPUT_INVALID", "exterior edge requires an explicit ghost_state_id and no neighbor face_id")
                binding = binding_by_edge.get(edge_id)
                if binding is not None:
                    if edge_id in set(package.security_exclusion_boundary_edge_ids):
                        raise K0Error("D0_SECURITY_EXCLUSION_EDGE_FORBIDDEN", "security-exclusion physical edge IDs remain closed walls for every alias")
                    diagnostic = diagnostic_map.get(edge_id)
                    if diagnostic is None:
                        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "each bound open edge needs its own certified K0 ghost for this stage")
                    expected_edge = _validate_physical_edge(package, binding, edge_id)
                    if expected_edge != diagnostic.edge:
                        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "CFL boundary diagnostic does not match the current physical P2 edge")
                    if item.boundary_request is None:
                        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "bound CFL edge requires the original typed request that produced its ghost")
                    request_fingerprint = _validate_current_boundary_request(
                        item.boundary_request, package, binding, expected_edge, stage_index, left_state
                    )
                    if getattr(binding.quantity, "value", binding.quantity) != BoundaryQuantity.DISCHARGE.value:
                        raise K0Error("D0_POLICY_INPUT_INVALID", "CFL stage contains a bound non-discharge edge outside K0 scope")
                    if diagnostic.edge.edge_index != edge_id or diagnostic.edge.owner_face_index != face_index:
                        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "boundary ghost identity does not match the actual owner edge")
                    if diagnostic.edge.mesh_id != mesh.mesh_id or diagnostic.edge.topology_fingerprint != topology_fingerprint(mesh):
                        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "boundary ghost belongs to a different P2 mesh/topology")
                    if diagnostic.calculation_fingerprint != diagnostic.root_consumer_fingerprint or diagnostic.calculation_fingerprint != diagnostic.uncertainty_consumer_fingerprint or diagnostic.calculation_fingerprint != diagnostic.flux_consumer_fingerprint:
                        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "root, uncertainty, and flux calculation fingerprints differ")
                    if diagnostic.input_fingerprint != request_fingerprint:
                        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "cached diagnostic input identity differs from the current package/binding/stage request")
                    if _diagnostic_calculation_fingerprint(diagnostic) != diagnostic.calculation_fingerprint:
                        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "root/ghost/actual-flux payload differs from its calculation fingerprint")
                    if (diagnostic.stage_index != stage_index or diagnostic.direction != item.boundary_request.direction
                            or diagnostic.q_requested_m2_s != item.boundary_request.q_target_m2_s):
                        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "cached diagnostic direction/q/stage differs from the current request")
                    if _state_bits(diagnostic.owner_state) != _state_bits(left_state) or _state_bits(diagnostic.ghost_state) != _state_bits(right):
                        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "CFL edge left/right state bits differ from the certified root inputs/output")
                    if item.ghost_state_id != diagnostic.ghost_state_id:
                        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "CFL ghost_state_id differs from the certified boundary ghost")
                    consumed_diagnostics.add(edge_id)
                    fingerprints[edge_id] = diagnostic.input_fingerprint
                    calculation_fingerprints[edge_id] = diagnostic.calculation_fingerprint
                else:
                    if item.boundary_request is not None:
                        raise K0Error("D0_POLICY_INPUT_STATE_MISMATCH", "unbound exterior wall edge cannot carry a discharge request")
                    wall, wall_id = make_closed_wall_ghost(
                        K0PhysicalEdge(mesh.mesh_id or "", topology_fingerprint(mesh), edge_id,
                                       tuple(mesh.edge_nodes[edge_id]), face_index, face_id,
                                       "closed_wall", *_edge_normal(mesh, edge_id, face_index),
                                       float(mesh.edge_length[edge_id])), left_state
                    )
                    if item.ghost_state_id != wall_id or not _wall_state_matches(
                            K0PhysicalEdge(mesh.mesh_id or "", topology_fingerprint(mesh), edge_id,
                                           tuple(mesh.edge_nodes[edge_id]), face_index, face_id,
                                           "closed_wall", *_edge_normal(mesh, edge_id, face_index),
                                           float(mesh.edge_length[edge_id])), left_state, right):
                        raise K0Error("D0_P2_EDGE_IDENTITY_INVALID", "unbound exterior edges, including security edges, must remain closed-wall ghosts")
            alpha = _alpha_upper(left_state, right, *_edge_normal(mesh, edge_id, face_index))
            edge_alphas[(face_id, edge_id)] = alpha
            denominator += Fraction.from_float(float(mesh.edge_length[edge_id])) * alpha
        if denominator <= 0:
            raise K0Error("D0_TRIANGLE_CFL_INVALID", "triangle has a non-positive per-edge wave-speed denominator")
        limits[face_id] = Fraction(1, 4) * area / denominator
    if consumed_diagnostics != set(diagnostic_map):
        raise K0Error("D0_POLICY_INPUT_INVALID", "a supplied boundary diagnostic was not consumed by an actual bound P2 edge")
    return limits, edge_alphas, fingerprints, calculation_fingerprints


def check_two_stage_domain_cfl(
    package: TriangularBusinessPackage,
    stage0: K0StageDomainInput,
    stage1: K0StageDomainInput,
    dt_s: float,
    *,
    cfl_number: float = 0.25,
    remaining_to_breakpoint_s: float | None = None,
    stage0_boundary_diagnostics: Sequence[K0BoundaryDiagnostic] = (),
    stage1_boundary_diagnostics: Sequence[K0BoundaryDiagnostic] = (),
) -> K0CflResult:
    """Check both supplied stage domains against per-edge CFL; never update cells."""
    if not isinstance(package, TriangularBusinessPackage):
        raise K0Error("D0_POLICY_INPUT_INVALID", "package must be a validated TriangularBusinessPackage")
    if not isinstance(stage0, K0StageDomainInput) or not isinstance(stage1, K0StageDomainInput):
        raise K0Error("D0_POLICY_INPUT_INVALID", "two-stage CFL inputs must be K0StageDomainInput values")
    if _strict_int(stage0.stage_index, "stage_index", 0, 1) != 0 or _strict_int(stage1.stage_index, "stage_index", 0, 1) != 1:
        raise K0Error("D0_POLICY_INPUT_INVALID", "two-stage CFL inputs must have strict stage indices 0 and 1")
    dt, dt_fraction = _fraction(dt_s, "dt_s", positive=True)
    if remaining_to_breakpoint_s is not None:
        _, remaining = _fraction(remaining_to_breakpoint_s, "remaining_to_breakpoint_s", positive=True)
        if dt_fraction > remaining:
            raise K0Error("D0_BREAKPOINT_CROSSED", "dt_s exceeds the supplied remaining time to the next breakpoint")
    cfl, cfl_fraction = _fraction(cfl_number, "cfl_number", positive=True)
    if cfl > 1.0:
        raise K0Error("D0_POLICY_INPUT_INVALID", "cfl_number must be in (0, 1]")
    if cfl_fraction != Fraction(1, 4):
        raise K0Error("D0_POLICY_INPUT_INVALID", "this K0 release freezes the CFL multiplier at 0.25")
    limits0, alpha0, fps0, calc0 = _validate_stage_domain(package, stage0, stage0_boundary_diagnostics)
    limits1, alpha1, fps1, calc1 = _validate_stage_domain(package, stage1, stage1_boundary_diagnostics)
    min0_id, min0 = min(limits0.items(), key=lambda item: item[1])
    min1_id, min1 = min(limits1.items(), key=lambda item: item[1])
    limiting_stage = 0 if min0 <= min1 else 1
    limiting_face = min0_id if limiting_stage == 0 else min1_id
    safe = min(min0, min1)
    fingerprints = tuple(fps0[key] for key in sorted(fps0)) + tuple(fps1[key] for key in sorted(fps1))
    calculation_fingerprints = tuple(calc0[key] for key in sorted(calc0)) + tuple(calc1[key] for key in sorted(calc1))
    return K0CflResult(
        accepted=dt_fraction <= safe,
        dt_s=dt_fraction,
        safe_dt_limit_s=safe,
        stage_limits_s=(min0, min1),
        limiting_stage_index=limiting_stage,
        limiting_face_id=limiting_face,
        per_stage_cell_limits_s=tuple((face_id, limits0[face_id]) for face_id in sorted(limits0))
        + tuple((face_id, limits1[face_id]) for face_id in sorted(limits1)),
        per_edge_alpha_upper_m_s=tuple((0, face_id, edge_id, alpha0[(face_id, edge_id)])
                                       for face_id, edge_id in sorted(alpha0))
        + tuple((1, face_id, edge_id, alpha1[(face_id, edge_id)]) for face_id, edge_id in sorted(alpha1)),
        cfl_number=cfl_fraction,
        input_fingerprints=fingerprints,
        calculation_fingerprints=calculation_fingerprints,
    )


__all__ = [
    "GRAVITY_M_S2", "K0BoundaryDiagnostic", "K0BoundaryRequest", "K0CflEdgeInput",
    "K0CflResult", "K0CellStageInput", "K0Error", "K0HllFlux", "K0PhysicalEdge",
    "K0RootCertificate", "K0StageDomainInput", "K0State", "K0UncertaintyBudget",
    "K0SyntheticHllDiagnostic", "check_two_stage_domain_cfl", "diagnose_single_edge",
    "diagnose_synthetic_hll_interface", "make_closed_wall_ghost", "resolve_p2_closed_wall_edge",
    "prepare_p2_edge_request", "root_precision_gate",
]
