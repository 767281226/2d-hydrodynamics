"""Source-backed CRS comparison for the preparation-only terrain route.

The raw DEM WKT is kept in the resulting provenance.  This module proves a
limited coordinate alias; it never transforms coordinates or changes raster
values/metadata in the source file.
"""
from __future__ import annotations

from hashlib import sha256
import math
from typing import Any


class PreparationCRSError(ValueError):
    """A source CRS cannot be proven equivalent for preparation-only XY use."""


def _number_equal(left: float, right: float, *, absolute: float = 1e-9) -> bool:
    return math.isfinite(float(left)) and math.isfinite(float(right)) and math.isclose(
        float(left), float(right), rel_tol=0.0, abs_tol=absolute
    )


def prove_epsg4548_xy_alias(source_wkt: str, *, expected_wkt_sha256: str | None = None) -> dict[str, Any]:
    """Return auditable evidence for an unchanged Easting/Northing alias.

    EPSG:4548's registry axis order is Northing/Easting, while the project
    mesh and raster affine use GIS XY (Easting, Northing).  The accepted alias
    is explicitly that XY convention.  A non-zero bound transformation,
    changed projection parameter, changed unit/axis set, datum or ellipsoid
    is rejected.  ``CRS.equals`` is intentionally not used as sole evidence:
    the source GeoTIFF carries a zero-parameter BoundCRS around its embedded
    EPSG:4548 projected CRS and serializes a different WKT.
    """
    if not isinstance(source_wkt, str) or not source_wkt.strip():
        raise PreparationCRSError("source CRS WKT is required for alias verification")
    digest = sha256(source_wkt.encode("utf-8")).hexdigest()
    if expected_wkt_sha256 is not None and digest != expected_wkt_sha256:
        raise PreparationCRSError("source CRS WKT SHA-256 differs from the configured identity")

    try:
        from pyproj import CRS

        parsed = CRS.from_wkt(source_wkt)
        target = CRS.from_epsg(4548)
    except Exception as exc:  # pragma: no cover - exercised with malformed WKT
        raise PreparationCRSError(f"source CRS WKT could not be parsed: {type(exc).__name__}: {exc}") from exc

    base = parsed.source_crs if parsed.is_bound else parsed
    if base is None or base.type_name != "Projected CRS" or target.type_name != "Projected CRS":
        raise PreparationCRSError("alias requires a projected source CRS and EPSG:4548 target")
    base_id = base.to_json_dict().get("id", {})
    if (str(base_id.get("authority", "")).upper(), str(base_id.get("code", ""))) != ("EPSG", "4548"):
        raise PreparationCRSError("the source projected CRS does not carry embedded EPSG:4548 identity")

    source_axes = [(axis.name.casefold(), axis.direction.casefold(), axis.unit_name.casefold(), float(axis.unit_conversion_factor))
                   for axis in base.axis_info]
    target_axes = [(axis.name.casefold(), axis.direction.casefold(), axis.unit_name.casefold(), float(axis.unit_conversion_factor))
                   for axis in target.axis_info]
    expected_xy = {("easting", "east", "metre"), ("northing", "north", "metre")}
    if len(source_axes) != 2 or len(target_axes) != 2:
        raise PreparationCRSError("source and target must each have exactly two projected axes")
    if {(name, direction, unit) for name, direction, unit, _ in source_axes} != expected_xy:
        raise PreparationCRSError("source axis names/directions/units do not define metre Easting/Northing XY")
    if {(name, direction, unit) for name, direction, unit, _ in target_axes} != expected_xy:
        raise PreparationCRSError("EPSG:4548 axes do not define metre Easting/Northing coordinates")
    if any(not _number_equal(factor, 1.0, absolute=1e-12) for *_, factor in source_axes + target_axes):
        raise PreparationCRSError("source or target projected axis unit factor is not exactly metre")

    source_op = base.coordinate_operation
    target_op = target.coordinate_operation
    if source_op is None or target_op is None:
        raise PreparationCRSError("source or target projected operation is unavailable")
    if (source_op.method_auth_name, source_op.method_code) != ("EPSG", "9807"):
        raise PreparationCRSError("source projection method is not EPSG Transverse Mercator (9807)")
    if (target_op.method_auth_name, target_op.method_code) != ("EPSG", "9807"):
        raise PreparationCRSError("target projection method is not EPSG Transverse Mercator (9807)")
    if len(source_op.params) != 5 or len(target_op.params) != 5:
        raise PreparationCRSError("source/target Transverse Mercator parameter count differs from the reviewed contract")
    parameters: list[dict[str, Any]] = []
    for actual, wanted in zip(source_op.params, target_op.params):
        if actual.name.casefold() != wanted.name.casefold() or actual.unit_name.casefold() != wanted.unit_name.casefold():
            raise PreparationCRSError("source/target projection parameter names or units differ")
        if not _number_equal(actual.value, wanted.value, absolute=1e-10):
            raise PreparationCRSError(f"source/target projection parameter '{actual.name}' differs")
        parameters.append({"name": wanted.name, "value": float(wanted.value), "unit": wanted.unit_name})

    source_datum = base.geodetic_crs.datum
    target_datum = target.geodetic_crs.datum
    if source_datum is None or target_datum is None or source_datum.name.casefold().replace("_", " ") != target_datum.name.casefold().replace("_", " "):
        raise PreparationCRSError("source/target geodetic datum names differ")
    source_ellipsoid, target_ellipsoid = base.ellipsoid, target.ellipsoid
    if not _number_equal(source_ellipsoid.semi_major_metre, target_ellipsoid.semi_major_metre, absolute=1e-8):
        raise PreparationCRSError("source/target ellipsoid semi-major axes differ")
    if not _number_equal(source_ellipsoid.inverse_flattening, target_ellipsoid.inverse_flattening, absolute=1e-9):
        raise PreparationCRSError("source/target ellipsoid inverse flattening differs")

    bound_operation: dict[str, Any] | None = None
    if parsed.is_bound:
        transform = parsed.coordinate_operation
        if transform is None or (transform.method_auth_name, transform.method_code) != ("EPSG", "9606"):
            raise PreparationCRSError("bound source CRS has an unreviewed transformation method")
        if len(transform.params) != 7 or any(not _number_equal(parameter.value, 0.0, absolute=0.0) for parameter in transform.params):
            raise PreparationCRSError("bound CRS transformation is non-zero or incomplete")
        bound_operation = {
            "method": transform.method_name,
            "method_authority": transform.method_auth_name,
            "method_code": transform.method_code,
            "parameters": [{"name": item.name, "value": float(item.value), "unit": item.unit_name}
                           for item in transform.params],
            "all_parameters_zero": True,
            "applied": False,
        }

    return {
        "format": "preparation-crs-equivalence-evidence-v1",
        "status": "verified_preparation_only",
        "source_crs_wkt_sha256": digest,
        "source_crs_name": parsed.name,
        "source_is_bound_crs": bool(parsed.is_bound),
        "source_projected_crs_authority": {"authority": str(base_id.get("authority")), "code": str(base_id.get("code"))},
        "target_crs": "EPSG:4548",
        "axis_comparison": {
            "source_axes_in_wkt_order": [{"name": a[0], "direction": a[1], "unit": a[2], "unit_factor": a[3]} for a in source_axes],
            "target_axes_in_registry_order": [{"name": a[0], "direction": a[1], "unit": a[2], "unit_factor": a[3]} for a in target_axes],
            "project_xy_policy": "easting_northing_no_axis_reorder",
        },
        "horizontal_unit": "metre",
        "projection": {"method": target_op.method_name, "authority": target_op.method_auth_name,
                       "code": target_op.method_code, "parameters": parameters},
        "geodetic_datum": target_datum.name,
        "ellipsoid": {"target_name": target_ellipsoid.name,
                      "source_semi_major_m": float(source_ellipsoid.semi_major_metre),
                      "target_semi_major_m": float(target_ellipsoid.semi_major_metre),
                      "source_inverse_flattening": float(source_ellipsoid.inverse_flattening),
                      "target_inverse_flattening": float(target_ellipsoid.inverse_flattening)},
        "bound_transformation": bound_operation,
        "coordinate_transform_performed": False,
        "coordinates_reordered": False,
        "raster_values_changed": False,
        "raw_source_wkt_retained": True,
    }


__all__ = ["PreparationCRSError", "prove_epsg4548_xy_alias"]
