"""Command-driven hourly calculation and complete result-bundle workflow."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
from importlib import resources
import json
from pathlib import Path
import struct
import sys
import time

import numpy as np

from .config import load_config
from .gate_dispatch import GateDispatchInput, GateOperation, build_gate_dispatch_log, gate_dispatch_paths
from .hourly_inundation import HourlyInundationSolver, write_hourly_netcdf
from .inundation_export import write_grid_shapefile, write_wet_grid_shapefile
from .nc2web import (
    FRAME_MAGIC, MESH_MAGIC, convert_netcdf_to_web, convert_netcdf_to_step_json,
    write_socio_overlay_rasters,
)
from .workflow_config import WorkflowError, load_workflow_config, resolve_workflow_path, validate_run_id
from .workflow_inputs import load_prepared_workflow_grid, load_workflow_forcing


_STAGES = ('preflight', 'prepare', 'model', 'gate_dispatch', 'shp', 'nc2web', 'stepjson', 'socio_overlay', 'viewer', 'validate')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _save_report(path, report):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def _dispatch_inputs(spec, dispatch_path, forcing_path, forcing):
    data = (GateDispatchInput.model_validate_json(dispatch_path.read_text(encoding='utf-8-sig'))
            if dispatch_path else GateDispatchInput(time_origin=spec.time_origin, provenance=spec.forcing.source))
    if data.time_origin and datetime.fromisoformat(data.time_origin) != datetime.fromisoformat(spec.time_origin):
        raise WorkflowError('dispatch.time_origin must represent the same instant as workflow.time_origin')
    automatic = []
    values = forcing.xihe_component_m3s
    if spec.record_xihe_component_changes and values is not None:
        indices = (list(np.flatnonzero(np.diff(values) != 0)+1))
        if values[0] > 0:
            indices.insert(0, 0)
        for i in indices:
            automatic.append(GateOperation(
                time_s=float(forcing.time_s[i]), gate_id='xihe', rule_id='GDR-005',
                action='调整模型预设的西河闸分泄流量',
                reason='forcing.csv 中西河闸分量发生变化，已作为总出流输入；原规则排沥/水质判断由调度快照另行提供。',
                source=f'model_prescribed_forcing:{forcing_path}:row={int(i)+2}; {spec.forcing.source}',
                discharge_before_m3s=float(values[i-1]) if i else None, discharge_after_m3s=float(values[i]),
            ))
    external_keys = {(op.time_s, op.gate_id) for op in data.operations}
    if any((op.time_s, op.gate_id) in external_keys for op in automatic):
        raise WorkflowError('dispatch operation overlaps an automatic XiHe component record; set record_xihe_component_changes=false to use supplied records')
    operations = sorted((*data.operations, *automatic), key=lambda op: (op.time_s, op.gate_id))
    merged = GateDispatchInput.model_validate({**data.model_dump(), 'time_origin': data.time_origin or spec.time_origin,
                                               'operations': [op.model_dump() for op in operations]})
    build_gate_dispatch_log(forcing.time_s, merged)
    return merged


def _export_shapes(target, bundle, grid, case):
    import netCDF4
    with netCDF4.Dataset(target) as ds:
        ds.set_auto_mask(False)
        maximum = np.asarray(ds.variables['maximum_depth'][:], dtype=float)
    wet = grid.active_mask & np.isfinite(maximum) & (maximum > 0)
    layers = []
    for crs, suffix in ((grid.coordinate_system, ''), ('EPSG:4490', '_epsg4490')):
        for kind in ('inundation_max_50m', 'detention_basin_grid_50m'):
            path = bundle / 'shp' / f'{kind}{suffix}.shp'
            geometry = dict(xmin=case.domain.xmin, ymin=case.domain.ymin, dx=case.domain.dx, dy=case.domain.dy,
                            crs=grid.coordinate_system, output_crs=crs)
            if kind == 'inundation_max_50m':
                write_wet_grid_shapefile(path, wet_mask=wet, max_depth=maximum, **geometry)
                count = int(wet.sum())
            else:
                write_grid_shapefile(path, mask=grid.combined_mask, max_depth=maximum,
                                     elevation=grid.elevation, zone=grid.zones, **geometry)
                count = int(grid.combined_mask.sum())
            layers.append({'path': path.relative_to(bundle).as_posix(), 'crs': crs, 'feature_count': count})
    return {'layers': layers}


def _validate_outputs(target, bundle, grid, forcing, report):
    import netCDF4
    from pyproj import CRS
    with netCDF4.Dataset(target) as ds:
        ds.set_auto_mask(False)
        if ds.coordinate_system != grid.coordinate_system or ds.vertical_datum != grid.vertical_datum:
            raise WorkflowError('output NC CRS/datum differs from the prepared grid')
        if not np.allclose(ds.variables['x'][:], grid.x, rtol=0, atol=1e-6) or not np.allclose(ds.variables['y'][:], grid.y, rtol=0, atol=1e-6):
            raise WorkflowError('output NC coordinate axes differ from the prepared grid')
        if not np.array_equal(ds.variables['time'][:], forcing.time_s):
            raise WorkflowError('output NC time coordinate differs from forcing.time_s')
        for name in ('water_depth','water_level','velocity_x','velocity_y','velocity_magnitude','discharge'):
            var = ds.variables[name]
            if var.shape != (len(forcing.time_s), *grid.elevation.shape):
                raise WorkflowError(f'output NC {name} shape is invalid')
            for i in range(len(forcing.time_s)):
                values = np.asarray(var[i])
                if not np.isfinite(values).all():
                    raise WorkflowError(f'output NC {name}[{i}] contains nonfinite values')
                if name == 'water_depth' and (np.any(values < 0) or np.any(values[~grid.active_mask] != 0)):
                    raise WorkflowError(f'output NC water_depth[{i}] is negative or outside the active mask')
        stats = {'max_depth_m': float(ds.variables['maximum_depth'][:].max()),
                 'max_wet_area_km2': float(ds.variables['wet_area'][:].max()/1e6),
                 'max_storage_m3': float(ds.variables['volume'][:].max()),
                 'final_water_level_m': float(ds.variables['eta'][-1])}
    manifest = json.loads((bundle/'nc2web/manifest.json').read_text(encoding='utf-8'))
    index = json.loads((bundle/'stepjson/index.json').read_text(encoding='utf-8'))
    for description, payload in (('nc2web', manifest), ('stepjson', index)):
        if payload['status'] != 'pass' or Path(payload['source']).resolve() != target.resolve() or not np.array_equal(payload['times'], forcing.time_s):
            raise WorkflowError(f'{description} status/source/times do not match this calculation')
    if len(manifest['frames']) != len(forcing.time_s) or len(index['files']) != len(forcing.time_s):
        raise WorkflowError('nc2web/stepjson frame counts do not match calculation')
    if len(list((bundle/'nc2web').glob('frame_*.bin'))) != len(forcing.time_s) or len(list((bundle/'stepjson').glob('step_*.json'))) != len(forcing.time_s):
        raise WorkflowError('nc2web/stepjson actual file counts do not match calculation')
    mesh_path = bundle/'nc2web/mesh.bin'
    with mesh_path.open('rb') as stream:
        if stream.read(len(MESH_MAGIC)) != MESH_MAGIC:
            raise WorkflowError('nc2web mesh binary header is invalid')
        nodes, triangles, _, _, _ = struct.unpack('<IIfff', stream.read(20))
        if nodes != manifest['node_count'] or triangles != manifest['triangle_count'] or mesh_path.stat().st_size != len(MESH_MAGIC)+20+nodes*8+triangles*12:
            raise WorkflowError('nc2web mesh binary is incomplete')
    expected_frame_size = len(FRAME_MAGIC) + 12 + grid.elevation.size * len(manifest['variables']) * 4
    for item in manifest['frames']:
        path = bundle/'nc2web'/item['file']
        with path.open('rb') as stream:
            if stream.read(len(FRAME_MAGIC)) != FRAME_MAGIC or path.stat().st_size != expected_frame_size:
                raise WorkflowError(f'nc2web frame binary is incomplete: {path.name}')
            if struct.unpack('<III', stream.read(12)) != (*grid.elevation.shape, len(manifest['variables'])):
                raise WorkflowError(f'nc2web frame dimensions are invalid: {path.name}')
    if len(index['wet_counts']) != len(index['files']):
        raise WorkflowError('stepjson wet_counts is incomplete')
    for name, count in zip(index['files'], index['wet_counts']):
        payload = json.loads((bundle/'stepjson'/name).read_text(encoding='utf-8'))
        if len(payload['all']) != count:
            raise WorkflowError(f'stepjson {name} record count differs from index')
    for layer in report['workflow']['shp']['layers']:
        path = bundle/layer['path']
        if any(not path.with_suffix(suffix).is_file() for suffix in ('.shp','.shx','.dbf','.prj','.cpg')):
            raise WorkflowError(f'SHP components missing: {path.name}')
        with path.with_suffix('.dbf').open('rb') as stream:
            count = struct.unpack('<I', stream.read(8)[4:8])[0]
        if count != layer['feature_count'] or CRS.from_wkt(path.with_suffix('.prj').read_text(encoding='utf-8')).to_epsg() != int(layer['crs'].split(':')[1]):
            raise WorkflowError(f'SHP count/CRS mismatch: {path.name}')
        for suffix in ('.shp','.shx'):
            component = path.with_suffix(suffix)
            with component.open('rb') as stream:
                header = stream.read(100)
            if len(header) != 100 or struct.unpack('>I', header[24:28])[0]*2 != component.stat().st_size:
                raise WorkflowError(f'SHP component is incomplete: {component.name}')
        if path.with_suffix('.shx').stat().st_size != 100+count*8:
            raise WorkflowError(f'SHP index count mismatch: {path.name}')
    if not (bundle/'viewer.html').is_file() or not (bundle/'run.json').is_file():
        raise WorkflowError('viewer/run metadata is missing')
    return {'frames_checked': len(forcing.time_s), **stats}


def run_workflow(workflow_file: str | Path, *, run_id: str | None = None, output_directory: str | Path | None = None) -> Path:
    workflow_path = Path(workflow_file).resolve()
    spec = load_workflow_config(workflow_path)
    identifier = validate_run_id(run_id or datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f'))
    parent = (Path(output_directory).expanduser().resolve() if output_directory else resolve_workflow_path(workflow_path, spec.output_directory))
    bundle = parent / identifier
    if bundle.exists():
        raise WorkflowError(f'run directory already exists: {bundle}; choose a new --id')
    bundle.mkdir(parents=True)
    target = bundle/spec.result_name
    report_path = bundle/'run_report.json'
    started = time.perf_counter()
    report = {'schema_version':'1.0', 'id':identifier, 'status':'running', 'started_at':_now(),
              'workflow_file':str(workflow_path), 'workflow_spec':spec.model_dump(), 'bundle_dir':str(bundle),
              'result_file':str(target), 'workflow':{name:{'status':'pending'} for name in _STAGES}}
    _save_report(report_path, report)
    context = {}

    def stage(name, action):
        entry = report['workflow'][name]
        entry.update(status='running', started_at=_now())
        _save_report(report_path, report)
        print(f'[{name}] running', flush=True)
        try:
            details = action() or {}
        except BaseException as exc:
            entry.update(status='failed', finished_at=_now(), error=f'{type(exc).__name__}: {exc}')
            report.update(status='failed', failed_stage=name, error=entry['error'], finished_at=_now(),
                          elapsed_seconds=time.perf_counter()-started)
            _save_report(report_path, report)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise WorkflowError(f'workflow failed at {name}: {exc}; report: {report_path}') from exc
        entry.update(status='complete', finished_at=_now(), **details)
        _save_report(report_path, report)
        print(f'[{name}] complete', flush=True)

    def preflight():
        for dependency in ('netCDF4','pyproj'):
            if importlib.util.find_spec(dependency) is None:
                raise WorkflowError(f'missing {dependency}; install with python -m pip install -e ".[workflow]"')
        paths = {name:resolve_workflow_path(workflow_path, value) for name,value in (
            ('case_config',spec.case_config), ('prepared_grid',spec.prepared_grid), ('forcing',spec.forcing.file),
            ('dispatch',spec.dispatch_file), ('socio',spec.socio_overlay_shp),
        ) if value is not None}
        for name, path in paths.items():
            if not path.is_file():
                raise WorkflowError(f'{name} input not found: {path}')
        if 'socio' in paths and not paths['socio'].with_suffix('.dbf').is_file():
            raise WorkflowError('socio_overlay_shp requires its DBF sidecar')
        resources.files('hydrodynamics').joinpath('assets/viewer.html').read_bytes()
        case = load_config(paths['case_config'])
        if case.model.start_time != 0:
            raise WorkflowError('hourly workflow requires model.start_time=0; use time_origin to shift calendar time')
        if case.initial_condition.type.value != 'constant_depth' or case.initial_condition.depth != 0:
            raise WorkflowError('hourly workflow requires initial_condition=constant_depth with depth=0')
        if case.roughness.type.value != 'constant':
            raise WorkflowError('hourly workflow requires constant roughness.manning_n')
        context.update(paths=paths, case=case)
        hashes = {name:_sha(path) for name,path in paths.items()}
        if 'socio' in paths:
            hashes['socio_dbf'] = _sha(paths['socio'].with_suffix('.dbf'))
        return {'input_files':{name:str(path) for name,path in paths.items()}, 'input_sha256':hashes}

    def prepare():
        paths, case = context['paths'], context['case']
        grid = load_prepared_workflow_grid(paths['prepared_grid'], case)
        forcing = load_workflow_forcing(paths['forcing'], case)
        dispatch = _dispatch_inputs(spec, paths.get('dispatch'), paths['forcing'], forcing)
        context.update(grid=grid, forcing=forcing, dispatch=dispatch)
        (bundle/'dispatch_input.json').write_text(dispatch.model_dump_json(indent=2), encoding='utf-8')
        return {'frames':len(forcing.time_s), 'active_cells':int(grid.active_mask.sum()),
                'combined_cells':int(grid.combined_mask.sum()), 'prepared_grid_provenance':grid.provenance,
                'forcing_source':spec.forcing.source, 'case_config_snapshot':case.model_dump(mode='json')}

    def model():
        case, grid, forcing = context['case'], context['grid'], context['forcing']
        class ProgressSolver(HourlyInundationSolver):
            def step(self, **kwargs):
                state = super().step(**kwargs)
                completed = int(round(state.time/case.output.interval))
                if completed % max(1, (len(forcing.time_s)-1)//12) == 0:
                    print(f'[model] {completed}/{len(forcing.time_s)-1} steps', flush=True)
                return state
        settings = spec.settings
        solver = ProgressSolver(grid.elevation, grid.active_mask, dx=case.domain.dx, dy=case.domain.dy,
                                xmin=case.domain.xmin, ymin=case.domain.ymin, manning_n=case.roughness.manning_n,
                                min_depth=case.numerics.min_depth, gravity=case.model.gravity,
                                eta_crest=settings.eta_crest_m, inlet_mask=grid.inlet_mask,
                                conveyance_mask=grid.conveyance_mask, water_surface_slope=settings.water_surface_slope,
                                max_spread_m_s=settings.max_spread_m_s,
                                area_schedule=[(p.time_h,p.fraction) for p in settings.area_schedule] or None)
        write_hourly_netcdf(target, solver, times_s=forcing.time_s, inflow=forcing.inflow_m3s,
                            outflow=forcing.outflow_m3s, rain_mm=forcing.rainfall_mm_per_step,
                            coordinate_system=grid.coordinate_system, vertical_datum=grid.vertical_datum,
                            overwrite=False, gate_dispatch=context['dispatch'], extra_attrs={
                                'case':case.model.name, 'run_id':identifier, 'event_start':spec.time_origin,
                                'event_end':(datetime.fromisoformat(spec.time_origin)+timedelta(seconds=float(forcing.time_s[-1]))).isoformat(),
                                'prepared_input_source':str(context['paths']['prepared_grid']),
                                'forcing_source':spec.forcing.source, 'workflow_settings':spec.settings.model_dump_json(),
                            })
        return {'file':target.name, 'frames':len(forcing.time_s), 'size_bytes':target.stat().st_size}

    def gate_records():
        csv_path, json_path = gate_dispatch_paths(target)
        if not csv_path.is_file() or not json_path.is_file():
            raise WorkflowError('gate dispatch CSV/JSON are missing after model export')
        payload = json.loads(json_path.read_text(encoding='utf-8'))
        with csv_path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        if (payload['reported_operation_count'] != len(context['dispatch'].operations)
                or len(rows) != len(payload['records'])
                or sum(row.get('record_type') == 'operation' for row in rows) != payload['reported_operation_count']
                or any(row.get('event_id') != str(record['event_id']) for row,record in zip(rows,payload['records']))):
            raise WorkflowError('gate dispatch operation count differs from supplied input')
        return {'csv':csv_path.name, 'json':json_path.name, 'reported_operations':payload['reported_operation_count'],
                'trigger_count':payload['trigger_count'], 'indeterminate_rules':sum(e['status']=='indeterminate' for e in payload['final_evaluations'])}

    def web():
        result = convert_netcdf_to_web(target, bundle/'nc2web')
        if result.status != 'pass':
            raise WorkflowError(f'nc2web conversion returned {result.status}: {result.blockers or result.warnings}')
        return {'frames':len(result.frames), 'epsg':result.epsg, 'manifest':'nc2web/manifest.json'}

    def steps():
        result = convert_netcdf_to_step_json(target, bundle/'stepjson')
        if result.status != 'pass':
            raise WorkflowError(f'stepjson conversion returned {result.status}: {result.warnings}')
        return {'files':len(result.files), 'epsg':result.epsg, 'index':'stepjson/index.json'}

    def socio():
        case = context['case']
        return write_socio_overlay_rasters(bundle/'nc2web', nx=case.domain.nx, ny=case.domain.ny,
                                           xmin=case.domain.xmin, ymin=case.domain.ymin, dx=case.domain.dx,
                                           dy=case.domain.dy, shp_path=context['paths']['socio'])

    def viewer():
        (bundle/'viewer.html').write_bytes(resources.files('hydrodynamics').joinpath('assets/viewer.html').read_bytes())
        (bundle/'run.json').write_text(json.dumps({'id':identifier,'out_dir':str(parent),'bundle_dir':str(bundle)}, indent=2), encoding='utf-8')
        summary = {'source':str(target), 'output_dir':str(bundle),
                   'nc2web':report['workflow']['nc2web'], 'stepjson':report['workflow']['stepjson'],
                   'socio':report['workflow']['socio_overlay']}
        (bundle/'parse_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'file':'viewer.html', 'run_metadata':'run.json'}

    stage('preflight', preflight)
    stage('prepare', prepare)
    stage('model', model)
    stage('gate_dispatch', gate_records)
    stage('shp', lambda:_export_shapes(target,bundle,context['grid'],context['case']))
    stage('nc2web', web)
    stage('stepjson', steps)
    if spec.socio_overlay_shp:
        stage('socio_overlay', socio)
    else:
        report['workflow']['socio_overlay'] = {'status':'skipped','reason':'socio_overlay_shp is not configured'}
        _save_report(report_path, report)
    stage('viewer', viewer)
    stage('validate', lambda:_validate_outputs(target,bundle,context['grid'],context['forcing'],report))
    report.update(status='complete', finished_at=_now(), elapsed_seconds=time.perf_counter()-started)
    _save_report(report_path, report)
    print(f'[complete] {report_path}', flush=True)
    return bundle


def find_latest_completed_bundle(directory: str | Path) -> Path | None:
    candidates = []
    for path in Path(directory).glob('*/run_report.json'):
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
            bundle = path.parent
            required = ('viewer.html','nc2web/manifest.json','nc2web/mesh.bin','stepjson/index.json')
            result = Path(report['result_file'])
            if (report['status'] == 'complete' and result.is_file() and result.parent.resolve() == bundle.resolve()
                    and all((bundle/name).is_file() for name in required)
                    and len(list((bundle/'shp').glob('*.shp'))) == 4):
                manifest = json.loads((bundle/'nc2web/manifest.json').read_text(encoding='utf-8'))
                index = json.loads((bundle/'stepjson/index.json').read_text(encoding='utf-8'))
                if (manifest['status'] == index['status'] == 'pass'
                        and len(list((bundle/'nc2web').glob('frame_*.bin'))) == len(manifest['frames'])
                        and len(list((bundle/'stepjson').glob('step_*.json'))) == len(index['files'])):
                    candidates.append(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return max(candidates, key=lambda p:p.stat().st_mtime_ns).parent.resolve() if candidates else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='计算并自动输出 NC、调闸记录、SHP、nc2web、stepjson 和 viewer')
    parser.add_argument('--workflow', type=Path, default=Path('examples/case_001/workflow.yaml'), help='流程配置文件')
    parser.add_argument('--id', dest='run_id', help='本次运行目录名；默认生成时间戳')
    parser.add_argument('--out-dir', type=Path, help='覆盖流程配置中的输出父目录')
    parser.add_argument('--show-latest', action='store_true', help='只打印最新完整成功的运行目录，供查看脚本使用')
    args = parser.parse_args(argv)
    try:
        if args.show_latest:
            if args.out_dir:
                parent = args.out_dir
            elif args.workflow.is_file():
                config = load_workflow_config(args.workflow)
                parent = resolve_workflow_path(args.workflow.resolve(),config.output_directory)
            else:
                parent = Path('work/results')
            latest = find_latest_completed_bundle(parent)
            if latest is None:
                raise WorkflowError(f'no complete result bundle found under {parent}')
            print(latest)
            return 0
        run_workflow(args.workflow, run_id=args.run_id, output_directory=args.out_dir)
    except KeyboardInterrupt:
        print('[failed] calculation interrupted', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'[failed] {exc}', file=sys.stderr)
        return 1
    return 0
