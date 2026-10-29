"""Strict, versioned Gmsh 2-D algorithm plans for mesh-only experiments."""
from __future__ import annotations

import hashlib
import json
from typing import Any


MESH_ALGORITHM_PLAN_FORMAT = "triangle-mesh-algorithm-plan-v1"
MESH_ALGORITHM_PLAN_VERSION = "1.0"
MESH_ALGORITHM_PLAN_FIELDS = {
    "format",
    "schema_version",
    "algorithm_2d",
    "smoothing",
    "algorithm_switch_on_failure",
    "optimizer",
    "iterations",
    "force",
    "entity_scope",
}


def validate_mesh_algorithm_plan_shape(value: Any) -> dict[str, Any]:
    """Validate a complete plan without supplying any defaults."""
    if not isinstance(value, dict):
        raise ValueError("mesh_algorithm_plan must be an object")
    if set(value) != MESH_ALGORITHM_PLAN_FIELDS:
        raise ValueError("mesh_algorithm_plan fields differ from the v1 contract")
    if value["format"] != MESH_ALGORITHM_PLAN_FORMAT:
        raise ValueError("mesh_algorithm_plan.format is unsupported")
    if value["schema_version"] != MESH_ALGORITHM_PLAN_VERSION:
        raise ValueError("mesh_algorithm_plan.schema_version must be '1.0'")

    algorithm = value["algorithm_2d"]
    if type(algorithm) is not int or algorithm not in (5, 6):
        raise ValueError("mesh_algorithm_plan.algorithm_2d must be strict integer 5 or 6 (unit 1)")
    smoothing = value["smoothing"]
    if type(smoothing) is not int or smoothing != 1:
        raise ValueError("mesh_algorithm_plan.smoothing must be strict integer 1")
    switch = value["algorithm_switch_on_failure"]
    if type(switch) is not int or switch != 0:
        raise ValueError("mesh_algorithm_plan.algorithm_switch_on_failure must be strict integer 0")

    optimizer = value["optimizer"]
    if optimizer not in ("none", "Relocate2D") or not isinstance(optimizer, str):
        raise ValueError("mesh_algorithm_plan.optimizer must be 'none' or 'Relocate2D'")
    iterations = value["iterations"]
    if type(iterations) is not int or iterations not in (0, 1):
        raise ValueError("mesh_algorithm_plan.iterations must be strict integer 0 or 1")
    expected_iterations = 0 if optimizer == "none" else 1
    if iterations != expected_iterations:
        raise ValueError("mesh_algorithm_plan.iterations must be 0 for none and 1 for Relocate2D")
    if type(value["force"]) is not bool or value["force"] is not False:
        raise ValueError("mesh_algorithm_plan.force is fixed to false")
    if value["entity_scope"] != "active_surface" or not isinstance(value["entity_scope"], str):
        raise ValueError("mesh_algorithm_plan.entity_scope is fixed to 'active_surface'")

    return {key: value[key] for key in sorted(MESH_ALGORITHM_PLAN_FIELDS)}


def mesh_algorithm_plan_sha256(value: Any) -> str:
    plan = validate_mesh_algorithm_plan_shape(value)
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256((payload + "\n").encode("utf-8")).hexdigest()
