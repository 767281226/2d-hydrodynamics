"""Compact native-triangle web codec with exact string face identity."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from typing import Any,Iterable

import numpy as np

from .native_result_stream import NativeResultFrame,ValidatedNativeResultFrames


MESH_MAGIC=b"NTW3M01\n"
FRAME_MAGIC=b"NTW3F01\n"
FORMAT="native-triangle-web"
VERSION="1.0"
VARIABLES=("water_depth_m","water_level_m","velocity_x_m_s","velocity_y_m_s","speed_m_s")


class NativeTriangleWebError(ValueError):
    pass


def _sha_file(path:Path)->tuple[str,int]:
    digest=hashlib.sha256();size=0
    with path.open("rb",buffering=0) as stream:
        while True:
            raw=stream.read(1024*1024)
            if not raw:break
            digest.update(raw);size+=len(raw)
    return digest.hexdigest(),size


class NativeTriangleWebWriter:
    def __init__(self,output_dir:str|Path,source:ValidatedNativeResultFrames,*,run_id:str):
        if type(source) is not ValidatedNativeResultFrames:raise NativeTriangleWebError("exact validated native frame source required")
        self.output=Path(output_dir).resolve();self.source=source;self.run_id=run_id
        if self.output.exists() or not self.output.parent.is_dir():
            raise NativeTriangleWebError("native_web output target must be a new directory with an existing parent")
        self.output.mkdir()
        self.frames=[];self._next=0;self._closed=False
        self.extrema={}
        self.mesh_path=self.output/"mesh.bin"
        self._write_mesh()

    def _write_mesh(self)->None:
        node_count=len(self.source.node_xy_m);face_count=len(self.source.face_id)
        if node_count>0xffffffff or face_count>0xffffffff:raise NativeTriangleWebError("native mesh exceeds uint32 topology count")
        temporary=self.mesh_path.with_name(".mesh.bin.tmp")
        try:
            with temporary.open("xb") as out:
                out.write(MESH_MAGIC);out.write(struct.pack("<II",node_count,face_count))
                xy=np.asarray(self.source.node_xy_m,dtype="<f8")
                faces=np.asarray(self.source.face_nodes,dtype="<u4")
                areas=np.asarray(self.source.face_area_m2,dtype="<f8")
                out.write(xy.tobytes(order="C"));out.write(faces.tobytes(order="C"))
                for face_id in self.source.face_id:
                    raw=str(int(face_id)).encode("ascii")
                    if not raw or len(raw)>32:raise NativeTriangleWebError("native face ID cannot be represented exactly in the fixed 32-byte string table")
                    out.write(raw.ljust(32,b" "))
                out.write(areas.tobytes(order="C"));out.flush();os.fsync(out.fileno())
            os.link(temporary,self.mesh_path);temporary.unlink()
        except BaseException:
            temporary.unlink(missing_ok=True);raise
        self.mesh_sha256,self.mesh_bytes=_sha_file(self.mesh_path)

    def write_frame(self,frame:NativeResultFrame)->dict[str,Any]:
        if self._closed or frame.index!=self._next or frame.index>=len(self.source.time_s):
            raise NativeTriangleWebError("native frame order is not contiguous on the validated saved-time axis")
        count=len(self.source.face_id)
        rows=(frame.water_depth_m,frame.velocity_x_m_s,frame.velocity_y_m_s,frame.speed_m_s)
        if any(row.shape!=(count,) or not np.isfinite(row).all() for row in rows):
            raise NativeTriangleWebError(f"native web frame {frame.index} has invalid face values")
        if self.source.vertical_datum is None:
            levels=np.full(count,np.nan,dtype=np.float64)
        else:
            levels=np.asarray(self.source.bed_elevation_m,dtype=np.float64)+frame.water_depth_m
        arrays=(frame.water_depth_m,levels,frame.velocity_x_m_s,frame.velocity_y_m_s,frame.speed_m_s)
        for name,array in zip(VARIABLES,arrays):
            finite=np.asarray(array,dtype=np.float64);finite=finite[np.isfinite(finite)]
            if finite.size:
                bounds=self.extrema.setdefault(name,{"min":float(finite.min()),"max":float(finite.max())})
                bounds["min"]=min(bounds["min"],float(finite.min()));bounds["max"]=max(bounds["max"],float(finite.max()))
        name=f"frame_{frame.index:06d}.bin";path=self.output/name;temporary=self.output/("."+name+".tmp")
        try:
            with temporary.open("xb") as out:
                out.write(FRAME_MAGIC);out.write(struct.pack("<IdIII",frame.index,frame.time_s,count,len(arrays),0))
                for array in arrays:out.write(np.asarray(array,dtype="<f8").tobytes(order="C"))
                out.flush();os.fsync(out.fileno())
            os.link(temporary,path);temporary.unlink()
        except BaseException:
            temporary.unlink(missing_ok=True);raise
        digest,size=_sha_file(path)
        record={"index":frame.index,"time_s":frame.time_s,"file":name,"sha256":digest,"bytes":size}
        self.frames.append(record);self._next+=1;return record

    def finish(self)->dict[str,Any]:
        if self._closed or self._next!=len(self.source.time_s):
            raise NativeTriangleWebError("native web output must contain every exact native saved frame")
        body={
            "format":FORMAT,"version":VERSION,"status":"qa_fixture_ready","run_id":self.run_id,
            "data_origin":"SYNTHETIC_SMALL_NATIVE_FIXTURE","qa_fixture":True,"live_solver_ran":False,
            "topology":"native_triangular_mesh","source_profile":"authoritative_native_faces",
            "source_native_result_path":str(self.source.native_result_path),
            "source_native_result_sha256":self.source.native_result_sha256,
            "source_request_path":str(self.source.request_path),"source_graph":dict(self.source.source_hashes),
            "coordinate_system":self.source.coordinate_system,"vertical_datum":self.source.vertical_datum,
            "time_origin":self.source.time_origin,"times_s":list(self.source.time_s),
            "dry_depth_threshold_m":self.source.dry_depth_threshold_m,
            "node_count":len(self.source.node_xy_m),"face_count":len(self.source.face_id),
            "face_ids_encoding":"decimal ASCII strings, fixed 32-byte records; never parsed as JavaScript Number",
            "face_id_sha256":hashlib.sha256("\n".join(str(x) for x in self.source.face_id).encode("ascii")).hexdigest(),
            "mesh_file":{"name":"mesh.bin","sha256":self.mesh_sha256,"bytes":self.mesh_bytes,
                "layout":"magic[8], uint32 node_count, uint32 face_count, node_xy float64 interleaved, face_nodes uint32 triples, face_id 32-byte ASCII strings, face_area float64"},
            "variables":[{"name":name,"dtype":"float64-le","units":units,"availability":"available" if name!="water_level_m" or self.source.vertical_datum else "not_available_datum_unspecified"}
                for name,units in zip(VARIABLES,("m","m","m/s","m/s","m/s"))],
            "extrema":self.extrema,
            "frame_magic":FRAME_MAGIC.decode("ascii"),"frame_header":"uint32 index, float64 time_s, uint32 face_count, uint32 variable_count, uint32 reserved=0; 32-byte total header, then each manifest variable as face-order float64-le",
            "frames":self.frames,"wet_area_rule":f"sum(native face_area_m2 where depth > {self.source.dry_depth_threshold_m:g} m",
            "security_exclusion":self.source.security_exclusion,
            "database_status":"DISABLED_NOT_REQUESTED","social_economy_status":"NOT_READ_NOT_REQUESTED",
        }
        path=self.output/"manifest.json";temporary=self.output/".manifest.json.tmp"
        raw=(json.dumps(body,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n").encode("utf-8")
        try:
            with temporary.open("xb") as out:out.write(raw);out.flush();os.fsync(out.fileno())
            os.link(temporary,path);temporary.unlink()
        except BaseException:
            temporary.unlink(missing_ok=True);raise
        self._closed=True
        return {"path":str(path),"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw),
            "frame_count":len(self.frames),"face_count":len(self.source.face_id),"status":body["status"]}
