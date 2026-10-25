"""Still image + audio -> visualizer video with a discrete spectrum, in one ffmpeg call.

No ComfyUI imports: the node feeds raw
pixels and samples through temp files, and receives progress through `on_frame(written, total)`.
"""
import os
import re
import subprocess
import tempfile
import shutil
from functools import lru_cache

FRAME_RATES = (30, 60)
DEFAULT_VIGNETTE = 0.4      # 0..1 maps to FFmpeg angle 0..PI/2; 0.4 preserves PI/5
DEFAULT_BAND = 0.35          # band height as a fraction of the video height
BAND_RANGE = (0.1, 0.9)
MEASURE_SHARE = 0.1          # share of the progress bar for measuring; encoding takes far longer
GAIN_RANGE = (0.25, 2.0)     # height of the loudest moment; 1.0 reaches the top of the band
MIN_BAND_H = 16
MEASURE_H = 512              # rows for measuring the loudest bar height
MARGIN = 6                   # gap between bars
BOTTOM_MARGIN = 3
STYLES = ("bars", "dots")
CODECS = ("h265", "h264")
AUDIO_CODECS = ("flac", "opus", "aac")
DEFAULT_CRF = {"h265": 18, "h264": 22}
COLOR = re.compile(r"^(0x|#)?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$|^[A-Za-z]+$")
FILTERS = frozenset({
    "volume", "format", "vignette", "loop", "setpts", "showcqt", "scale", "lut", "color", "geq",
    "blend", "lagfun", "split", "alphamerge", "colorchannelmixer", "gblur", "overlay", "fade", "fps",
})


@lru_cache(maxsize=2)
def _ffmpeg_features(kind):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("Audio Visualizer needs the FFmpeg executable on PATH. "
                           "Install a full FFmpeg build in the environment that runs ComfyUI.")
    try:
        result = subprocess.run([executable, "-hide_banner", f"-{kind}"],
                                capture_output=True, text=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Audio Visualizer could not inspect FFmpeg {kind}: {error}") from error
    return frozenset(line.split()[1] for line in result.stdout.splitlines()
                     if len(line.split()) >= 2)


def check_ffmpeg(video_codec, audio_codec):
    """Check only the selected encoders; cache capability listings, not render failures."""
    needed = {"libx264" if video_codec == "h264" else "libx265",
              {"flac": "flac", "aac": "aac", "opus": "libopus"}[audio_codec]}
    missing_encoders = needed - _ffmpeg_features("encoders")
    missing_filters = FILTERS - _ffmpeg_features("filters")
    missing = []
    if missing_encoders:
        missing.append("encoders: " + ", ".join(sorted(missing_encoders)))
    if missing_filters:
        missing.append("filters: " + ", ".join(sorted(missing_filters)))
    if missing:
        raise RuntimeError("Audio Visualizer's FFmpeg build is missing " + "; ".join(missing)
                           + ". Install a full FFmpeg build or choose an available codec.")


def _metadata_file(directory, metadata):
    """An FFMETADATA file: workflow JSON easily exceeds the 128 KiB limit of one command-line argument."""
    def escape(text):
        return re.sub(r"([=;#\\\n])", r"\\\1", text)
    path = os.path.join(directory, "metadata.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        for key, value in metadata.items():
            f.write(f"{escape(key)}={escape(value)}\n")
    return path


def _spectrum(style, width, band_h, dot, fps, level, rows=None):
    """The showcqt chain up to the binary band mask: its x offset, width, filters, grid and columns.

    level scales the analysed audio only. showcqt clamps bar_v at 100, too little for quiet audio.
    rows overrides the mask's vertical resolution, for measuring.
    """
    X, W, H = 0, width, band_h
    if style == "bars":     # 64 bars with 6px gaps, continuous height; ~1/8-octave bands
        # The final cell already supplies the right gap; inset only the left edge.
        X, W = MARGIN, W - MARGIN
        grid = f"lum='if(lt(mod(X,{W}/64),{W}/64-{MARGIN}),255,0)'"
        columns, q = 64, 12
        rows = rows or H
    else:                   # LED matrix; bands narrower than the dot spacing so a tone lights ~2-3 columns
        grid = f"lum='255*clip({dot}*0.4-hypot(mod(X,{dot})-({dot}-1)/2,mod(Y,{dot})-({dot}-1)/2),0,1)'"
        columns, q = W // dot, W // dot * 11 // 32
        rows = rows or H // dot
    # tlength sets constant-Q analysis bands (capped at 0.17s in the bass to limit lag).
    # The default window makes high tones ~1px wide, which area-averaging shrinks away.
    bands = (f"volume={level:.6g}:precision=float,showcqt=s={W}x{H}:fps={fps}:bar_h={H}:axis_h=0:sono_h=0:"
             f"basefreq=40:endfreq=12000:tlength='min(0.17,{q}/f)':bar_g=3:bar_v=1:cscheme=1|1|1|1|1|1,"
             f"format=gray,scale={columns}:{rows}:flags=area,lut=y='if(gt(val,127),255,0)'")
    return X, W, bands, grid, columns


def _peak(inputs, style, width, dot, fps, level, frames, on_frame):
    """The loudest bar height over all frames as a fraction of the band, at this level."""
    # Full row resolution; lagfun with decay 1 keeps each pixel's maximum over time.
    *_, bands, _, columns = _spectrum(style, width, MEASURE_H, dot, fps, level, rows=MEASURE_H)
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", *inputs, "-filter_complex",
           f"[0:a]{bands},lagfun=decay=1[m]", "-map", "[m]", "-frames:v", str(frames),
           "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    size = columns * MEASURE_H
    last, written = b"", 0
    with subprocess.Popen(cmd, stdout=subprocess.PIPE) as proc:
        try:
            while chunk := proc.stdout.read(size):
                last = chunk if len(chunk) == size else (last + chunk)[-size:]
                written += 1
                if on_frame and written % fps == 0:
                    on_frame(written, frames)
        except BaseException:
            proc.kill()
            raise
    if proc.returncode or len(last) < size:
        raise RuntimeError(f"ffmpeg exited with status {proc.returncode} while measuring loudness")
    lit = max(sum(last[y * columns + x] > 127 for y in range(MEASURE_H)) for x in range(columns))
    return lit / MEASURE_H


def full_scale(inputs, style, width, dot, fps, frames, on_frame=None):
    """The level at which the loudest moment exactly reaches the top of the band.

    Bar heights grow with the level to the power 2/3 (showcqt's cube-root bars, then
    the band's area scaling and threshold), so one measurement below the top suffices.
    Very quiet or clipped readings are measured again at a better level.
    """
    level = 4.0
    for _ in range(8):
        peak = _peak(inputs, style, width, dot, fps, level, frames, on_frame)
        if peak >= 1.0:
            level /= 8          # the peak drops to a quarter
        elif peak < 0.2:
            level *= 64 if peak == 0 else (0.6 / peak) ** 1.5
        else:
            return level * peak ** -1.5
    return 1.0  # silence: nothing to scale


def render(pixels, width, height, samples, sample_rate, channels, output, *, style="bars", color="#9750c4",
           dot=17, gain=1.3, glow=0.0, trail=0.0, codec="h265", crf=-1, audio_codec="flac", audio_bitrate=128,
           start=0.0, duration=0.0, fps=60, aac_bitrate=192, h264_crf=22, h265_crf=18, metadata=None, on_frame=None,
           band_height=DEFAULT_BAND, vignette=DEFAULT_VIGNETTE):
    """Render `output` (mp4). `pixels` are rgb24 bytes; `samples` are interleaved float32 little-endian bytes.

    band_height is the band's share of the video height, 0.1 to 0.9. gain scales the band so that 1.0
    makes the loudest moment just reach its top; the audio is measured first.
    vignette controls edge darkening from 0 (disabled) to 1 (strongest); default 0.4 preserves PI/5.
    duration 0 = to the end of the audio. crf >= 0 overrides the codec-specific CRF for legacy API prompts.
    Returns the number of frames written.
    """
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}")
    if codec not in CODECS:
        raise ValueError(f"unknown codec {codec!r}")
    if audio_codec not in AUDIO_CODECS:
        raise ValueError(f"unknown audio codec {audio_codec!r}")
    if fps not in FRAME_RATES:
        raise ValueError(f"unsupported frame rate {fps!r}: choose 30 or 60")
    color = color.strip()
    if not COLOR.match(color):
        raise ValueError(f"color {color!r}: use 0xRRGGBB, #RRGGBB or an ffmpeg color name")
    if width % 2 or height % 2:
        raise ValueError(f"image is {width}x{height}: yuv420p needs an even width and height")
    # showcqt needs an even height.
    band_h = 2 * round(height * band_height / 2)
    if not BAND_RANGE[0] <= band_height <= BAND_RANGE[1]:
        raise ValueError(f"band_height {band_height}: use {BAND_RANGE[0]} to {BAND_RANGE[1]} of the video height")
    if band_h < max(MIN_BAND_H, dot if style == "dots" else 0):
        raise ValueError(f"band_height {band_height} is {band_h}px of this {height}px image: too low for the band")
    if height < band_h + BOTTOM_MARGIN:
        raise ValueError(f"image is {height}px tall: the visualizer band needs {band_h + BOTTOM_MARGIN}px")
    if not GAIN_RANGE[0] <= gain <= GAIN_RANGE[1]:
        raise ValueError(f"gain {gain}: use {GAIN_RANGE[0]} to {GAIN_RANGE[1]}, 1.0 reaching the top of the band")
    if not 0.0 <= vignette <= 1.0:
        raise ValueError(f"vignette {vignette}: use 0.0 to 1.0; 0 disables it")
    check_ffmpeg(codec, audio_codec)

    total = len(samples) / (4 * channels * sample_rate)
    if start >= total:
        raise ValueError(f"start {start}s is past the end of the audio ({total:.2f}s)")
    dur = total - start if duration <= 0 else min(duration, total - start)
    frames = int(dur * fps)
    fade_out = max(0.0, dur - 2.5)

    H, Y = band_h, height - band_h - BOTTOM_MARGIN

    quality = crf if crf >= 0 else {"h264": h264_crf, "h265": h265_crf}[codec]
    if codec == "h264":
        venc = ["-c:v", "libx264", "-preset", "slow", "-crf", str(quality)]
    else:
        venc = ["-c:v", "libx265", "-preset", "slow", "-crf", str(quality),
                "-tag:v", "hvc1", "-x265-params", "log-level=warning"]
    if audio_codec == "flac":
        aenc = ["-c:a", "flac", "-sample_fmt", "s32"]    # 24-bit; ComfyUI audio is float
    elif audio_codec == "aac":
        aenc = ["-c:a", "aac", "-b:a", f"{aac_bitrate}k"]
    else:
        aenc = ["-c:a", "libopus", "-b:a", f"{audio_bitrate}k"]

    vignette_filter = f"vignette=PI/2*{vignette:.12g}," if vignette > 0 else ""

    def graph(level):
        X, W, bands, grid, _ = _spectrum(style, width, H, dot, fps, level)
        return X, f"""
        [0:v]format=rgb24,{vignette_filter}
          loop=loop=-1:size=1,setpts=N/({fps}*TB)[scene];

        [1:a]{bands},
          scale={W}:{H}:flags=neighbor[bands];
        color=c=black:s={W}x{H}:r={fps},format=gray,geq={grid}[grid];
        [bands][grid]blend=all_mode=multiply,lagfun=decay={trail},split[m1][m2];
        color=c={color}:s={W}x{H}:r={fps},format=rgba,split[g1][g2];
        [g1][m1]alphamerge,colorchannelmixer=aa=0.9[sharp];
        [m2]gblur=sigma=5[mb];
        [g2][mb]alphamerge,colorchannelmixer=aa={glow}[glow];

        [scene][glow]overlay={X}:{Y}:shortest=1:format=rgb[s1];
        [s1][sharp]overlay={X}:{Y}:shortest=1:format=rgb,
          fade=in:st=0:d=1.5,fade=out:st={fade_out:.2f}:d=2.5,
          fps={fps},format=yuv420p[v]
    """

    with tempfile.TemporaryDirectory(prefix="audio_viz_") as tmp:
        image_path = os.path.join(tmp, "image.rgb")
        audio_path = os.path.join(tmp, "audio.f32")
        with open(image_path, "wb") as f:
            f.write(pixels)
        with open(audio_path, "wb") as f:
            f.write(samples)
        audio_input = ["-ss", str(start), "-t", str(dur),
                       "-f", "f32le", "-ar", str(sample_rate), "-ac", str(channels), "-i", audio_path]
        # One progress bar that only moves forward: ComfyUI drops updates below the last one sent.
        # Measuring fills the first MEASURE_SHARE, encoding the rest; extra passes hold their share.
        measure_units = int(frames * MEASURE_SHARE / (1 - MEASURE_SHARE))
        total, shown = frames + measure_units, 0

        def measuring(written, _frames):
            nonlocal shown
            shown = max(shown, written * measure_units // frames)
            if on_frame:
                on_frame(shown, total)

        # Height grows with the level to the power 2/3, so gain g needs g ** 1.5 times full scale.
        level = full_scale(audio_input, style, width, dot, fps, frames, measuring) * gain ** 1.5
        X, filters = graph(level)
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-y", "-nostats", "-progress", "pipe:1",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-i", image_path, *audio_input]
        meta_args = ["-map_metadata", "-1"]
        if metadata:
            cmd += ["-i", _metadata_file(tmp, metadata)]
            meta_args = ["-map_metadata", "2"]
        cmd += ["-filter_complex", filters, "-map", "[v]", "-map", "1:a", "-frames:v", str(frames),
                *venc, "-g", str(fps * 10), "-force_key_frames", "1.6", "-pix_fmt", "yuv420p",
                # forced keyframe right after the fade-in; otherwise the still is built from degraded fade
                # frames until 10s
                *aenc, *meta_args, "-shortest", "-movflags", "+faststart+use_metadata_tags", output]

        written = 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        try:
            for line in proc.stdout:
                if line.startswith("frame="):
                    written = int(line.split("=", 1)[1])
                    if on_frame:
                        on_frame(measure_units + written, total)
        except BaseException:
            proc.kill()
            proc.wait()
            if os.path.exists(output):
                os.unlink(output)
            raise
        finally:
            proc.stdout.close()
        if proc.wait():
            raise RuntimeError(f"ffmpeg exited with status {proc.returncode}; see the ComfyUI log for its error")
    return written
