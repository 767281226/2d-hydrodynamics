from __future__ import annotations

from dataclasses import dataclass

from pathlib import Path

import struct

import numpy as np

from .config import DomainConfig

class VectorMaskError(ValueError): pass

@dataclass(frozen=True)

class ActiveMask:

    mask: np.ndarray; nx:int; ny:int; dx:float; dy:float; xmin:float; ymin:float; coordinate_system:str; source_path:Path; source_crs:str

    def __post_init__(self):

        if self.mask.shape!=(self.ny,self.nx) or self.mask.dtype!=np.bool_: raise VectorMaskError("active mask shape/dtype mismatch")

    @property

    def active_count(self): return int(self.mask.sum())

    @property

    def active_ratio(self): return self.active_count / float(self.nx*self.ny)

@dataclass(frozen=True)

class LeveeLine:

    lines: tuple[tuple[tuple[float,float],...],...]; source_path:Path; source_crs:str; coordinate_system:str

    @property

    def line_count(self): return len(self.lines)

    @property

    def vertex_count(self): return sum(len(line) for line in self.lines)

    @property

    def bounds(self):

        points=[point for line in self.lines for point in line]

        if not points: raise VectorMaskError("levee line has no vertices")

        xs=[point[0] for point in points]; ys=[point[1] for point in points]

        return min(xs),min(ys),max(xs),max(ys)

def _prj(path):

    p=Path(path).with_suffix('.prj')

    if not p.is_file(): raise VectorMaskError(f"missing CRS sidecar: {p}")

    text=p.read_text(encoding='utf-8',errors='replace').strip()

    if not text: raise VectorMaskError("CRS sidecar is empty")

    return text

def _transform(points, source_crs, target_crs):

    if source_crs.strip().upper()==target_crs.strip().upper(): return tuple(points)

    try:

        from pyproj import Transformer

        return tuple(Transformer.from_crs(source_crs,target_crs,always_xy=True).itransform(points))

    except ImportError as exc: raise VectorMaskError("CRS conversion requires optional pyproj") from exc

    except Exception as exc: raise VectorMaskError(f"invalid CRS conversion: {exc}") from exc

def _read(path, expected):

    path=Path(path)

    if not path.is_file(): raise VectorMaskError(f"vector file not found: {path}")

    _prj(path); raw=path.read_bytes(); off=100; records=[]

    while off+8<=len(raw):

        _,words=struct.unpack('>2i',raw[off:off+8]); off+=8; end=off+words*2

        typ=struct.unpack('<i',raw[off:off+4])[0]

        if typ not in (3,5): off=end; continue

        n_parts,n_points=struct.unpack('<2i',raw[off+36:off+44]); base=off+44; starts=struct.unpack('<%di'%n_parts,raw[base:base+4*n_parts]); pos=base+4*n_parts; pts=[]

        for _ in range(n_points): pts.append(struct.unpack('<2d',raw[pos:pos+16])); pos+=16

        parts=tuple(tuple(pts[s:starts[i+1] if i+1<n_parts else n_points]) for i,s in enumerate(starts))

        if typ==expected: records.append(parts)

        off=end

    if not records: raise VectorMaskError("no compatible geometries found")

    return records

def _inside(x,y,ring):

    hit=False

    for (x1,y1),(x2,y2) in zip(ring,ring[1:]+ring[:1]):

        if ((y1>y)!=(y2>y)) and x < (x2-x1)*(y-y1)/(y2-y1+1e-30)+x1: hit=not hit

    return hit

def _rasterize(rings, domain):

    try:

        from rasterio.features import rasterize

        from rasterio.transform import from_origin

    except ImportError as exc: raise VectorMaskError("active mask rasterization requires optional rasterio") from exc

    shapes=[]

    for ring in rings:

        if len(ring)<3: continue

        closed=ring if ring[0]==ring[-1] else ring+(ring[0],)

        shapes.append(({"type":"Polygon","coordinates":[closed]},1))

    if not shapes: raise VectorMaskError("polygon has no usable rings")

    transform=from_origin(domain.xmin,domain.ymax,domain.dx,domain.dy)

    return rasterize(shapes,out_shape=(domain.ny,domain.nx),transform=transform,fill=0,default_value=1,all_touched=False).astype(bool)

def polygon_active_mask(path, domain:DomainConfig, *, target_crs:str):

    if (domain.xmin,domain.xmax,domain.ymin,domain.ymax,domain.dx,domain.dy)!=(474150,499500,4299600,4335550,50,50): raise VectorMaskError("domain must use confirmed V1 fixed 50 m extent")

    source=_prj(path); rings=[_transform(part,source,target_crs) for shape in _read(path,5) for part in shape]

    # Rasterio arrays are north-to-south (row 0 is ymax), while the model

    # TerrainField/ActiveMask convention is south-to-north (j=0 is ymin).

    # Flip once at the boundary so exported cells and solver-facing indices

    # use the same spatial orientation.

    mask=np.flipud(_rasterize(rings,domain))

    return ActiveMask(mask,domain.nx,domain.ny,domain.dx,domain.dy,domain.xmin,domain.ymin,target_crs,Path(path).resolve(),source)

def read_levee_line(path, *, target_crs:str):

    source=_prj(path); lines=tuple(_transform(part,source,target_crs) for shape in _read(path,3) for part in shape); return LeveeLine(lines,Path(path).resolve(),source,target_crs)



def _dbf_field_values(path, field_name: str) -> tuple[str, ...]:

    path = Path(path)

    cpg = path.with_suffix('.cpg')

    enc = 'utf-8'

    if cpg.is_file():

        raw_enc = cpg.read_text(encoding='ascii', errors='replace').strip()

        if raw_enc:

            enc = 'utf-8' if raw_enc.upper().replace('-', '') in {'UTF8', 'UTF8SIG'} else raw_enc

    dbf = path.with_suffix('.dbf').read_bytes()

    recs = struct.unpack('<I', dbf[4:8])[0]

    hlen = struct.unpack('<H', dbf[8:10])[0]

    rlen = struct.unpack('<H', dbf[10:12])[0]

    fields = []

    off = 32

    while off < hlen - 1:

        name = dbf[off:off+11].split(bytes([0]), 1)[0].decode('ascii', 'replace')

        width = dbf[off+16]

        fields.append((name, width))

        off += 32

    names = [item[0] for item in fields]

    if field_name not in names:

        raise VectorMaskError(f'dbf field not found: {field_name}')

    idx = names.index(field_name)

    start = 1 + sum(item[1] for item in fields[:idx])

    width = fields[idx][1]

    values = []

    for i in range(recs):

        rec = dbf[hlen + i * rlen:hlen + (i + 1) * rlen]

        blob = rec[start:start + width]

        try:

            text = blob.decode(enc)

        except Exception:

            text = blob.decode('gbk', errors='replace')

        values.append(text.strip())

    return tuple(values)



def read_named_polylines(path, *, name: str, target_crs: str, field: str = 'O_Name'):

    source = _prj(path)

    records = _read(path, 3)

    labels = _dbf_field_values(path, field)

    if len(labels) != len(records):

        raise VectorMaskError('shapefile feature count does not match dbf records')

    lines = []

    for parts, label in zip(records, labels):

        if label != name:

            continue

        for part in parts:

            if len(part) >= 2:

                lines.append(_transform(part, source, target_crs))

    if not lines:

        raise VectorMaskError(f'no polyline named {name!r}')

    return tuple(lines)

def write_active_grid_shapefile(active_mask: ActiveMask, path: str | Path, *, include_inactive: bool = False, target_crs: str | None = None) -> Path:

    """Write a diagnostic cell-polygon Shapefile; no source data is changed.



    By default only active cells are written to keep the preview compact. Set

    ``include_inactive=True`` to write the complete rectangular grid.

    """

    raise VectorMaskError(
        "structured-grid diagnostic rectangle SHP export is disabled; use hydrodynamics-triangle-shp with a validated native P4 result"
    )

    if not isinstance(active_mask, ActiveMask): raise TypeError("active_mask must be an ActiveMask")

    if target_crs is not None and target_crs.strip().upper() != active_mask.coordinate_system.strip().upper():

        return write_active_grid_shapefile_reprojected(active_mask, path, target_crs=target_crs, include_inactive=include_inactive)

    target=Path(path)

    if target.suffix.lower() != ".shp": target=target.with_suffix(".shp")

    target.parent.mkdir(parents=True,exist_ok=True)

    cells=[(j,i) for j in range(active_mask.ny) for i in range(active_mask.nx) if include_inactive or bool(active_mask.mask[j,i])]

    if not cells: raise VectorMaskError("active mask contains no cells to write")

    boxes=[(active_mask.xmin+i*active_mask.dx, active_mask.ymin+j*active_mask.dy, active_mask.xmin+(i+1)*active_mask.dx, active_mask.ymin+(j+1)*active_mask.dy) for j,i in cells]

    xmin=min(b[0] for b in boxes); ymin=min(b[1] for b in boxes); xmax=max(b[2] for b in boxes); ymax=max(b[3] for b in boxes)

    content_len=136; shp_len=(100+len(cells)*(8+content_len))//2; shx_len=(100+len(cells)*8)//2

    def header(length):

        h=bytearray(100); struct.pack_into(">i",h,0,9994); struct.pack_into(">5i",h,4,0,0,0,0,0); struct.pack_into(">i",h,24,length); struct.pack_into("<2i",h,28,1000,5); struct.pack_into("<4d",h,36,xmin,ymin,xmax,ymax); return bytes(h)

    with target.open("wb") as shp, target.with_suffix(".shx").open("wb") as shx:

        shp.write(header(shp_len)); shx_header=bytearray(header(shx_len)); shx.write(shx_header)

        offset=50

        for record,(x0,y0,x1,y1) in enumerate(boxes,1):

            points=((x0,y0),(x0,y1),(x1,y1),(x1,y0),(x0,y0))

            body=struct.pack("<i4d2i",5,x0,y0,x1,y1,1,5)+struct.pack("<i",0)+b"".join(struct.pack("<2d",*p) for p in points)

            shp.write(struct.pack(">2i",record,len(body)//2)); shp.write(body); shx.write(struct.pack(">2i",offset,len(body)//2)); offset += 4+len(body)//2

    with target.with_suffix(".dbf").open("wb") as dbf:

        fields=((b"row",b"N",8,0),(b"col",b"N",8,0),(b"active",b"L",1,0)); header_len=32+32*len(fields)+1; record_len=1+sum(f[2] for f in fields); now=__import__("datetime").date.today()

        dbf.write(struct.pack("<BBBBIHH20x",3,now.year-1900,now.month,now.day,len(cells),header_len,record_len))

        for name,typ,width,dec in fields: dbf.write(name.ljust(11,b"\0")+typ+bytes(4)+bytes((width,dec))+bytes(14))

        dbf.write(b"\r")

        for j,i in cells: dbf.write(b" "+str(j).rjust(8).encode()+str(i).rjust(8).encode()+b"T" if active_mask.mask[j,i] else b" "+str(j).rjust(8).encode()+str(i).rjust(8).encode()+b"F")

        dbf.write(b"\x1a")

    try:

        from pyproj import CRS

        target.with_suffix(".prj").write_text(CRS.from_user_input(active_mask.coordinate_system).to_wkt(),encoding="utf-8")

    except ImportError as exc: raise VectorMaskError("writing a projected grid requires optional pyproj") from exc

    return target

def write_active_grid_shapefile_reprojected(active_mask: ActiveMask, path: str | Path, *, target_crs: str, include_inactive: bool = False) -> Path:

    """Write the diagnostic grid in another CRS for GIS overlay only."""

    raise VectorMaskError(
        "structured-grid diagnostic rectangle SHP export is disabled; use hydrodynamics-triangle-shp with a validated native P4 result"
    )

    if not isinstance(target_crs, str) or not target_crs.strip(): raise VectorMaskError("target_crs must be a non-empty string")

    try:

        from pyproj import CRS, Transformer

        transformer=Transformer.from_crs(active_mask.coordinate_system,target_crs,always_xy=True)

        target_wkt=CRS.from_user_input(target_crs).to_wkt()

    except ImportError as exc: raise VectorMaskError("reprojected grid writing requires optional pyproj") from exc

    except Exception as exc: raise VectorMaskError(f"invalid target CRS: {exc}") from exc

    target=Path(path)

    if target.suffix.lower() != ".shp": target=target.with_suffix(".shp")

    target.parent.mkdir(parents=True,exist_ok=True)

    cells=[(j,i) for j in range(active_mask.ny) for i in range(active_mask.nx) if include_inactive or bool(active_mask.mask[j,i])]

    if not cells: raise VectorMaskError("active mask contains no cells to write")

    source_points=[]

    for j,i in cells:

        x0=active_mask.xmin+i*active_mask.dx; x1=x0+active_mask.dx; y0=active_mask.ymin+j*active_mask.dy; y1=y0+active_mask.dy

        source_points.extend(((x0,y0),(x0,y1),(x1,y1),(x1,y0),(x0,y0)))

    xs,ys=transformer.transform(np.asarray([p[0] for p in source_points]),np.asarray([p[1] for p in source_points]))

    polygons=[tuple((float(xs[k]),float(ys[k])) for k in range(n*5,n*5+5)) for n in range(len(cells))]

    xmin=min(p[0] for poly in polygons for p in poly); ymin=min(p[1] for poly in polygons for p in poly); xmax=max(p[0] for poly in polygons for p in poly); ymax=max(p[1] for poly in polygons for p in poly)

    content_len=136; shp_len=(100+len(polygons)*(8+content_len))//2; shx_len=(100+len(polygons)*8)//2

    def header(length):

        h=bytearray(100); struct.pack_into(">i",h,0,9994); struct.pack_into(">5i",h,4,0,0,0,0,0); struct.pack_into(">i",h,24,length); struct.pack_into("<2i",h,28,1000,5); struct.pack_into("<4d",h,36,xmin,ymin,xmax,ymax); return bytes(h)

    with target.open("wb") as shp, target.with_suffix(".shx").open("wb") as shx:

        shp.write(header(shp_len)); shx.write(header(shx_len)); offset=50

        for record,points in enumerate(polygons,1):

            bx0=min(p[0] for p in points); by0=min(p[1] for p in points); bx1=max(p[0] for p in points); by1=max(p[1] for p in points)

            body=struct.pack("<i4d2i",5,bx0,by0,bx1,by1,1,5)+struct.pack("<i",0)+b"".join(struct.pack("<2d",*p) for p in points)

            shp.write(struct.pack(">2i",record,len(body)//2)); shp.write(body); shx.write(struct.pack(">2i",offset,len(body)//2)); offset += 4+len(body)//2

    with target.with_suffix(".dbf").open("wb") as dbf:

        fields=((b"row",b"N",8,0),(b"col",b"N",8,0),(b"active",b"L",1,0)); header_len=32+32*len(fields)+1; record_len=1+sum(f[2] for f in fields); now=__import__("datetime").date.today()

        dbf.write(struct.pack("<BBBBIHH20x",3,now.year-1900,now.month,now.day,len(cells),header_len,record_len))

        for name,typ,width,dec in fields: dbf.write(name.ljust(11,b"\0")+typ+bytes(4)+bytes((width,dec))+bytes(14))

        dbf.write(b"\r")

        for j,i in cells: dbf.write(b" "+str(j).rjust(8).encode()+str(i).rjust(8).encode()+(b"T" if active_mask.mask[j,i] else b"F"))

        dbf.write(b"\x1a")

    target.with_suffix(".prj").write_text(target_wkt,encoding="utf-8")

    return target

__all__=["ActiveMask","LeveeLine","VectorMaskError","polygon_active_mask","read_levee_line","write_active_grid_shapefile","write_active_grid_shapefile_reprojected"]
