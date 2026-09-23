#!/usr/bin/env python3
"""Synthetic PCM verification. Default: local fake backend + actual pipe readback.

Main-only --linux-owned-no-record creates owned virtual nodes, checks writer
attachment, injects a generated tone, and verifies cleanup. It NEVER records any
source. No physical input, default setter, loopback or captured PCM file is used.
Automatic host default-route selection is not observed by this script.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from omodachi_core.audio_virtual_input import VirtualMicrophoneSession,VirtualInputError,FRAME_BYTES


def tone_frames(count):
    return [b''.join(struct.pack('<h',int(6000*math.sin(2*math.pi*440*(frame*960+i)/48000)))
                    for i in range(960)) for frame in range(count)]


def synthetic_pipe(count):
    sys.path.insert(0,str(ROOT/'tests'))
    from audio_virtual_input_support import FakePulse
    backend=FakePulse();session=VirtualMicrophoneSession(runner=backend.runner,process_factory=backend.spawn)
    try:
        metadata=session.begin(generation=1);frames=tone_frames(count)
        accepted=[session.accept(frame) for frame in frames]
        if not all(accepted):raise RuntimeError('synthetic frame unexpectedly dropped')
        readback=backend.processes[0].read_exact(count*FRAME_BYTES,timeout=1)
        expected=b''.join(frames)
        if readback!=expected:raise RuntimeError('synthetic pipe readback mismatch')
        ended=session.end(generation=1,reason='synthetic_validation')
        if not ended['cleanup_complete']:raise RuntimeError('synthetic cleanup failed')
        return {'mode':'synthetic_pipe','frames_accepted':len(frames),'pipe_bytes_read':len(readback),
                'pipe_readback_matches':True,'generated_sha256':hashlib.sha256(expected).hexdigest(),
                'readback_sha256':hashlib.sha256(readback).hexdigest(),'downstream_audio_readback':False,
                'owned_nodes_metadata_verified':metadata['ownership_verified'],'cleanup':ended,
                'recording_started':False,'physical_input_opened':False,'explicit_default_setter_called':False,'default_route_change_verified':False}
    finally:
        if session.metadata and not session.closed:session.end(generation=1,reason='synthetic_cleanup')
        backend.close()


def writer_attachment(pid,sink_index):
    # Metadata only. Keep no unrelated application properties in the result.
    result=subprocess.run(['pactl','--format=json','list','sink-inputs'],stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=2,check=False)
    if result.returncode or len(result.stdout)>2*1024*1024:raise RuntimeError('writer metadata unavailable')
    rows=json.loads(result.stdout)
    if not isinstance(rows,list):raise RuntimeError('writer metadata invalid')
    ours=[row for row in rows if isinstance(row,dict) and str(row.get('properties',{}).get('application.process.id',''))==str(pid)]
    if not ours:return None
    if len(ours)!=1:raise RuntimeError('writer stream ambiguous')
    row=ours[0];props=row.get('properties',{})
    if str(row.get('sink'))!=str(sink_index):raise RuntimeError('writer not attached to owned sink')
    # Require the two explicit PipeWire no-fallback/no-reconnect properties to
    # be visible before any tone bytes are written. Absence fails closed.
    if any(str(props.get(key,'')).lower()!='true' for key in ('node.dont-fallback','node.dont-reconnect')):
        raise RuntimeError('writer target protection not observed; no PCM injected')
    return {'stream_index':row.get('index'),'sink_index':sink_index,'writer_pid':pid,
            'explicit_owned_target_verified':True,'no_fallback_properties_observed':True}


def linux_no_record(count,confirmed):
    if not confirmed:raise RuntimeError('--confirm-owned-synthetic is required')
    if not sys.platform.startswith('linux') or os.getuid()==0 or os.getuid()!=os.geteuid():
        raise RuntimeError('normal Linux user session required')
    if not shutil.which('pactl') or not shutil.which('pacat'):raise RuntimeError('pactl and pacat required')
    session=VirtualMicrophoneSession();report={'mode':'linux_owned_no_record','recording_started':False,
        'physical_input_opened':False,'explicit_default_setter_called':False,'default_route_change_verified':False,'downstream_audio_readback':False}
    failure=None
    try:
        metadata=session.begin(generation=1);report['owned_nodes']=metadata
        pid=session.writer.process.pid
        deadline=time.monotonic()+2;attached=None
        while time.monotonic()<deadline:
            if session.writer.process.poll() is not None:raise RuntimeError('writer exited before injection')
            attached=writer_attachment(pid,metadata['sink_index'])
            if attached:break
            time.sleep(.02)
        if not attached:raise RuntimeError('writer did not attach; no PCM injected')
        report['writer_attachment']=attached
        accepted=[]
        for frame in tone_frames(count):
            session.verify_owned()
            if writer_attachment(pid,metadata['sink_index']) is None:
                raise RuntimeError('writer stream disappeared before injection')
            accepted.append(session.accept(frame));time.sleep(.02)
        if not all(accepted):raise RuntimeError('synthetic writer backpressure')
        deadline=time.monotonic()+1
        while session.stats().get('written_frames',0)<count and time.monotonic()<deadline:
            if session.stats().get('writer_failed'):raise RuntimeError('writer failed')
            time.sleep(.005)
        report['frames_accepted']=sum(accepted);report['writer_stats']=session.stats()
        if report['writer_stats'].get('written_frames')!=count:raise RuntimeError('writer did not accept all bytes')
        report['writer_attachment_after']=writer_attachment(pid,metadata['sink_index'])
        if report['writer_attachment_after'] is None:raise RuntimeError('writer stream disappeared after injection')
        report['result']='owned_nodes_created_and_synthetic_pcm_written'
    except Exception as error:
        failure=error
        report['error']=error.code if isinstance(error,VirtualInputError) else str(error)
    finally:
        if session.metadata:
            ended=None
            for _ in range(3):
                ended=session.end(generation=1,reason='synthetic_probe_end')
                if ended['cleanup_complete']:break
                time.sleep(.05)
            report['cleanup']=ended
            if not ended['cleanup_complete']:report['result']='cleanup_pending'
    print(json.dumps(report,indent=2))
    return 1 if failure or report.get('cleanup',{}).get('cleanup_complete') is not True else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frames',type=int,choices=(1,2,3),default=3)
    parser.add_argument('--linux-owned-no-record',action='store_true')
    parser.add_argument('--confirm-owned-synthetic',action='store_true')
    args=parser.parse_args()
    if args.linux_owned_no_record:return linux_no_record(args.frames,args.confirm_owned_synthetic)
    print(json.dumps(synthetic_pipe(args.frames),indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
