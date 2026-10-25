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
BAND_H = 120                 # visualizer band height
MARGIN = 6                   # gap between bars
BOTTOM_MARGIN = 3
STYLES = ("bars", "dots")
CODECS = ("h265", "h264")
AUDIO_CODECS = ("flac", "opus", "aac")
DEFAULT_CRF = {"h265": 18, "h264": 22}
DEFAULT_GAIN = {"bars": 2.0, "dots": 32.0}   # bars: 16 saturates the jrock-escape bass, 2 leaves headroom
COLOR = re.compile(r"^(0x|#)?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$|^[A-Za-z]+$")
FILTERS = frozenset({
    "format", "vignette", "loop", "setpts", "showcqt", "scale", "lut", "color", "geq",
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


def render(pixels, width, height, samples, sample_rate, channels, output, *, style="bars", color="#DFA078",
           dot=12, gain=0.0, glow=0.0, trail=0.0, codec="h265", crf=-1, audio_codec="flac", audio_bitrate=128,
           start=0.0, duration=0.0, fps=60, aac_bitrate=192, h264_crf=22, h265_crf=18, metadata=None, on_frame=None):
    """Render `output` (mp4). `pixels` are rgb24 bytes; `samples` are interleaved float32 little-endian bytes.

    duration 0 = to the end of the audio. gain 0 = the style's default. crf >= 0 overrides the codec-specific CRF for legacy API prompts.
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
    if height < BAND_H + BOTTOM_MARGIN:
        raise ValueError(f"image is {height}px tall: the visualizer band needs {BAND_H + BOTTOM_MARGIN}px")
    check_ffmpeg(codec, audio_codec)

    total = len(samples) / (4 * channels * sample_rate)
    if start >= total:
        raise ValueError(f"start {start}s is past the end of the audio ({total:.2f}s)")
    dur = total - start if duration <= 0 else min(duration, total - start)
    frames = int(dur * fps)
    fade_out = max(0.0, dur - 2.5)

    W, H = width, BAND_H
    X, Y = 0, height - BAND_H - BOTTOM_MARGIN
    if style == "bars":     # 64 bars with 6px gaps, continuous height; ~1/8-octave bands
        # The final cell already supplies the right gap; inset only the left edge.
        X, W = MARGIN, W - MARGIN
        grid = f"lum='if(lt(mod(X,{W}/64),{W}/64-{MARGIN}),255,0)'"
        low, q = f"64:{H}", 12
    else:                   # LED matrix; bands narrower than the dot spacing so a tone lights ~2-3 columns
        grid = f"lum='255*clip({dot}*0.4-hypot(mod(X,{dot})-({dot}-1)/2,mod(Y,{dot})-({dot}-1)/2),0,1)'"
        low, q = f"{W // dot}:{H // dot}", W // dot * 11 // 32
    gain = gain or DEFAULT_GAIN[style]

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

    # tlength sets constant-Q analysis bands (capped at 0.17s in the bass to limit lag).
    # The default window makes high tones ~1px wide, which area-averaging shrinks away.
    graph = f"""
        [0:v]format=rgb24,vignette=PI/5,
          loop=loop=-1:size=1,setpts=N/({fps}*TB)[scene];

        [1:a]showcqt=s={W}x{H}:fps={fps}:bar_h={H}:axis_h=0:sono_h=0:
          basefreq=40:endfreq=12000:tlength='min(0.17,{q}/f)':bar_g=3:bar_v={gain}:cscheme=1|1|1|1|1|1,
          format=gray,scale={low}:flags=area,lut=y='if(gt(val,127),255,0)',
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
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-y", "-nostats", "-progress", "pipe:1",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-i", image_path,
               "-ss", str(start), "-t", str(dur),
               "-f", "f32le", "-ar", str(sample_rate), "-ac", str(channels), "-i", audio_path]
        meta_args = ["-map_metadata", "-1"]
        if metadata:
            cmd += ["-i", _metadata_file(tmp, metadata)]
            meta_args = ["-map_metadata", "2"]
        cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "1:a", "-frames:v", str(frames),
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
                        on_frame(written, frames)
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
