# Upscale

A free Windows desktop GUI for video upscaling and frame interpolation using the native NVIDIA engine from [ComfyUI NVIDIA DLSS Frame Interpolation](https://github.com/Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation). ComfyUI and PyTorch are not needed to run this GUI.

Download and use Upscale at no charge. No subscription or paid API is required; processing runs locally on a compatible NVIDIA RTX GPU. The app source is available under the [MIT license](LICENSE). Downloaded engine components and other dependencies keep their own licenses; see [third-party notices](THIRD-PARTY-NOTICES.md).

## Use

1. Double-click **Upscale.lnk** (the green arrow icon), or **Launch Upscale.vbs**.
2. Drop one or more videos into the window, or use **+ Add videos** (Ctrl+O).
3. Choose the scale, target frame rate, processing mode, and quality, then click **Start pending (N)**.
4. Find completed videos in **outputs** by default, or the output folder you selected.

You can also drop videos onto **Launch Upscale.vbs** or pass video paths to `launch.ps1`. Originals are kept intact. Keep the complete `engine` folder beside `main.py`; it contains the native NVIDIA workers and runtime DLLs.

You can add videos while a run is active; they wait for the next **Start pending** run. **Cancel run** stops the current video and leaves unstarted videos pending. When idle:

- **Run selected again** processes the selected sources with the current settings and creates new outputs.
- **Retry failed** runs failed or cancelled sources again.
- **Remove selected** (Delete) removes queue entries; **Clear finished** removes successful entries.
- **New batch** (Ctrl+N) empties the queue and resets progress. Saved outputs remain in their output folder.
- **Refresh files** (F5) rereads selected source files, or all files when nothing is selected. **Refresh engine** updates installation checks.

Settings, retry, removal, and refresh controls are disabled during a run. Adding videos remains available. Each run uses a snapshot of its settings and queue, so adding a video does not change work already in progress.

### Queue recovery and checks before starting

The queue is now saved automatically beside the app in `queue.json`. Reopening Upscale restores pending videos, failures, and links to previous results. If the app or PC stops during a run, unfinished entries return as **Interrupted** and wait for **Start pending**; processing never restarts automatically. An interrupted video restarts from its beginning. Completed outputs remain available even if their source was moved. Use **Refresh files** after reconnecting a source drive.

Before a run, the queue checks HDR, codec/container compatibility, target FPS, enhancement settings, and strict mode. Entries show **Check settings** with the reason above the queue; Start, Retry, and Run selected are enabled only when their respective videos have valid settings. These checks do not initialize a GPU render session; the NVIDIA runtime still verifies feature support during processing.

Queue snapshots and preferences use atomic writes. A damaged or newer queue file is preserved and reported in the app, rather than silently replaced. Rename that `queue.json` file and reopen Upscale to start a fresh saved queue. Missing source files remain visible for diagnosis; missing output files become retryable entries. Source files and saved videos are never deleted by queue actions.

### Verification

Run the regression suite after setup:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
```

The queue-recovery and cancellation update passes 59 regression tests. For an optional real NVIDIA GPU test, run `.\.venv\Scripts\python.exe tests\gui_smoke.py`. It creates a short synthetic video if needed, processes it through the actual GUI worker and both GPU stages, decodes the resulting video and audio, checks the source checksum, and verifies saved-queue restoration. Test preferences and queues use an isolated temporary directory. GPU outputs and reports stay under `tests`.

Cancellation now closes the interpolation muxer before its encoder pipe. This fixes a reproduced native crash during later garbage collection. A regression test checks cleanup ordering, including a broken pipe; `tests/cancel_smoke.py` checks cancellation with the real GPU pipeline. Three consecutive native cancellation runs exited cleanly, stopping within 0.16 seconds on the test PC.

Every queued video goes through both upscaling and frame interpolation. Interpolation is always enabled. New settings default to **60 FPS**; saved settings are remembered. You can choose a target up to **120 FPS** and **6x the source frame rate**. The target must exceed the video's source frame rate. Native-size enhancement still includes frame interpolation.

## Processing modes and speed

**Fast · interpolate, then upscale** is the default. It generates intermediate frames at the source resolution, then enlarges the completed sequence. **Detail · upscale, then interpolate** preserves the original processing order and runs interpolation at the enlarged resolution. Both modes produce the requested resolution and frame rate with interpolation enabled. Changing the order changes image processing and can change appearance; the two outputs are not pixel-identical.

**Advanced settings → Parallel interpolation passes** is enabled by default. It overlaps decoding, the independent DLSSG passes, and encoding within one video. Each pass keeps its complete motion history and processes frames in order; videos are not split into separately initialized chunks. Small, resolution-dependent queues limit buffering to 1–4 frames per connection. The upscaler already overlaps decoding/motion preparation, its native GPU worker, and encoding. Queue entries still run one video at a time. Turning parallel passes off uses the serial path and keeps frame interpolation enabled.

Reports record the actual interpolation pipeline mode, per-pass processing times, and maximum queue depths. For diagnostic comparisons, `DLSS_INTERPOLATION_PIPELINE=0` also selects serial interpolation without changing saved preferences. `tools/benchmark_video.py --parallel off` and `--parallel on` provide complete-job comparisons.

Use an **NVIDIA NVENC** codec for GPU encoding. Codec, container, look, interpolation engine, HDR, intensity, and strict validation are under **Advanced settings**. The selected codec and container remain visible above the queue. **Max** encoding quality can produce large files; changing quality affects the result as well as processing cost.

This engine uses both CPU and GPU work, including CPU motion guides and transfers between native workers. On this PC, one fully occupied logical CPU accounts for about 4% of total CPU usage. Allocated VRAM is capacity in use, not a speed indicator; filling all 24 GB is not a performance target. The GPU's compute and video-encoding engines also have separate utilization readings.

The NVIDIA engine offers native-size enhancement and enlargement up to 3x, with output dimensions rounded to even numbers. It uses the installed NVIDIA GPU and driver. Neural Rendering availability is checked when processing. Some configurations use DLSS Super Resolution followed by Neural Rendering at the enlarged native resolution; diagnostics identify that native NR fallback. A successful fallback does not mean direct neural upscaling was used. Inspect the job diagnostics for the path actually used on your video.

HDR source videos require **Preserve HDR** and H.265, AV1 or ProRes. Enabling HDR selects H.265 NVENC if H.264 was selected. ProRes requires MOV or MKV. The final stage takes audio and metadata directly from the original source. MP4/MOV audio is encoded as AAC; MKV preserves compatible source audio/subtitles. Both video stages encode, so processing time and file size depend on resolution, target FPS and quality.

Leave **Advanced settings → Reject fallback output (strict)** off for normal use. It is a validation switch, not a way to enable better upscaling. When checked, the app rejects the entire result if NVIDIA uses its native NR fallback. If you see that rejection, turn the switch off and retry; upscaling and frame interpolation both remain enabled.

## Verified on this PC

On the RTX 4090 with driver 616.56, the user's complete **10.125-second, 736×1280, 24 FPS** clip processed in **106.61 seconds**, compared with **402.85 seconds** for the prior pipeline: **3.78× faster**. Both runs used 3× enlargement, 60 FPS, Default look, Max quality, H.265 NVENC, and MP4. The optimized result contains **608 frames at 2208×3840 / 60 FPS**. Interpolation took 61.30 seconds and upscaling took 45.09 seconds. This comparison includes the new Fast processing order and CPU/buffer optimizations; appearance can differ because processing order changed. It is a measurement of this clip, not a promise for every video.

The benchmark report is `tests/benchmark-fast.json`; the output's adjacent JSON report contains stage reports and the actual NVIDIA execution evidence. Direct neural upscaling used the repository's reported fallback. The GUI and saved report identify this; a completed enlarged video does not itself confirm direct neural Super Resolution.

The user's latest settings (3×, 120 FPS, Cinematic, Max, H.265 NVENC, MOV) initially completed on the full clip in **149.51 seconds**, producing **1,215 frames at 2208×3840 / 120 FPS**. Both full outputs passed complete video/audio decoding checks and retained the 10.125-second audio track. The source file's checksum stayed unchanged.

The subsequent parallel-processing comparison used those same 120 FPS settings: a fresh serial baseline took **145.80 seconds**, and parallel passes plus the exact motion-buffer conversion improvement took **127.45 seconds** (**14.4% faster**, 12.6% less elapsed time). Interpolation fell from **66.66 to 44.42 seconds**; upscaling took **82.81 seconds** and remains the larger cost. Data is in `tests/benchmark-parallel-before.json` and `tests/benchmark-parallel-after.json`. Reducing OpenCV to four threads was slower in a separate test, so the original automatic thread configuration is retained.

The serial and parallel 240-frame comparison had identical decoded pixels, timestamps, and interpolation counters. The complete parallel result passed video/audio decoding, retained all 1,215 frames and the 10.125-second audio track, and left the source checksum unchanged. The app passed **44 regression tests**, a real GUI render, and native parallel cancellation checks that stopped within 0.3 seconds without publishing incomplete outputs. Results are specific to this clip and PC.

## Setup or repair

This folder uses its own `.venv`. To recreate it, open PowerShell here and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup.ps1
```

Setup supports Python 3.11–3.13. To choose an interpreter:

```powershell
.\Setup.ps1 -Python "C:\Program Files\Python311\python.exe"
```

It installs the versions in `requirements.txt` and requires Git to check the bundled engine patch. If `engine` is absent, it also requires Git LFS, clones the upstream repository at commit `c755e274a405a7a47667bd567d489b6845066bcf`, and downloads its LFS binaries. If a previous download was interrupted, complete its binaries with `git -C .\engine lfs pull`.

Setup installs `patches/engine-performance.patch` after cloning or checking an existing engine. It includes parallel interpolation, motion-guide and frame-buffer optimizations, timestamp normalization, and original-source audio support needed by this GUI. It then installs `patches/engine-cancellation.patch` for deterministic muxer cleanup. Setup recognizes already-applied patches and leaves them alone. It recognizes the previous v1 performance patch and applies the supplied upgrade patch to that known state. An existing engine version and unrelated edits are preserved. If patch checks find a conflict, Setup stops with a clear error instead of resetting or overwriting the checkout. Keep the complete `patches` directory with the application when copying or reinstalling it.

A compatible NVIDIA RTX GPU, current driver, **FFmpeg**, and **FFprobe** are required. FFmpeg and FFprobe must be on PATH, or their locations can be set explicitly before launching:

```powershell
$env:DLSS_FFMPEG_PATH = "C:\tools\ffmpeg\bin\ffmpeg.exe"
$env:DLSS_FFPROBE_PATH = "C:\tools\ffmpeg\bin\ffprobe.exe"
.\launch.ps1
```

Setup does not change system-wide settings. Processing is local. Launcher and application startup logs are saved in **logs**. If the window does not open, its error popup includes the relevant log path. The upstream engine license and NVIDIA component notices remain in the `engine` folder.
