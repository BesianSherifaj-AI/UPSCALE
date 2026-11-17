"""Benchmark the real video pipeline and record NVIDIA + Python CPU telemetry."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backend


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reference', default=str(ROOT / 'tests' / 'user-clip-result.json'))
    parser.add_argument('--order', choices=['interpolate_first', 'upscale_first'], default='interpolate_first')
    parser.add_argument('--label', default='fast')
    parser.add_argument('--fps')
    parser.add_argument('--parallel', choices=['on', 'off'], default='on')
    parser.add_argument('--cv-threads', type=int)
    args = parser.parse_args()
    reference = json.loads(Path(args.reference).read_text(encoding='utf-8'))
    reference = reference.get('result', reference)
    source = Path(reference['input_path'])
    options = dict(reference['settings'])
    options['processing_order'] = args.order
    options['parallel_processing'] = args.parallel == 'on'
    if args.cv_threads is not None:
        import cv2
        cv2.setNumThreads(args.cv_threads)
    if args.fps:
        options['target_fps'] = args.fps
    configuration = backend.Settings(**options)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    stop = threading.Event()
    samples = []
    progress_state = {'value': 0, 'message': 'Starting'}
    last_progress = 0
    started = time.perf_counter()
    cpu_started = time.process_time()

    def sample_gpu():
        fields = ['utilization.gpu', 'utilization.encoder', 'utilization.decoder', 'memory.used', 'memory.total', 'power.draw', 'clocks.current.graphics']
        while not stop.is_set():
            try:
                reply = subprocess.run(['nvidia-smi', '--query-gpu=' + ','.join(fields), '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                values = reply.stdout.splitlines()[0].split(',')
                data = {key: float(value.strip()) if value.strip().replace('.', '', 1).isdigit() else None for key, value in zip(fields, values)}
                samples.append({'seconds': time.perf_counter()-started, 'stage': progress_state['message'], **data})
            except Exception:
                pass
            stop.wait(1)

    def progress(value, message):
        nonlocal last_progress
        progress_state.update(value=value, message=message)
        if time.perf_counter()-last_progress > 5 or value in (0, 1):
            last_progress=time.perf_counter()
            print(f'{value:.1%} {message}', flush=True)

    monitor=threading.Thread(target=sample_gpu, daemon=True)
    monitor.start()
    try:
        result = backend.process_video(source, configuration, ROOT / 'tests' / 'benchmark-outputs', progress)
    finally:
        stop.set()
        monitor.join(timeout=6)
    cpu_seconds=time.process_time()-cpu_started
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert result['engine_reports']['interpolation']['generated_frames'] > 0
    assert result['frames'] == result['engine_reports']['interpolation']['output_frames']
    metrics = {}
    for key in ['utilization.gpu', 'utilization.encoder', 'utilization.decoder', 'memory.used', 'power.draw']:
        values = [sample[key] for sample in samples if sample.get(key) is not None]
        if values:
            metrics[key] = {'mean': statistics.mean(values), 'peak': max(values)}
    summary = {'label':args.label, 'reference_elapsed_seconds':reference['elapsed_seconds'], 'elapsed_seconds':result['elapsed_seconds'],
               'speedup': reference['elapsed_seconds']/result['elapsed_seconds'], 'stage_timings': result['stage_timings'],
               'output_path':result['output_path'], 'width':result['width'], 'height':result['height'], 'fps':result['fps'], 'frames':result['frames'],
               'python_cpu_seconds':cpu_seconds, 'python_cpu_average_cores':cpu_seconds/result['elapsed_seconds'],
               'python_cpu_percent_of_machine':100*cpu_seconds/result['elapsed_seconds']/(os.cpu_count() or 1), 'gpu_metrics':metrics}
    artifact = ROOT / 'tests' / ('benchmark-' + args.label + '.json')
    artifact.write_text(json.dumps({'summary':summary, 'source_sha256':original_hash, 'result':result, 'samples':samples},indent=2,default=str), encoding='utf-8')
    print(json.dumps(summary,indent=2), flush=True)


if __name__ == '__main__':
    run()
