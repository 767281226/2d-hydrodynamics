import struct
from pathlib import Path
import pytest
import numpy as np
from hydrodynamics.config import DomainConfig
from hydrodynamics.vector_mask import ActiveMask, VectorMaskError, polygon_active_mask, read_levee_line, write_active_grid_shapefile, write_active_grid_shapefile_reprojected

def _write(path, typ, parts):
    points=[p for part in parts for p in part]; starts=[]; n=0
    for part in parts: starts.append(n); n+=len(part)
    body=struct.pack("<i4d2i",typ,0,0,0,0,len(parts),len(points))+struct.pack("<%di"%len(starts),*starts)+b"".join(struct.pack("<2d",*p) for p in points)
    rec=struct.pack(">2i",1,len(body)//2)+body; header=bytearray(100); struct.pack_into(">i",header,0,9994); struct.pack_into(">i",header,24,(100+len(rec))//2); struct.pack_into("<i",header,28,1000); struct.pack_into("<i4d",header,32,typ,0,0,100,100); path.write_bytes(bytes(header)+rec); path.with_suffix('.prj').write_text('EPSG:4548',encoding='utf-8')

def test_polygon_mask_shape_and_non_rectangle(tmp_path):
    p=tmp_path/'poly.shp'; _write(p,5,[[(474150,4299600),(474250,4299600),(474150,4299700),(474150,4299600)]])
    d=DomainConfig(xmin=474150,xmax=499500,ymin=4299600,ymax=4335550,dx=50,dy=50); m=polygon_active_mask(p,d,target_crs='EPSG:4548')
    assert m.mask.shape==(719,507); assert 0<m.mask.sum()<719*507
    assert m.mask[0,0]
    assert not m.mask[-1,0]

def test_structured_grid_writers_refuse_before_creating_files(tmp_path):
    active = ActiveMask(np.array([[True]], dtype=bool), 1, 1, 1.0, 1.0, 0.0, 0.0,
                        'EPSG:32650', tmp_path/'source.shp', 'EPSG:32650')
    target = tmp_path/'new-output'/'grid.shp'
    with pytest.raises(VectorMaskError, match='native P4 result'):
        write_active_grid_shapefile(active, target, target_crs='EPSG:4490')
    with pytest.raises(VectorMaskError, match='native P4 result'):
        write_active_grid_shapefile_reprojected(active, target, target_crs='EPSG:4490')
    assert not target.parent.exists()
    assert not list(tmp_path.rglob('*.shp'))
    assert not list(tmp_path.rglob('*.dbf'))

def test_missing_prj_and_levee(tmp_path):
    p=tmp_path/'poly.shp'; _write(p,5,[[(0,0),(1,0),(1,1),(0,0)]]); p.with_suffix('.prj').unlink(); d=DomainConfig(xmin=474150,xmax=499500,ymin=4299600,ymax=4335550,dx=50,dy=50)
    with pytest.raises(VectorMaskError): polygon_active_mask(p,d,target_crs='EPSG:4548')
    l=tmp_path/'line.shp'; _write(l,3,[[(474150,4299600),(474200,4299650)]]); assert len(read_levee_line(l,target_crs='EPSG:4548').lines)==1


def test_pyproj_missing_is_explicit(tmp_path, monkeypatch):
    import builtins
    p=tmp_path/"poly.shp"; _write(p,5,[[(474150,4299600),(474250,4299600),(474150,4299700),(474150,4299600)]])
    p.with_suffix(".prj").write_text("GEOGCS[\"CGCS2000\"]",encoding="utf-8")
    d=DomainConfig(xmin=474150,xmax=499500,ymin=4299600,ymax=4335550,dx=50,dy=50)
    real_import = builtins.__import__
    def without_pyproj(name, *args, **kwargs):
        if name == "pyproj" or name.startswith("pyproj."):
            raise ImportError("simulated missing optional pyproj")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_pyproj)
    with pytest.raises(VectorMaskError, match="pyproj"):
        polygon_active_mask(p,d,target_crs="EPSG:4548")
