"""Validated local server and browser launcher for complete output bundles."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import math
import os
from pathlib import Path
import socket
import sys
from urllib.parse import urlencode
import webbrowser


class ViewerBundleError(ValueError):pass


def _strict_json(path:Path,max_bytes:int=4*1024*1024)->dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size>max_bytes:
        raise ViewerBundleError(f"required bundle metadata is missing, symlinked, or over its cap: {path}")
    pairs=[]
    def unique(items):
        out={}
        for key,value in items:
            if key in out:raise ViewerBundleError(f"duplicate JSON key in {path}")
            out[key]=value
        return out
    try:
        value=json.loads(path.read_text(encoding="utf-8"),object_pairs_hook=unique,
            parse_constant=lambda token:(_ for _ in ()).throw(ValueError(token)))
    except Exception as exc:raise ViewerBundleError(f"invalid strict JSON {path}: {exc}") from exc
    if type(value) is not dict:raise ViewerBundleError(f"metadata root must be an object: {path}")
    return value


def _safe_child(root:Path,relative:str)->Path:
    if type(relative) is not str or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ViewerBundleError(f"bundle artifact path is not relative and normalized: {relative!r}")
    candidate=root/relative
    try:
        from .triangular_shp_export import _is_reparse_component
        if _is_reparse_component(candidate):raise ViewerBundleError(f"bundle path crosses a symlink/reparse point: {relative}")
    except ImportError:pass
    path=candidate.resolve()
    if not path.is_relative_to(root.resolve()) or path.is_symlink() or not path.is_file():
        raise ViewerBundleError(f"bundle artifact is missing or escaped its root: {relative}")
    return path


def _legacy_workflow_bootstrap(root:Path,metadata:dict,report:dict,*,explicit:bool)->dict:
    """Normalize the actual pre-M1 workflow metadata without certifying it."""
    required=("id","script_id","out_dir","bundle_dir","engine_mode","grid_topology","shp_status")
    if any(not isinstance(metadata.get(key),str) or not metadata[key].strip() for key in required):
        raise ViewerBundleError("legacy workflow run.json is missing its historical identity/output fields")
    if metadata["id"]!=metadata["script_id"] or metadata["engine_mode"]!="hourly_inundation" or metadata["grid_topology"]!="structured_grid":
        raise ViewerBundleError("legacy workflow run.json identity/engine/topology is inconsistent")
    if not explicit:raise ViewerBundleError("legacy workflow bundles require an explicit path; run.json does not carry production approval")
    if (report.get("format")!="hydrodynamics-workflow-run-report-v1" or report.get("schema_version")!="1.1" or
            report.get("status")!="complete" or report.get("id")!=metadata["id"] or
            report.get("script_id")!=metadata["script_id"] or report.get("bundle_dir")!=metadata["bundle_dir"] or
            report.get("engine_mode")!=metadata["engine_mode"] or report.get("grid_topology")!=metadata["grid_topology"]):
        raise ViewerBundleError("legacy workflow report is missing, failed, mixed-version, or disagrees with run.json")
    stages=report.get("workflow")
    if (not isinstance(stages,dict) or any(not isinstance(stages.get(name),dict) or stages[name].get("status")!="complete"
            for name in ("nc2web","stepjson","viewer","validate"))):
        raise ViewerBundleError("legacy workflow report does not show successful nc2web/stepjson/viewer/validate stages")
    if stages.get("shp",{}).get("status")!="not_applicable":
        raise ViewerBundleError("legacy structured workflow SHP status is not the historical not_applicable state")
    compat_path=root/"nc2web"/"manifest.json"
    compat=_strict_json(compat_path)
    if (compat.get("format")!="nc2web" or compat.get("version")!="1.0" or compat.get("status")!="pass" or
            not isinstance(compat.get("frames"),list) or not compat["frames"] or
            not {"water_depth","velocity_x","velocity_y"}.issubset(compat.get("variables",[]))):
        raise ViewerBundleError("legacy structured nc2web v1 manifest is incomplete or blocked")
    _safe_child(compat_path.parent,"mesh.bin")
    for frame in compat["frames"]:_safe_child(compat_path.parent,frame.get("file",""))
    raw_threshold=report.get("dry_depth_threshold_m")
    has_display_threshold=(not isinstance(raw_threshold,bool) and isinstance(raw_threshold,(int,float))
        and math.isfinite(float(raw_threshold)) and float(raw_threshold)>=0)
    dry_threshold=float(raw_threshold) if has_display_threshold else 0.0
    return {"format":"legacy-structured-viewer-bootstrap-v1","run_id":metadata["script_id"],
        "case_id":metadata["id"],"status":"LEGACY_REPORTED_COMPLETE","legacy_structured":True,
        "normalized_from_legacy_workflow":True,"time_origin":report.get("time_origin"),
        "native_result":{"dry_depth_threshold_m":dry_threshold,"threshold_is_display_fallback":not has_display_threshold},
        "compatibility":{"grid_id_digest":hashlib.sha256(json.dumps(compat,sort_keys=True,separators=(",",":")).encode()).hexdigest()},
        "viewer":{"default_profile":"compatibility_grid","profiles":{"compatibility_grid":"nc2web/manifest.json"}}}


def validate_bundle(root_value:str|Path,*,explicit:bool)->tuple[Path,dict]:
    root=Path(root_value).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():raise ViewerBundleError(f"bundle directory is missing or symlinked: {root}")
    viewer=root/"viewer.html";run_path=root/"run.json"
    if not viewer.is_file() or viewer.is_symlink():raise ViewerBundleError("bundle requires viewer.html")
    legacy=False
    if run_path.is_symlink():raise ViewerBundleError("run.json must not be a symlink")
    if run_path.is_file():
        run=_strict_json(run_path)
        if any(key in run for key in ("id","script_id","out_dir","bundle_dir","engine_mode","grid_topology")):
            legacy_report=_strict_json(root/"run_report.json")
            run=_legacy_workflow_bootstrap(root,run,legacy_report,explicit=explicit)
            legacy=True
    else:
        if run_path.exists():raise ViewerBundleError("run.json is not a regular file")
        report=_strict_json(root/"run_report.json")
        compat_path=root/"nc2web"/"manifest.json"
        legacy_qa=report.get("qa_fixture") is True and report.get("data_origin")=="SYNTHETIC_SMALL_NATIVE_FIXTURE"
        if ((legacy_qa and (not explicit or report.get("complete") is not False or report.get("can_publish") is not False)) or
                (not legacy_qa and report.get("status") not in ("complete","completed","success")) or not compat_path.is_file()):
            raise ViewerBundleError("legacy structured bundle requires a successful report, or an explicitly named QA fixture, and nc2web v1 artifacts")
        compat=_strict_json(compat_path)
        if compat.get("format")!="nc2web" or compat.get("version")!="1.0" or compat.get("status")=="block" or not compat.get("frames"):
            raise ViewerBundleError("legacy structured nc2web v1 manifest is incomplete or blocked")
        _safe_child(compat_path.parent,"mesh.bin")
        for frame in compat["frames"]:_safe_child(compat_path.parent,frame.get("file",""))
        legacy=True
        run={"format":"legacy-structured-viewer-bootstrap-v1","run_id":report.get("run_id",root.name),
            "status":"QA_FIXTURE_READY_NOT_PRODUCTION" if legacy_qa else "legacy_complete",
            "data_origin":"SYNTHETIC_SMALL_NATIVE_FIXTURE" if legacy_qa else "LEGACY_STRUCTURED_SUCCESS",
            "qa_fixture":legacy_qa,"legacy_structured":True,"production_approved":False if legacy_qa else True,
            "time_origin":report.get("time_origin"),"native_result":{"dry_depth_threshold_m":0.0},
            "viewer":{"default_profile":"compatibility_grid","profiles":{"compatibility_grid":"nc2web/manifest.json"}},
            "compatibility":{"grid_id_digest":hashlib.sha256(json.dumps(compat,sort_keys=True,separators=(",",":")).encode()).hexdigest()}}
    qa=run.get("qa_fixture") is True and run.get("data_origin")=="SYNTHETIC_SMALL_NATIVE_FIXTURE"
    report_path=root/"run_report.json"
    report=_strict_json(report_path) if report_path.is_file() else {}
    if qa:
        if (not explicit or run.get("status")!="QA_FIXTURE_READY_NOT_PRODUCTION" or run.get("production_approved") is not False or
                report.get("status")!="qa_fixture" or report.get("complete") is not False or report.get("can_publish") is not False):
            raise ViewerBundleError("QA fixtures may be opened only by an explicit directory argument and must remain non-production")
        if run.get("format")=="m1-output-bundle-v1":
            checks=_strict_json(root/"checksums.json")
            rows=checks.get("files")
            if not isinstance(rows,list) or checks.get("file_count")!=len(rows):
                raise ViewerBundleError("M1 QA bundle checksums.json is incomplete")
            expected=set()
            for row in rows:
                if not isinstance(row,dict):raise ViewerBundleError("M1 checksums row is malformed")
                path=_safe_child(root,row.get("path",""));expected.add(path.relative_to(root).as_posix())
                if path.stat().st_size!=row.get("bytes") or _sha_file(path)!=row.get("sha256"):
                    raise ViewerBundleError(f"M1 QA bundle artifact hash/size differs: {row.get('path')}")
            actual={path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and path.name!="checksums.json"}
            if actual!=expected:raise ViewerBundleError("M1 QA bundle file list differs from its no-clobber checksums manifest")
    elif not legacy:
        if (run.get("status") not in ("complete","success") or run.get("production_approved") is not True or
                report.get("status") not in ("complete","success") or report.get("can_publish") is not True):
            raise ViewerBundleError("latest/viewer selection requires a complete success report with can_publish=true")
    profiles=run.get("viewer",{}).get("profiles",{})
    if not isinstance(profiles,dict) or not profiles:
        raise ViewerBundleError("run.json must declare explicit viewer profiles")
    if "native_web" in profiles:
        manifest_path=_safe_child(root,profiles["native_web"])
        manifest=_strict_json(manifest_path)
        if (manifest.get("format")!="native-triangle-web" or manifest.get("version")!="1.0" or
                manifest.get("run_id")!=run.get("run_id") or manifest.get("face_count",0)<=0):
            raise ViewerBundleError("native web manifest identity/version/topology is incomplete")
        mesh=manifest.get("mesh_file",{})
        mesh_path=_safe_child(manifest_path.parent,mesh.get("name",""))
        if mesh_path.stat().st_size!=mesh.get("bytes") or _sha_file(mesh_path)!=mesh.get("sha256"):
            raise ViewerBundleError("native web mesh hash/length differs from manifest")
        frames=manifest.get("frames")
        if not isinstance(frames,list) or len(frames)!=len(manifest.get("times_s",[])) or not frames:
            raise ViewerBundleError("native web saved frame list/time axis is incomplete")
        for index,frame in enumerate(frames):
            if frame.get("index")!=index or not isinstance(frame.get("time_s"),(int,float)):
                raise ViewerBundleError("native web frame order/time is malformed")
            frame_path=_safe_child(manifest_path.parent,frame.get("file",""))
            if frame_path.stat().st_size!=frame.get("bytes"):
                raise ViewerBundleError("native web frame byte count differs from manifest")
    if "compatibility_grid" in profiles:
        compat_path=_safe_child(root,profiles["compatibility_grid"])
        compat=_strict_json(compat_path)
        if compat.get("format")!="nc2web" or compat.get("version")!="1.0":
            raise ViewerBundleError("derived compatibility profile is not the versioned nc2web 1.0 contract")
        for frame in compat.get("frames",[]):
            _safe_child(compat_path.parent,frame.get("file",""))
        if not legacy:
            grid=_strict_json(_safe_child(root,"compatibility_grid.json"))
            frame_hash_rows=grid.get("nc2web_frame_files")
            if (grid.get("format")!="hydrodynamics-legacy-grid-compatibility-v1" or
                    grid.get("run_id")!=run.get("run_id") or
                    grid.get("grid_id_digest")!=run.get("compatibility",{}).get("grid_id_digest") or
                    not isinstance(frame_hash_rows,list) or len(frame_hash_rows)!=len(compat.get("frames",[]))):
                raise ViewerBundleError("M1 compatibility grid/source identity and frame hash index are inconsistent")
    return root,run


def _sha_file(path:Path)->str:
    digest=hashlib.sha256()
    with path.open("rb",buffering=0) as stream:
        while True:
            chunk=stream.read(1024*1024)
            if not chunk:break
            digest.update(chunk)
    return digest.hexdigest()


def _project_root()->Path:return Path(__file__).resolve().parents[1]


def _latest_success(project:Path)->Path:
    from .workflow import find_latest_completed_bundle
    latest=find_latest_completed_bundle(project/"work"/"results")
    if latest is None:raise ViewerBundleError("no complete successful viewer bundle is available under work/results")
    return latest.resolve()


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self,format,*args):sys.stdout.write("[http] "+(format%args)+"\n");sys.stdout.flush()
    def end_headers(self):
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        super().end_headers()


class _ExclusiveThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address=False
    allow_reuse_port=False
    daemon_threads=True

    def server_bind(self):
        exclusive=getattr(socket,"SO_EXCLUSIVEADDRUSE",None)
        if exclusive is not None:self.socket.setsockopt(socket.SOL_SOCKET,exclusive,1)
        self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,0)
        reuse_port=getattr(socket,"SO_REUSEPORT",None)
        if reuse_port is not None:
            try:self.socket.setsockopt(socket.SOL_SOCKET,reuse_port,0)
            except OSError:pass
        super().server_bind()


def serve(bundle_root:Path,*,run_metadata:dict|None=None,start_port:int=8788,open_browser:bool=True)->int:
    class BundleHandler(_Handler):
        def do_GET(self):
            normalized_legacy=bool(run_metadata and run_metadata.get("normalized_from_legacy_workflow"))
            if (self.path.split("?",1)[0]=="/run.json" and run_metadata is not None and
                    (normalized_legacy or not (bundle_root/"run.json").is_file())):
                raw=(json.dumps(run_metadata,ensure_ascii=False,separators=(",",":"))+"\n").encode("utf-8")
                self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw);return
            super().do_GET()
    handler=lambda *args,**kwargs:BundleHandler(*args,directory=str(bundle_root),**kwargs)
    server=None;port=None
    for candidate in range(start_port,start_port+20):
        try:
            server=_ExclusiveThreadingHTTPServer(("127.0.0.1",candidate),handler);port=candidate;break
        except OSError:
            continue
    if server is None:raise ViewerBundleError(f"no available loopback port in {start_port}..{start_port+19}; existing listeners were not touched")
    run_id=str((run_metadata or {}).get("run_id",""))
    url=f"http://127.0.0.1:{port}/viewer.html?{urlencode({'expected_run_id':run_id})}"
    print(f"VIEWER_RUN_ID={run_id}",flush=True)
    print(f"VIEWER_URL={url}",flush=True)
    if open_browser:
        opened=webbrowser.open(url,new=2,autoraise=True)
        print(f"BROWSER_OPEN_REQUESTED={bool(opened)}",flush=True)
    try:server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return 0


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(prog="start_viewer")
    parser.add_argument("target",nargs="?",help="explicit complete run directory or QA fixture directory")
    parser.add_argument("--project-root",type=Path,default=_project_root())
    parser.add_argument("--port",type=int,default=8788)
    parser.add_argument("--no-browser",action="store_true")
    args=parser.parse_args(argv)
    try:
        explicit=bool(args.target)
        if explicit:
            candidate=Path(args.target).expanduser()
            if not candidate.is_absolute():
                if candidate.exists():pass
                elif (args.project_root/"work"/"results"/candidate).exists():candidate=args.project_root/"work"/"results"/candidate
                else:candidate=args.project_root/candidate
            root,run=validate_bundle(candidate,explicit=True)
        else:
            root,run=validate_bundle(_latest_success(args.project_root),explicit=False)
        print(f"VIEWER_BUNDLE={root}",flush=True)
        if run.get("qa_fixture") is True:print("VIEWER_NOTICE=explicit QA fixture; not a production result and never selected as latest",flush=True)
        return serve(root,run_metadata=run,start_port=args.port,open_browser=not args.no_browser)
    except Exception as exc:
        print(f"[viewer error] {type(exc).__name__}: {exc}",file=sys.stderr,flush=True)
        return 1


if __name__=="__main__":raise SystemExit(main())
