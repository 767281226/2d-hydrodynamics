from __future__ import annotations

from datetime import date
from pathlib import Path
import struct

import numpy as np

from .vector_mask import VectorMaskError


def _n(value: float | int, width: int, dec: int = 0) -> bytes:
    if dec:
        text = f"{float(value):.{dec}f}"
    else:
        text = str(int(value))
    if len(text) > width:
        text = text[:width]
    return text.rjust(width).encode("ascii")


def _c(value: str, width: int) -> bytes:
    text = str(value)[:width]
    return text.encode("ascii", errors="replace").ljust(width)


def _transformer(source_crs: str, output_crs: str):
    try:
        from pyproj import CRS, Transformer
    except Exception as exc:
        raise VectorMaskError(f"unable to load pyproj: {exc}") from exc
    try:
        src = CRS.from_user_input(source_crs)
        dst = CRS.from_user_input(output_crs)
        if src == dst or src.equals(dst):
            return None, dst
        return Transformer.from_crs(src, dst, always_xy=True), dst
    except Exception as exc:
        raise VectorMaskError(f"unable to build CRS transformer: {exc}") from exc


def write_grid_shapefile(
    path: str | Path,
    *,
    mask: np.ndarray,
    xmin: float,
    ymin: float,
    dx: float,
    dy: float,
    crs: str = "EPSG:4548",
    output_crs: str | None = None,
    max_depth: np.ndarray | None = None,
    elevation: np.ndarray | None = None,
    zone: np.ndarray | None = None,
) -> Path:
    """Write one polygon per selected grid cell."""

    selected = np.asarray(mask, dtype=bool)
    ny, nx = int(selected.shape[0]), int(selected.shape[1])
    if max_depth is None:
        depth = np.zeros(selected.shape, dtype=float)
    else:
        depth = np.asarray(max_depth, dtype=float)
        if depth.shape != selected.shape:
            raise VectorMaskError("max_depth shape must match mask")
    elev = None
    if elevation is not None:
        elev = np.asarray(elevation, dtype=float)
        if elev.shape != selected.shape:
            raise VectorMaskError("elevation shape must match mask")
    zone_arr = None
    if zone is not None:
        zone_arr = np.asarray(zone)
        if zone_arr.shape != selected.shape:
            raise VectorMaskError("zone shape must match mask")
    cells = [(j, i) for j in range(ny) for i in range(nx) if bool(selected[j, i])]
    source_polys = []
    centers = []
    for j, i in cells:
        x0 = xmin + i * dx
        y0 = ymin + j * dy
        x1 = x0 + dx
        y1 = y0 + dy
        source_polys.append(((x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)))
        centers.append((x0 + 0.5 * dx, y0 + 0.5 * dy))
    dest_crs = output_crs or crs
    transformer, dest = _transformer(crs, dest_crs)
    if transformer is None:
        polygons = source_polys
        out_centers = centers
    else:
        sx = np.asarray([pt[0] for poly in source_polys for pt in poly], dtype=float)
        sy = np.asarray([pt[1] for poly in source_polys for pt in poly], dtype=float)
        tx, ty = transformer.transform(sx, sy)
        polygons = [
            tuple((float(tx[k]), float(ty[k])) for k in range(n * 5, n * 5 + 5))
            for n in range(len(cells))
        ]
        cx, cy = transformer.transform(
            np.asarray([pt[0] for pt in centers], dtype=float),
            np.asarray([pt[1] for pt in centers], dtype=float),
        )
        out_centers = list(zip((float(v) for v in cx), (float(v) for v in cy)))
    # A dry run still exports a valid, empty maximum-inundation layer.
    bx0 = min((pt[0] for poly in polygons for pt in poly), default=0.0)
    by0 = min((pt[1] for poly in polygons for pt in poly), default=0.0)
    bx1 = max((pt[0] for poly in polygons for pt in poly), default=0.0)
    by1 = max((pt[1] for poly in polygons for pt in poly), default=0.0)
    # Polygon content: type/bbox/counts (44), one part index (4), five XY pairs (80).
    content_len = 128
    shp_len = (100 + len(cells) * (8 + content_len)) // 2
    shx_len = (100 + len(cells) * 8) // 2

    def header(length: int) -> bytes:
        h = bytearray(100)
        struct.pack_into(">i", h, 0, 9994)
        struct.pack_into(">5i", h, 4, 0, 0, 0, 0, 0)
        struct.pack_into(">i", h, 24, length)
        struct.pack_into("<2i", h, 28, 1000, 5)
        struct.pack_into("<4d", h, 36, bx0, by0, bx1, by1)
        return bytes(h)

    target = Path(path)
    if target.suffix.lower() != ".shp":
        target = target.with_suffix(".shp")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as shp, target.with_suffix(".shx").open("wb") as shx:
        shp.write(header(shp_len))
        shx.write(header(shx_len))
        offset = 50
        for record, points in enumerate(polygons, 1):
            px0 = min(pt[0] for pt in points)
            py0 = min(pt[1] for pt in points)
            px1 = max(pt[0] for pt in points)
            py1 = max(pt[1] for pt in points)
            body = struct.pack("<i4d2i", 5, px0, py0, px1, py1, 1, 5) + struct.pack("<i", 0)
            body += b"".join(struct.pack("<2d", *pt) for pt in points)
            shp.write(struct.pack(">2i", record, len(body) // 2))
            shp.write(body)
            shx.write(struct.pack(">2i", offset, len(body) // 2))
            offset += 4 + len(body) // 2
    geographic = bool(getattr(dest, "is_geographic", False))
    xy_dec = 7 if geographic else 2
    x_width = 12
    y_width = 12 if geographic else 13
    fields: list[tuple[bytes, bytes, int, int]] = [
        (b"cell_id", b"N", 10, 0),
        (b"row", b"N", 8, 0),
        (b"col", b"N", 8, 0),
        (b"max_h", b"N", 8, 3),
    ]
    if elev is not None:
        fields.append((b"elev", b"N", 8, 3))
    if zone_arr is not None:
        fields.append((b"zone", b"C", 12, 0))
    fields.extend(
        (
            (b"x", b"N", x_width, xy_dec),
            (b"y", b"N", y_width, xy_dec),
        )
    )
    header_len = 32 + 32 * len(fields) + 1
    record_len = 1 + sum(item[2] for item in fields)
    now = date.today()
    with target.with_suffix(".dbf").open("wb") as dbf:
        dbf.write(struct.pack("<BBBBIHH20x", 3, now.year - 1900, now.month, now.day, len(cells), header_len, record_len))
        for name, typ, width, dec in fields:
            dbf.write(name.ljust(11, b"\0") + typ + bytes(4) + bytes((width, dec)) + bytes(14))
        dbf.write(b"\r")
        for (j, i), (xc, yc) in zip(cells, out_centers):
            cid = j * nx + i + 1
            row = b" " + _n(cid, 10) + _n(j, 8) + _n(i, 8) + _n(float(depth[j, i]), 8, 3)
            if elev is not None:
                row += _n(float(elev[j, i]), 8, 3)
            if zone_arr is not None:
                row += _c(str(zone_arr[j, i]), 12)
            row += _n(xc, x_width, xy_dec) + _n(yc, y_width, xy_dec)
            dbf.write(row)
        dbf.write(b"\x1a")
    target.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    try:
        target.with_suffix(".prj").write_text(dest.to_wkt(), encoding="utf-8")
    except Exception as exc:
        raise VectorMaskError(f"unable to write PRJ: {exc}") from exc
    return target


def write_wet_grid_shapefile(
    path: str | Path,
    *,
    wet_mask: np.ndarray,
    max_depth: np.ndarray,
    xmin: float,
    ymin: float,
    dx: float,
    dy: float,
    crs: str = "EPSG:4548",
    output_crs: str | None = None,
) -> Path:
    """Write one polygon per wet cell for third-party GIS use."""

    return write_grid_shapefile(
        path,
        mask=wet_mask,
        max_depth=max_depth,
        xmin=xmin,
        ymin=ymin,
        dx=dx,
        dy=dy,
        crs=crs,
        output_crs=output_crs,
    )


__all__ = ["write_grid_shapefile", "write_wet_grid_shapefile"]
