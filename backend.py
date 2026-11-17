"""Standalone, cancellable adapter for the bundled upstream DLSS video engine.

Only ``dlss_engine`` is imported; ComfyUI and PyTorch are not required. Native
processing is delegated to the repository, and completed outputs are published
only after its reports and the final video have been checked.
"""
from __future__ import annotations

import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from types import SimpleNamespace
from typing import Callable
from uuid import uuid4

APP_DIR = Path(__file__).resolve().parent
ENGINE_DIR = APP_DIR / "engine"
SCALE_CHOICES = (("1x · Native / DLAA", 1.0), ("1.5x · Quality", 1.5),
                 ("1.724x · Balanced", 1.724), ("2x · Performance", 2.0),
                 ("3x · Ultra Performance", 3.0))
STYLE_CHOICES = ("Default", "Natural", "Cinematic")
QUALITY_CHOICES = ("Auto (Default)", "Max", "Best", "Good")
CODEC_CHOICES = ("H.264 (NVIDIA NVENC)", "H.265 (NVIDIA NVENC)",
                 "AV1 (NVIDIA NVENC)", "H.264", "H.265", "AV1", "ProRes Proxy")
CONTAINER_CHOICES = ("MP4", "MKV", "MOV")
FPS_CHOICES = ("23.976", "25", "29.97", "30", "50", "59.94", "60", "90", "120")
INTERPOLATION_CHOICES = ("Auto", "Native DLSSG", "Cascade")
PROCESSING_CHOICES = (("Fast · interpolate, then upscale", "interpolate_first"),
                      ("Detail · upscale, then interpolate", "upscale_first"))
Progress = Callable[[float, str], None]


@dataclass(frozen=True)
class Settings:
    scale: float = 2.0
    style: str = "Natural"
    intensity: float = 1.0
    quality: str = "Best"
    codec: str = "H.264 (NVIDIA NVENC)"
    container: str = "MP4"
    preserve_hdr: bool = False
    target_fps: str = "60"
    interpolation_engine: str = "Auto"
    require_neural_upscaling: bool = False
    processing_order: str = "interpolate_first"
    parallel_processing: bool = True


class Cancelled(RuntimeError):
    """Raised when the user stops the current conversion."""


_RENDER_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_active_cancel: threading.Event | None = None


@lru_cache(maxsize=1)
def _engine() -> SimpleNamespace:
    if not (ENGINE_DIR / "dlss_engine" / "__init__.py").is_file():
        raise RuntimeError("The DLSS engine is missing. Run Setup.ps1 to restore it.")
    # Explicit environment settings always take priority over bundled tools.
    for name in ("ffmpeg", "ffprobe"):
        key = "DLSS_" + name.upper() + "_PATH"
        if not os.environ.get(key):
            candidates = (APP_DIR / "tools" / "ffmpeg" / "bin" / f"{name}.exe",
                          APP_DIR / "tools" / "ffmpeg" / f"{name}.exe",
                          APP_DIR / "tools" / "bin" / f"{name}.exe")
            for candidate in candidates:
                if candidate.is_file():
                    os.environ[key] = str(candidate)
                    break
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))
    try:
        def module(name: str):
            return importlib.import_module("dlss_engine." + name)
        return SimpleNamespace(
            ffmpeg=module("core.ffmpeg"), paths=module("core.paths"),
            runtime=module("core.runtime"), jobs=module("core.jobs"),
            video=module("video.processor"), models=module("video.models"),
            interpolation=module("frame_interpolation.processor"),
            interpolation_models=module("frame_interpolation.models"),
            gpu=module("core.gpu_detection"),
        )
    except ImportError as exc:
        raise RuntimeError(f"A required Python package is unavailable: {exc}. "
                           "Run Setup.ps1 to repair the installation.") from exc


def probe(path: str | os.PathLike) -> dict:
    """Fast metadata probe; never decodes an entire video just to populate the UI."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Video file does not exist: {source}")
    try:
        result = _engine().ffmpeg.probe_video(source, count_mode="metadata")
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(f"Cannot read {source.name}: {exc}") from exc
    return {**result, "path": str(source), "size_bytes": source.stat().st_size}


def output_size(width: int, height: int, scale: float) -> tuple[int, int]:
    return _engine().runtime.resolve_output_size(width, height, scale)


def validate_settings(settings: Settings, metadata: dict) -> tuple[int, int]:
    engine = _engine()
    if settings.container not in CONTAINER_CHOICES:
        raise ValueError("Choose MP4, MKV or MOV as the output container.")
    if settings.codec not in CODEC_CHOICES:
        raise ValueError("Choose a supported video codec.")
    if settings.quality not in QUALITY_CHOICES:
        raise ValueError("Choose a supported encoding quality.")
    if settings.style not in STYLE_CHOICES:
        raise ValueError("Choose Default, Natural or Cinematic as the style.")
    if not math.isfinite(settings.intensity) or not 0 <= settings.intensity <= 2:
        raise ValueError("Enhancement intensity must be between 0 and 2.")
    if settings.interpolation_engine not in INTERPOLATION_CHOICES:
        raise ValueError("Choose Auto, Native DLSSG or Cascade for interpolation.")
    if settings.processing_order not in {"interpolate_first", "upscale_first"}:
        raise ValueError("Choose Fast or Detail as the processing mode.")
    if not isinstance(settings.parallel_processing, bool):
        raise ValueError("Parallel processing must be enabled or disabled.")
    engine.ffmpeg.validate_codec_container(settings.codec, settings.container)
    if settings.preserve_hdr and not engine.ffmpeg.hdr_mode_supported(settings.codec):
        raise ValueError("Preserve HDR requires H.265, AV1 or ProRes Proxy. "
                         "Choose H.265 (NVIDIA NVENC) for GPU encoding.")
    if metadata.get("hdr") and not settings.preserve_hdr:
        raise ValueError("This is an HDR video. Enable Preserve HDR and use H.265, "
                         "AV1 or ProRes; this engine does not provide HDR-to-SDR tone mapping.")
    width, height = int(metadata["width"]), int(metadata["height"])
    if min(width, height) < 64:
        raise ValueError("Each input dimension must be at least 64 pixels.")
    size = output_size(width, height, settings.scale)
    if settings.require_neural_upscaling and float(settings.scale) == 1.0:
        raise ValueError("Require Neural Upscaling needs a scale greater than 1x.")
    fps = float(metadata["fps"])
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("The video has no usable frame rate.")
    if settings.target_fps not in FPS_CHOICES:
        raise ValueError("Frame interpolation is required. Choose a supported output FPS.")
    target = engine.interpolation_models.resolve_target_rate(settings.target_fps)
    source_rate = metadata.get("rate", fps)
    if target <= source_rate:
        raise ValueError("Interpolation FPS must be higher than the source FPS. "
                         "Choose a higher output frame rate.")
    if target > source_rate * 6:
        raise ValueError("Interpolation cannot exceed 6x the source frame rate.")
    return size


def environment_info() -> dict:
    """Check local installation without initializing a DLSS render session."""
    result = {"ready": False, "gpus": [], "ffmpeg": "", "ffprobe": "",
              "errors": [], "details": [], "hags_enabled": None}
    try:
        engine = _engine()
        result["ffmpeg"] = str(engine.paths.FFMPEG)
        result["ffprobe"] = str(engine.paths.FFPROBE)
        engine.runtime.validate_runtime_files()
        gpus = engine.gpu.detect_gpus()
        result["gpus"] = [dict(gpu) for gpu in gpus]
        if not any(gpu.get("ai_compatible") for gpu in gpus):
            raise RuntimeError("A compatible NVIDIA RTX GPU is required for this engine.")
        for name in ("FFMPEG", "FFPROBE"):
            completed = subprocess.run(
                [str(getattr(engine.paths, name)), "-version"], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode:
                raise RuntimeError(f"{name} cannot run: {completed.stderr.strip()}")
            result["details"].append(completed.stdout.splitlines()[0])
        if sys.platform == "win32":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers") as key:
                    value, _ = winreg.QueryValueEx(key, "HwSchMode")
                result["hags_enabled"] = value == 2
            except OSError:
                pass
        result["details"].append("DLSS feature support is checked by the NVIDIA runtime during processing.")
        result["ready"] = True
    except Exception as exc:
        result["errors"].append(str(exc))
    return result


def gpu_usage() -> dict:
    """Read whole-device activity; VRAM occupancy is not a speed percentage."""
    try:
        reply = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        values = reply.stdout.splitlines()[0].split(",")
        return dict(zip(("gpu_percent", "encoder_percent", "memory_used_mb", "memory_total_mb"),
                        (float(value.strip()) for value in values)))
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return {}


def cancel_processing() -> str:
    with _STATE_LOCK:
        event = _active_cancel
        if event is not None:
            event.set()
    if event is None:
        return "No video is being processed."
    _engine().jobs.cancel_active_job()
    return "Stopping processing; incomplete output will be removed."


def _read_report(path: str) -> dict:
    with Path(path).open("r", encoding="utf-8") as stream:
        result = json.load(stream)
    if not isinstance(result, dict) or result.get("status") != "success":
        raise RuntimeError(f"The engine did not report a successful conversion: {path}")
    return result


def process_video(path: str | os.PathLike, settings: Settings,
                  output_dir: str | os.PathLike, progress: Progress | None = None,
                  cancel_event: threading.Event | None = None) -> dict:
    """Upscale and interpolate one file, then save a permanent result.

    Call from a worker thread. Output names are unique, source files are never
    replaced, and a JSON report beside the result includes actual DLSS evidence.
    """
    global _active_cancel
    if not _RENDER_LOCK.acquire(blocking=False):
        raise RuntimeError("Another video is already being processed.")
    cancelled = cancel_event if cancel_event is not None else threading.Event()
    stopped = threading.Event()
    monitor = None
    engine = None
    final_path = None
    published = False
    started = time.perf_counter()
    with _STATE_LOCK:
        _active_cancel = cancelled

    def check_cancelled():
        if cancelled.is_set():
            raise Cancelled("Processing stopped. Your original video is unchanged.")

    def notify(value: float, message: str):
        check_cancelled()
        if progress:
            progress(max(0.0, min(1.0, float(value))), message)

    try:
        check_cancelled()
        engine = _engine()

        def cancellation_monitor():
            while not stopped.wait(0.1):
                if cancelled.is_set():
                    engine.jobs.cancel_active_job()

        monitor = threading.Thread(target=cancellation_monitor, daemon=True,
                                   name="DLSS cancellation")
        monitor.start()
        notify(0.0, "Reading video and checking settings")
        source = Path(path).expanduser().resolve()
        metadata = probe(source)
        dimensions = validate_settings(settings, metadata)
        destination = Path(output_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        logs = APP_DIR / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        # Keeping staging on the output volume makes final publication a rename.
        work = destination / ".work"
        work.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        reports = {}
        stage_timings = {}
        with tempfile.TemporaryDirectory(prefix="dlss-", dir=work) as temporary:
            stage = Path(temporary)
            order = ("interpolation", "upscale") if settings.processing_order == "interpolate_first" else ("upscale", "interpolation")
            # The expensive cascade runs at source size in Fast mode. Both modes
            # use the same requested final dimensions, frame rate and codec.
            boundary = 0.58 if settings.processing_order == "interpolate_first" else 0.10
            intervals = ((0.01, boundary), (boundary, 0.97))
            finished = source
            for index, operation in enumerate(order):
                start_fraction, end_fraction = intervals[index]
                stage_started = time.perf_counter()
                prefix = f"Stage {index + 1}/2 · {'Interpolate' if operation == 'interpolation' else 'Upscale'}"
                notify(start_fraction, prefix + " · preparing NVIDIA engine")

                def stage_progress(value, message):
                    notify(start_fraction + value * (end_fraction - start_fraction), prefix + " · " + message)

                # The final mux always takes audio/metadata from the original;
                # intermediate AAC is never used as input for a second encode.
                mux_source = str(source) if index == 1 else None
                if operation == "upscale":
                    options = engine.models.ConversionOptions(
                        upscaling_factor=settings.scale, nr_style=settings.style,
                        nr_intensity=settings.intensity, quality=settings.quality,
                        codec=settings.codec, container=settings.container,
                        preserve_hdr=settings.preserve_hdr, rename_mode="Auto",
                        mux_source_path=mux_source,
                    )
                    rendered = engine.video.convert_video(
                        str(finished), options, stage_progress,
                        output_directory=stage / "upscale", jobs_directory=stage / "jobs", logs_directory=logs,
                    )
                else:
                    options = engine.interpolation_models.FrameInterpolationOptions(
                        target_fps=settings.target_fps, engine=settings.interpolation_engine,
                        quality=settings.quality, codec=settings.codec, container=settings.container,
                        hdr_mode=settings.preserve_hdr, rename_mode="Auto", mux_source_path=mux_source,
                        parallel_processing=settings.parallel_processing,
                    )
                    rendered = engine.interpolation.interpolate_video(
                        finished, options, stage_progress,
                        output_directory=stage / "interpolation", jobs_directory=stage / "jobs", logs_directory=logs,
                    )
                check_cancelled()
                report = _read_report(rendered.report_path)
                reports[operation] = report
                stage_timings[operation + "_seconds"] = time.perf_counter() - stage_started
                if operation == "upscale":
                    if not report.get("feature_18_confirmed"):
                        raise RuntimeError("The engine did not confirm DLSS Neural Rendering execution.")
                    if float(settings.scale) > 1 and not report.get("nr_upscaling_active"):
                        if settings.require_neural_upscaling:
                            raise RuntimeError(
                                "Strict validation rejected NVIDIA's native NR fallback. "
                                "Turn off 'Reject fallback output (strict)' in More settings "
                                "to continue with upscaling and frame interpolation. "
                                "This option does not enable neural upscaling support. "
                                f"No output was saved. Report: {rendered.report_path}"
                            )
                        warnings.append("NVIDIA reported its fallback path: output resolution increased "
                                        "and Neural Rendering ran, but direct neural upscaling was not confirmed.")
                finished = Path(rendered.output_path)
            notify(0.98, "Verifying and saving the finished video")
            verified = probe(finished)
            if (verified["width"], verified["height"]) != dimensions:
                raise RuntimeError("The final video's dimensions do not match the requested size.")
            if not finished.is_file() or finished.stat().st_size == 0:
                raise RuntimeError("The engine produced an empty output file.")
            expected_fps = float(engine.interpolation_models.resolve_target_rate(settings.target_fps))
            if not math.isclose(verified["fps"], expected_fps, abs_tol=0.01):
                raise RuntimeError("The final video's frame rate does not match the requested FPS.")
            expected_frames = int(reports["interpolation"]["output_frames"])
            if int(verified["frames"]) != expected_frames:
                verified = {**verified, **engine.ffmpeg.probe_video(finished, count_mode="exact")}
                if int(verified["frames"]) != expected_frames:
                    raise RuntimeError("The final video lost frames after interpolation; no output was saved.")
            fps_suffix = f"_{settings.target_fps}fps"
            stem = source.stem[:100].strip().rstrip(".") or "video"
            tag = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
            name = f"{stem}_DLSS_{settings.scale:g}x{fps_suffix}_{tag}.{settings.container.lower()}"
            final_path = destination / name
            report_path = final_path.with_suffix(final_path.suffix + ".report.json")
            result = {
                "status": "success", "input_path": str(source),
                "output_path": str(final_path), "report_path": str(report_path),
                "warnings": warnings, "width": verified["width"],
                "height": verified["height"], "fps": verified["fps"],
                "frames": verified["frames"], "duration": verified["duration"],
                "elapsed_seconds": time.perf_counter() - started,
                "settings": asdict(settings), "engine_reports": reports,
                "stage_timings": stage_timings, "processing_order": list(order),
                "audio_source": str(source),
            }
            staged_report = stage / "result.report.json"
            staged_report.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                                default=str), encoding="utf-8")
            check_cancelled()
            if final_path.exists() or report_path.exists():
                raise FileExistsError("An output name collision occurred; please run the file again.")
            # On Windows rename fails if the destination already exists.
            finished.rename(final_path)
            try:
                staged_report.rename(report_path)
            except Exception:
                final_path.unlink(missing_ok=True)
                raise
            published = True
        # Once committed, a late Stop request must not mislabel a complete result.
        if progress:
            # A UI notification failure cannot undo a successfully saved file.
            try:
                progress(1.0, "Saved · " + final_path.name)
            except Exception:
                pass
        return result
    except Exception as exc:
        if not published and (cancelled.is_set() or
                              (engine is not None and isinstance(exc, engine.jobs.Cancelled))):
            raise Cancelled("Processing stopped. Your original video is unchanged.") from exc
        raise
    finally:
        stopped.set()
        if monitor is not None:
            monitor.join(timeout=1)
        with _STATE_LOCK:
            _active_cancel = None
        _RENDER_LOCK.release()
