# Audio Visualizer for ComfyUI

Turn a still image and audio into an MP4 with a constant-Q spectrum along
its bottom edge. Choose 64 bars or an LED dot matrix, a color, gain, glow,
and trails. FFmpeg renders and encodes the video in one pass, without
passing video frames through the ComfyUI graph.

## Requirements

- ComfyUI **0.38.0 or newer**, with its normal PyTorch and NumPy dependencies.
- An **FFmpeg executable on PATH**, including `showcqt` and the standard
  video filters. H.264 requires `libx264`; H.265 requires `libx265`.
  Audio requires the selected encoder: `flac`, `aac`, or `libopus`.
- Enough temporary disk space for an RGB image and the full uncompressed
  float32 audio (4 bytes × channels × samples), plus space for the output.

No additional pip packages, models, other custom nodes, or GPU are required.
Rendering uses the CPU. The current version is tested on Linux; Windows and
macOS should work with an FFmpeg build providing the required features,
but have not been tested.

Install FFmpeg through your OS package manager or use a full build linked
from [ffmpeg.org](https://ffmpeg.org/download.html). Make sure `ffmpeg -version`
works in the same environment that launches ComfyUI. Installing a Python
package named `ffmpeg` does not supply this executable. Missing encoders or
filters produce an error naming the missing features when rendering starts.

## Install

Clone this repository into ComfyUI's `custom_nodes` directory:

```sh
git clone https://github.com/bno84/ComfyUI-AudioVisualizer.git
```

Restart ComfyUI and refresh the browser. The node is **🎵 Audio visualizer
video (still + spectrum)** in **audio/visualize**. Registry publication is
being prepared; the repository URL above is the intended publication location.

## Use

Connect an IMAGE and AUDIO, choose the settings, and queue the workflow.
The node saves the MP4 under ComfyUI's output folder and returns a VIDEO
for downstream nodes. Turning `enabled` off renders nothing, does not request
image/audio inputs, and blocks the video output.

- **Visualization:** `style`, `dot_spacing` (dots only, in pixels), `color`,
  `gain`, `glow`, and `trail`. Gain 0 uses the style default: bars 2, dots 32.
  Dot spacing also scales the dot diameter. Glow/trail 0 disables each effect.
- **Timing:** 30 or 60 FPS (default 60); `start` in seconds;
  `duration` 0 means the rest of the audio.
- **Encoding:** H.265 CRF 18 or H.264 CRF 22. Each remembers its own CRF.
  FLAC is the default audio format (24-bit lossless). AAC defaults to
  192 kbps and Opus to 128 kbps, with independent bitrate controls.
  Irrelevant controls hide automatically. Unlabeled separators divide the groups.
- **Filename:** `video/song_visualizer` produces files such as
  `video/song_visualizer_bars_00001_.mp4` or
  `video/song_visualizer_dots_00001_.mp4`. Style is appended automatically.

The first image in a batch sets the output size. Odd image dimensions are
padded by repeating the last row/column for yuv420p. The fixed spectrum band
is 120 pixels high with a 3-pixel bottom margin, so the padded image must be
at least 124 pixels tall. Very narrow images may not fit the fixed bar grid.
The video includes a vignette, a 1.5-second fade-in and a 2.5-second fade-out;
short clips can have overlapping fades.

Workflow and prompt metadata are embedded in the MP4 unless ComfyUI's
`--disable-metadata` option is set. Re-encoding may strip this metadata.
FLAC and Opus inside MP4 have less player support than AAC; use H.264/AAC
when compatibility matters.

## Existing workflows

The node identifier remains `AudioVisualizer`. Earlier local workflows retain
their settings when opened: widget order and renamed controls are migrated,
and an explicit legacy CRF moves to the selected codec's control. Saved prefixes
are retained. Remove the older bundled copy before installing this pack, so
ComfyUI loads only one node with this identifier.

## Development

```sh
python3 -m unittest discover -s tests -v
node --test tests/*.mjs
```

Python tests use FFmpeg and FFprobe for small CPU renders; those integration
tests skip when either executable is absent. FFprobe is only needed for tests.
The node code and frontend are shipped directly, with no build step.
See [RELEASING.md](RELEASING.md) for registry publication.

## License

MIT. Copyright © 2026 bno84.
