"""Prepare a workflow grid from an already mapped NC and existing case geometry."""

from pathlib import Path
import argparse
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import netCDF4
import numpy as np
from hydrodynamics import load_config
from hydrodynamics.hourly_inundation import classify_qingbei_mask, classify_dongdian_mask, polyline_buffer_mask
from hydrodynamics.vector_mask import polygon_active_mask, read_levee_line, read_named_polylines
from hydrodynamics.workflow_inputs import load_prepared_workflow_grid


def main():
    parser = argparse.ArgumentParser(description='将已经映射好的NC网格整理为计算流程输入；不读取原始DEM')
    parser.add_argument('--source-nc', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path('examples/case_001/config.yaml'))
    parser.add_argument('--polygon', type=Path, default=Path('map/东淀&文安洼_shape.shp'))
    parser.add_argument('--divider', type=Path, default=Path('map/文安洼东淀_分界线.shp'))
    parser.add_argument('--river', type=Path, default=Path('map/river_track/river_track.shp'))
    parser.add_argument('--source-mask', choices=('qingbei','combined'), default='qingbei')
    parser.add_argument('--river-buffer-m', type=float, default=100.0)
    parser.add_argument('--inlet-width-m', type=float, default=800.0)
    args = parser.parse_args()
    if args.source_nc.suffix.lower() != '.nc' or args.output.suffix.lower() != '.npz':
        raise ValueError('source-nc must be an already mapped .nc; output must be .npz')
    if args.output.exists():
        raise FileExistsError(args.output)
    for name in ('river_buffer_m','inlet_width_m'):
        if not np.isfinite(getattr(args,name)) or getattr(args,name) <= 0:
            raise ValueError(f'{name} must be finite and positive, in m')
    case = load_config(args.config)
    with netCDF4.Dataset(args.source_nc) as ds:
        elevation = ds.variables['elevation'][:]
        if np.ma.getmaskarray(elevation).any():
            raise ValueError('source elevation has NoData; input preparation does not fill it')
        z = np.asarray(elevation, dtype=float)
        ds.set_auto_mask(False)
        source_mask = np.asarray(ds.variables['active_mask'][:], dtype=bool)
        x = np.asarray(ds.variables['x'][:], dtype=float)
        y = np.asarray(ds.variables['y'][:], dtype=float)
        crs = getattr(ds,'coordinate_system', None)
        datum = getattr(ds,'vertical_datum', None)
        if not datum or datum == 'unknown' or crs != case.model.coordinate_system:
            raise ValueError('source must declare a vertical datum and the same CRS as case_config')
    combined = polygon_active_mask(args.polygon, case.domain, target_crs=crs).mask
    divider = read_levee_line(args.divider, target_crs=crs).lines[0]
    river = read_named_polylines(args.river, name='大清河', target_crs=crs)
    active, _ = classify_qingbei_mask(combined,x,y,divider,z,daqing_river=river)
    if not np.array_equal(source_mask, active if args.source_mask == 'qingbei' else combined):
        raise ValueError('source active_mask differs from the explicitly selected source-mask scope')
    dongdian, wenanwa = classify_dongdian_mask(combined,x,y,divider)
    conveyance = polyline_buffer_mask(x,y,river,args.river_buffer_m) & dongdian
    xx, _ = np.meshgrid(x,y)
    inlet = (active | conveyance) & (xx <= xx[active].min()+args.inlet_width_m)
    zones = np.full(z.shape,'none',dtype='<U12')
    zones[wenanwa] = 'wenanwa'
    zones[dongdian & ~active] = 'qingnan'
    zones[active] = 'qingbei'
    provenance = json.dumps({'source_netcdf':str(args.source_nc.resolve()),
                             'source_sha256':hashlib.sha256(args.source_nc.read_bytes()).hexdigest(),
                             'source_mask':args.source_mask, 'river_buffer_m':args.river_buffer_m,
                             'inlet_width_m':args.inlet_width_m},ensure_ascii=False)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('xb') as stream:
        np.savez_compressed(stream,elevation=z,active_mask=active,combined_mask=combined,inlet_mask=inlet,
                            conveyance_mask=conveyance,zones=zones,x=x,y=y,coordinate_system=crs,
                            vertical_datum=datum,elevation_unit='m',provenance=provenance)
    load_prepared_workflow_grid(args.output,case)
    print(args.output.resolve())


if __name__ == '__main__':
    main()
