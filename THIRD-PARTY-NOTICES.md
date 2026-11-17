# Third-party notices

The original Upscale application code and contributions are available under the
[MIT license](LICENSE). This license does not replace the licenses of third-party
components.

## Engine source and patches

Upscale uses [ComfyUI NVIDIA DLSS Frame Interpolation](https://github.com/Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation)
by Konohamaru04. The engine's application source is provided under the MIT license.
The patches in `patches/` contain changes and context from that source; its original
copyright and permission notice is preserved in
[LICENSES/DLSS-Visual-Enhancer.txt](LICENSES/DLSS-Visual-Enhancer.txt).

Setup downloads the tested engine separately. The engine and its native binaries
are not included in this repository. NVIDIA DLSS runtime components retain their
NVIDIA license; they are not relicensed under the app's MIT license. The upstream
checkout includes NVIDIA, ReShade, and RenoDX notices. Preserve those notices with
the downloaded engine.

## Other dependencies

PySide6/Qt, NumPy, OpenCV, PyAV, FFmpeg, and FFprobe retain their respective licenses.
Python dependencies are installed separately by Setup; FFmpeg and FFprobe must be
installed separately. Refer to each installed package's license notices. This
repository does not distribute their binaries.
