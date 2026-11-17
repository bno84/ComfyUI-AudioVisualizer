"""CPU FFmpeg filters and encoding through PyAV; no executable or ComfyUI imports."""
import math
import os
import re
from fractions import Fraction

import av
import numpy as np

FRAME_RATES = (30, 60)
DEFAULT_VIGNETTE = 0.0      # 0..1 maps to FFmpeg angle 0..PI/2; 0.4 preserves PI/5
DEFAULT_BAND = 0.35          # band height as a fraction of the video height
BAND_RANGE = (0.1, 0.9)
MEASURE_SHARE = 0.1          # share of the progress bar for measuring; encoding takes far longer
GAIN_RANGE = (0.25, 2.0)     # height of the loudest moment; 1.0 reaches the top of the band
MIN_BAND_H = 16
MEASURE_H = 512              # rows for measuring the loudest bar height
MARGIN = 6                   # gap between bars
BOTTOM_MARGIN = 3
STYLES = ("(none)", "bars", "dots", "spline")
DEFAULT_FADE_IN = 1.5
DEFAULT_FADE_OUT = 2.5
CONTAINERS = ("mp4", "webm")
CODECS = ("h265", "h264", "vp9")
AUDIO_CODECS = ("flac", "opus", "aac")
DEFAULT_CRF = {"h265": 18, "h264": 22, "vp9": 20}
COLOR = re.compile(r"^(0x|#)?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$|^[A-Za-z]+$")
VIDEO_ENCODERS = {"h264": "libx264", "h265": "libx265", "vp9": "libvpx-vp9"}
AUDIO_ENCODERS = {"flac": "flac", "aac": "aac", "opus": "libopus"}
SCENE_FILTERS = frozenset({"buffer", "buffersink", "format", "loop", "setpts", "fps"})
SPECTRUM_FILTERS = frozenset({
    "abuffer", "volume", "showcqt", "scale", "lut", "color", "geq", "blend", "lagfun",
    "split", "alphamerge", "colorchannelmixer", "gblur", "overlay",
})
CHUNK_SAMPLES = 1024


def check_features(codec, audio_codec, style, vignette=0, fade_in=0, fade_out=0):
    """Check the library build actually used by PyAV, not an unrelated executable."""
    needed = SCENE_FILTERS | (SPECTRUM_FILTERS if style != "(none)" else set())
    if vignette:
        needed |= {"vignette"}
    if fade_in or fade_out:
        needed |= {"fade"}
    missing = []
    encoders = []
    for name in (VIDEO_ENCODERS[codec], AUDIO_ENCODERS[audio_codec]):
        try:
            av.Codec(name, "w")
        except (ValueError, av.error.FFmpegError):
            encoders.append(name)
    if encoders:
        missing.append("encoders: " + ", ".join(encoders))
    filters = needed - av.filter.filters_available
    if filters:
        missing.append("filters: " + ", ".join(sorted(filters)))
    if missing:
        raise RuntimeError("Audio Visualizer's PyAV build is missing " + "; ".join(missing)
                           + ". Choose an available style/codec, or use a PyAV/FFmpeg build "
                           "with these features in ComfyUI's Python environment. "
                           "Manage PyAV with the package manager used by that environment; "
                           "installing an ffmpeg executable does not change PyAV's libraries.")


def _chain(graph, source, *filters):
    for name, args in filters:
        target = graph.add(name, args)
        source.link_to(target)
        source = target
    return source


class _Audio:
    """A view over the input bytes, fed in bounded packed-float chunks."""
    def __init__(self, samples, rate, channels, start=0, duration=0):
        self.rate = rate
        self.layout = av.AudioLayout(f"{channels}c").name
        data = np.frombuffer(samples, dtype="<f4").reshape(-1, channels)
        first = round(start * rate)
        last = len(data) if duration <= 0 else min(len(data), first + round(duration * rate))
        self.data = data[first:last]

    def __len__(self):
        return len(self.data)

    def frame(self, position):
        # Packed floats avoid a transpose and PyAV's eight-channel planar-array limitation.
        chunk = np.ascontiguousarray(self.data[position:position + CHUNK_SAMPLES].reshape(1, -1),
                                     dtype=np.float32)
        frame = av.AudioFrame.from_ndarray(chunk, format="flt", layout=self.layout)
        frame.sample_rate, frame.time_base, frame.pts = self.rate, Fraction(1, self.rate), position
        return frame

    def source(self, graph):
        return graph.add_abuffer(sample_rate=self.rate, format="flt", layout=self.layout,
                                 time_base=Fraction(1, self.rate))


class _Feed:
    """Supply analysis and encoding with the same timestamped audio chunks on demand."""
    def __init__(self, audio, source=None, consume=None):
        self.audio, self.source, self.consume = audio, source, consume
        self.position, self.eof = 0, False

    def step(self):
        if self.eof:
            return False
        if self.position < len(self.audio):
            frame = self.audio.frame(self.position)
            if self.source is not None:
                self.source.push(frame)
            if self.consume is not None:
                self.consume(frame)
            self.position += frame.samples
        else:
            if self.source is not None:
                self.source.push(None)
            self.eof = True
        return True

    def read(self, sink):
        while True:
            try:
                return sink.pull()
            except av.error.BlockingIOError:
                if not self.step():
                    raise RuntimeError("PyAV filter graph stalled after the end of the audio") from None


def _spectrum(style, width, band_h, dot, fps, level, rows=None):
    """The showcqt chain up to the binary band mask: its x offset, width, filters, grid and columns.

    level scales the analysed audio only. showcqt clamps bar_v at 100, too little for quiet audio.
    rows overrides the mask's vertical resolution, for measuring.
    """
    X, W, H = 0, width - width % 2, band_h
    if style in ("bars", "spline"):     # 64 bars with 6px gaps, continuous height; ~1/8-octave bands
        # The final cell already supplies the right gap; inset only the left edge.
        margin = MARGIN if W >= 64 * (MARGIN + 1) else max(1, W // 192)
        X, W = (0, W) if style == "spline" else (margin, W - margin)
        W -= W % 2  # showcqt requires an even width
        grid = f"lum='if(lt(mod(X,{W}/64),{W}/64-{margin}),255,0)'"
        columns, q = 64, 12
        rows = rows or H
    else:                   # LED matrix; bands narrower than the dot spacing so a tone lights ~2-3 columns
        columns, q = W // dot, W // dot * 11 // 32
        rows = rows or H // dot
        # The nearest-neighbour mask fills W x H, so its cells are usually
        # slightly larger than dot. Center each LED in those same cells;
        # a fixed-pitch grid drifts across their edges and gets sliced.
        grid = (f"lum='255*clip({dot}*0.4-hypot("
                f"(mod((X+0.5)*{columns}/{W},1)-0.5)*{W}/{columns},"
                f"(mod((Y+0.5)*{rows}/{H},1)-0.5)*{H}/{rows}),0,1)'")
    # tlength sets constant-Q analysis bands (capped at 0.17s in the bass to limit lag).
    # The default window makes high tones ~1px wide, which area-averaging shrinks away.
    bands = [
        ("volume", f"{level:.6g}:precision=float"),
        ("showcqt", f"s={W}x{H}:fps={fps}:bar_h={H}:axis_h=0:sono_h=0:"
         f"basefreq=40:endfreq=12000:tlength='min(0.17,{q}/f)':bar_g=3:bar_v=1:cscheme=1|1|1|1|1|1"),
        ("format", "gray"),
        ("scale", f"{columns}:{rows}:flags=area"),
        ("lut", "y='if(gt(val,127),255,0)'"),
    ]
    return X, W, bands, grid, columns


def _peak(audio, style, width, dot, fps, level, frames, on_frame):
    """Measure maximum mask height through the same filters used for rendering."""
    graph = av.filter.Graph()
    source = audio.source(graph)
    *_, bands, _, columns = _spectrum(style, width, MEASURE_H, dot, fps, level, rows=MEASURE_H)
    end = _chain(graph, source, *bands, ("lagfun", "decay=1"))
    sink = graph.add("buffersink")
    end.link_to(sink)
    graph.configure()
    feed = _Feed(audio, source)
    last = None
    for written in range(frames):
        try:
            last = feed.read(sink)
        except av.error.EOFError:
            break
        if on_frame:
            on_frame(written + 1, frames)
    if last is None:
        return 0.0
    mask = last.to_ndarray(format="gray")
    return float((mask > 127).sum(axis=0).max() / MEASURE_H)


def full_scale(audio, style, width, dot, fps, frames, on_frame=None):
    """The level at which the loudest moment exactly reaches the top of the band.

    Bar heights grow with the level to the power 2/3 (showcqt's cube-root bars,
    followed by area scaling and thresholding), so one reading below the top
    suffices. Very quiet or clipped readings need a better measurement level.
    """
    level = 4.0
    for _ in range(8):
        peak = _peak(audio, style, width, dot, fps, level, frames, on_frame)
        if peak >= 1.0:
            level /= 8  # the peak drops to a quarter
        elif peak < 0.2:
            level *= 64 if peak == 0 else (0.6 / peak) ** 1.5
        else:
            return level * peak ** -1.5
    return 1.0  # silence: nothing to scale


def _stroke_polyline(points, width, height, thickness):
    """Antialiased round stroke using Euclidean distance to each line segment.

    Work only inside segment bounds, keeping memory independent of curve length.
    Union coverage before applying effects so joins cannot brighten or widen.
    """
    alpha = np.zeros((height, width), dtype=np.float32)
    radius = thickness / 2 + .5
    for first, last in zip(points[:-1], points[1:]):
        x0, y0 = first
        x1, y1 = last
        left = max(0, math.ceil(min(x0, x1) - radius))
        right = min(width, math.floor(max(x0, x1) + radius) + 1)
        top = max(0, math.ceil(min(y0, y1) - radius))
        bottom = min(height, math.floor(max(y0, y1) + radius) + 1)
        if left >= right or top >= bottom:
            continue
        xx = np.arange(left, right)[None, :] - x0
        yy = np.arange(top, bottom)[:, None] - y0
        dx, dy = x1-x0, y1-y0
        length2 = dx*dx + dy*dy
        t = np.clip((xx*dx + yy*dy) / length2, 0, 1) if length2 else 0
        distance = np.hypot(xx-t*dx, yy-t*dy)
        coverage = np.clip(radius-distance, 0, 1)
        region = alpha[top:bottom, left:right]
        np.maximum(region, coverage, out=region)
    return (alpha * 255).astype(np.uint8)


def _spline_mask(mask, width, height, line_thickness=2.0):
    """Shape-preserving cubic Hermite interpolation of 64 measured band heights."""
    line_thickness = min(line_thickness, height - 2, width - 2)
    values = (mask > 127).sum(axis=0).astype(float)
    delta = np.diff(values)
    slopes = np.zeros_like(values)
    slopes[0], slopes[-1] = delta[0], delta[-1]
    same = delta[:-1] * delta[1:] > 0
    slopes[1:-1][same] = 2 * delta[:-1][same] * delta[1:][same] / (delta[:-1][same] + delta[1:][same])
    # Half-pixel segments closely approximate the cubic while keeping the
    # stroke's width perpendicular to the curve, including steep peaks.
    inset = line_thickness / 2 + .5
    pixel_x = np.linspace(inset, width-1-inset, max(2, width * 2))
    x = np.linspace(0, len(values)-1, len(pixel_x))
    i = np.minimum(x.astype(int), len(values)-2)
    t = x-i
    y = ((2*t**3-3*t**2+1)*values[i] + (t**3-2*t**2+t)*slopes[i]
         + (-2*t**3+3*t**2)*values[i+1] + (t**3-t**2)*slopes[i+1])
    centers = height - 1 - inset - np.clip(y / height, 0, 1) * (height - 1 - 2 * inset)
    # Zero-height bands remain connected along the inset baseline.
    return _stroke_polyline(np.column_stack((pixel_x, centers)), width, height, line_thickness)


def _channel_audio(audio):
    # Mono repeats; for multichannel audio use front left and front right.
    return [_Audio(np.ascontiguousarray(audio.data[:, c:c+1]).tobytes(), audio.rate, 1)
            for c in (0, min(1, audio.data.shape[1]-1))]


class _MaskSource:
    """Bounded audio analysis feeding a raster mask into the existing effects graph."""
    def __init__(self, target, audio, style, width, height, dot, fps, level, stereo, gap, line_thickness=2.0):
        self.target, self.style = target, style
        self.line_thickness = line_thickness
        self.width, self.height, self.stereo, self.gap = width, height, stereo, gap
        self.parts = []
        self.remaining = int(len(audio) / audio.rate * fps)
        self.closed = False
        w = (width-gap)//2 if stereo else width
        for channel in (_channel_audio(audio) if stereo else [audio]):
            graph = av.filter.Graph()
            source = channel.source(graph)
            x, actual_w, bands, grid, _ = _spectrum(style, w, height, dot, fps, level)
            end = _chain(graph, source, *bands)
            if style != "spline":
                end = _chain(graph, end, ("scale", f"{actual_w}:{height}:flags=neighbor"))
                black = graph.add("color", f"c=black:s={actual_w}x{height}:r={fps}")
                grid_node = _chain(graph, black, ("format", "gray"), ("geq", grid))
                blend = graph.add("blend", "all_mode=multiply")
                end.link_to(blend, 0, 0)
                grid_node.link_to(blend, 0, 1)
                end = blend
            sink = graph.add("buffersink")
            end.link_to(sink)
            graph.configure()
            self.parts.append((graph, source, sink, channel, x, actual_w))

    def push(self, frame):
        if self.closed:
            return
        for _, source, _, audio, _, _ in self.parts:
            source.push(None if frame is None else audio.frame(frame.pts))
        while self.remaining:
            masks, stamp = [], None
            for _, _, sink, _, x, w in self.parts:
                try:
                    stamp = sink.pull()
                except (av.error.BlockingIOError, av.error.EOFError):
                    if frame is None:
                        self.target.push(None)
                        self.closed = True
                    return
                mask = stamp.to_ndarray(format="gray")
                if self.style == "spline":
                    mask = _spline_mask(mask, w, self.height, self.line_thickness)
                part = np.zeros((self.height, (self.width-self.gap)//2 if self.stereo else self.width), np.uint8)
                part[:, x:x+w] = mask
                masks.append(part)
            result = np.zeros((self.height, self.width), np.uint8)
            result[:, :masks[0].shape[1]] = masks[0]
            if self.stereo:
                result[:, -masks[1].shape[1]:] = masks[1][:, ::-1]
            output = av.VideoFrame.from_ndarray(result, format="gray")
            output.pts, output.time_base = stamp.pts, stamp.time_base
            self.target.push(output)
            self.remaining -= 1
        self.target.push(None)
        self.closed = True


def _composition(pixels, width, height, audio, style, band_h, dot, fps, level, color,
                 glow, trail, vignette, fade_in, fade_out, stereo_mirror=False, stereo_gap=0, line_thickness=2.0):
    graph = av.filter.Graph()
    image = av.VideoFrame.from_ndarray(np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3),
                                      format="rgb24")
    image.time_base, image.pts = Fraction(1, fps), 0
    source = graph.add_buffer(template=image)
    scene = _chain(graph, source, ("format", "rgb24"))
    if vignette:
        scene = _chain(graph, scene, ("vignette", f"PI/2*{vignette:.12g}"))
    scene = _chain(graph, scene, ("loop", "loop=-1:size=1"), ("setpts", f"N/({fps}*TB)"))
    audio_source = None
    if style != "(none)":
        if stereo_mirror or style == "spline":
            x, w = 0, width
            template = av.VideoFrame(width, band_h, "gray")
            template.time_base = Fraction(1, fps)
            mask_source = graph.add_buffer(template=template)
            audio_source = _MaskSource(mask_source, audio, style, width, band_h, dot, fps,
                                       level, stereo_mirror, stereo_gap, line_thickness)
            masks = _chain(graph, mask_source, ("lagfun", f"decay={trail}"), ("split", None))
        else:
            audio_source = audio.source(graph)
            x, w, bands, grid, _ = _spectrum(style, width, band_h, dot, fps, level)
            band = _chain(graph, audio_source, *bands, ("scale", f"{w}:{band_h}:flags=neighbor"))
            black = graph.add("color", f"c=black:s={w}x{band_h}:r={fps}")
            grid = _chain(graph, black, ("format", "gray"), ("geq", grid))
            blend = graph.add("blend", "all_mode=multiply")
            band.link_to(blend, 0, 0)
            grid.link_to(blend, 0, 1)
            masks = _chain(graph, blend, ("lagfun", f"decay={trail}"), ("split", None))
        solid = graph.add("color", f"c={color}:s={w}x{band_h}:r={fps}")
        colors = _chain(graph, solid, ("format", "rgba"), ("split", None))
        sharp = graph.add("alphamerge")
        colors.link_to(sharp, 0, 0)
        masks.link_to(sharp, 0, 1)
        sharp = _chain(graph, sharp, ("colorchannelmixer", "aa=0.9"))
        blur = graph.add("gblur", "sigma=5")
        masks.link_to(blur, 1, 0)
        halo = graph.add("alphamerge")
        colors.link_to(halo, 1, 0)
        blur.link_to(halo, 0, 1)
        halo = _chain(graph, halo, ("colorchannelmixer", f"aa={glow}"))
        for layer in (halo, sharp):
            overlay = graph.add("overlay", f"x={x}:y={height-band_h-BOTTOM_MARGIN}:shortest=1:format=rgb")
            scene.link_to(overlay, 0, 0)
            layer.link_to(overlay, 0, 1)
            scene = overlay
    if fade_in:
        scene = _chain(graph, scene, ("fade", f"in:st=0:d={fade_in:.12g}"))
    if fade_out:
        start = max(0, len(audio) / audio.rate - fade_out)
        scene = _chain(graph, scene, ("fade", f"out:st={start:.12g}:d={fade_out:.12g}"))
    scene = _chain(graph, scene, ("fps", str(fps)), ("format", "yuv420p"))
    sink = graph.add("buffersink")
    scene.link_to(sink)
    graph.configure()
    source.push(image)
    source.push(None)
    return graph, sink, audio_source


def _streams(container, width, height, fps, codec, quality, audio_codec, bitrate, audio):
    video = container.add_stream(VIDEO_ENCODERS[codec], rate=fps)
    video.width, video.height, video.pix_fmt = width, height, "yuv420p"
    video.codec_context.gop_size = fps * 10
    video.options = {"preset": "slow", "crf": str(quality), "forced-idr": "1"}
    if codec == "vp9":
        video.bit_rate = 0
        video.options = {"deadline": "good", "cpu-used": "1", "crf": str(quality)}
        if quality == 0:
            video.options["lossless"] = "1"
    if codec == "h265":
        video.codec_context.codec_tag = "hvc1"
        video.options["x265-params"] = "log-level=warning:open-gop=0:min-keyint=1"
    encoder = av.Codec(AUDIO_ENCODERS[audio_codec], "w")
    rates = encoder.audio_rates
    rate = 48000 if audio_codec == "opus" else audio.rate
    if rates and rate not in rates:
        rate = min(rates, key=lambda value: abs(value - rate))
    sound = container.add_stream(AUDIO_ENCODERS[audio_codec], rate=rate)
    sound.layout = audio.layout
    if audio_codec == "flac":
        sound.codec_context.format = "s32"  # FFmpeg's FLAC encoder stores 24-bit samples.
    else:
        sound.bit_rate = bitrate * 1000
    return video, sound


def render(pixels, width, height, samples, sample_rate, channels, output, *, style="bars", color="#9750c4",
           dot=17, gain=1.3, glow=0.0, trail=0.0, codec="h265", crf=-1, audio_codec="flac", audio_bitrate=128,
           start=0.0, duration=0.0, fps=60, aac_bitrate=192, h264_crf=22, h265_crf=18, metadata=None, on_frame=None,
           band_height=DEFAULT_BAND, vignette=DEFAULT_VIGNETTE,
           fade_in=DEFAULT_FADE_IN, fade_out=DEFAULT_FADE_OUT, container="mp4", vp9_crf=20, stereo_mirror=False, stereo_gap=0, line_thickness=2.0):
    """Render MP4 or WebM using CPU library filters. Inputs remain rgb24 and interleaved f32le bytes.

    band_height is the band's fraction of image height (0.1 to 0.9). gain 1.0
    makes the measured loudest moment reach the top of the band; (none) skips it.
    vignette controls edge darkening from 0 (disabled) to 1; 0.4 reproduces PI/5.
    crf >= 0 overrides the selected codec's CRF for legacy API prompts.
    Duration zero means the remainder of the audio. Fades affect video only,
    each lasting 0 to 3 seconds; zero disables the corresponding fade.
    Progress/cancellation callbacks run during measurement and encoding. Return the frame count.
    """
    if container not in CONTAINERS:
        raise ValueError(f"unknown container {container!r}")
    if container == "webm" and (codec != "vp9" or audio_codec != "opus"):
        raise ValueError("WebM requires VP9 video and Opus audio; choose vp9 and opus, or MP4")
    style = "(none)" if style == "none" else style
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}")
    if codec not in CODECS:
        raise ValueError(f"unknown codec {codec!r}")
    if audio_codec not in AUDIO_CODECS:
        raise ValueError(f"unknown audio codec {audio_codec!r}")
    if fps not in FRAME_RATES:
        raise ValueError(f"unsupported frame rate {fps!r}: choose 30 or 60")
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError(f"image is {width}x{height}: yuv420p needs a positive even width and height")
    color = color.strip()
    band_h = 0
    if style != "(none)":
        if style == "spline" and not 1 <= line_thickness <= 32:
            raise ValueError("line_thickness must be 1 to 32 pixels")
        if not isinstance(stereo_gap, int) or stereo_gap < 0 or (stereo_mirror and (width-stereo_gap)//2 < 128):
            raise ValueError("stereo_gap must leave at least 128 pixels per stereo half")
        if not COLOR.fullmatch(color):
            raise ValueError(f"color {color!r}: use 0xRRGGBB, #RRGGBB or an ffmpeg color name")
        band_h = 2 * round(height * band_height / 2)
        if not BAND_RANGE[0] <= band_height <= BAND_RANGE[1]:
            raise ValueError(f"band_height {band_height}: use {BAND_RANGE[0]} to {BAND_RANGE[1]} of the video height")
        if band_h < max(MIN_BAND_H, dot if style == "dots" else 0):
            raise ValueError(f"band_height {band_height} is {band_h}px of this {height}px image: too low for the band")
        if height < band_h + BOTTOM_MARGIN:
            raise ValueError(f"image is {height}px tall: the visualizer band needs {band_h + BOTTOM_MARGIN}px")
        if not GAIN_RANGE[0] <= gain <= GAIN_RANGE[1]:
            raise ValueError(f"gain {gain}: use {GAIN_RANGE[0]} to {GAIN_RANGE[1]}, 1.0 reaching the top of the band")
        if not 0 <= glow <= 1 or not 0 <= trail <= .99:
            raise ValueError("glow must be 0 to 1; trail must be 0 to 0.99")
        if style == "dots" and not 4 <= dot <= 64:
            raise ValueError("dot spacing must be 4 to 64 pixels")
    if not 0 <= vignette <= 1:
        raise ValueError(f"vignette {vignette}: use 0.0 to 1.0; 0 disables it")
    for name, value in (("fade_in", fade_in), ("fade_out", fade_out)):
        if not 0 <= value <= 3:
            raise ValueError(f"{name} {value}: use 0.0 to 3.0 seconds; 0 disables it")
    for name, value in (("start", start), ("duration", duration)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite nonnegative number of seconds")
    quality = crf if crf >= 0 else {"h264": h264_crf, "h265": h265_crf, "vp9": vp9_crf}[codec]
    bitrate = aac_bitrate if audio_codec == "aac" else audio_bitrate
    max_crf = 63 if codec == "vp9" else 51
    if not 0 <= quality <= max_crf:
        raise ValueError(f"CRF must be 0 to {max_crf}")
    if audio_codec != "flac" and not 32 <= bitrate <= 512:
        raise ValueError("audio bitrate must be 32 to 512 kbit/s")
    if sample_rate <= 0 or not 1 <= channels <= 8 or len(samples) % (4 * channels):
        raise ValueError("audio needs a positive sample rate and complete float32 samples in 1 to 8 channels")
    if len(pixels) != width * height * 3:
        raise ValueError("pixels must contain exactly width * height * 3 rgb24 bytes")
    if start >= len(samples) / (4 * channels * sample_rate):
        raise ValueError(f"start {start}s is past the end of the audio")
    audio = _Audio(samples, sample_rate, channels, start, duration)
    dur = len(audio) / audio.rate
    frames = int(dur * fps)
    if frames < 1:
        raise ValueError("duration is shorter than one output frame")
    fade_in, fade_out = min(fade_in, dur), min(fade_out, dur)
    check_features(codec, audio_codec, style, vignette, fade_in, fade_out)
    measure_units = int(frames * MEASURE_SHARE / (1 - MEASURE_SHARE)) if style != "(none)" else 0
    total, shown = frames + measure_units, 0

    def measuring(written, _frames):
        nonlocal shown
        shown = max(shown, written * measure_units // frames)
        if on_frame:
            on_frame(shown, total)

    if on_frame:
        on_frame(0, total)
    # Height grows as level ** (2/3), so gain g needs g ** 1.5 times full scale.
    analysis_width = (width-stereo_gap)//2 if stereo_mirror else width
    analysis_audio = _channel_audio(audio) if stereo_mirror else [audio]
    level = (min(full_scale(a, style, analysis_width, dot, fps, frames, measuring) for a in analysis_audio) * gain ** 1.5
             if style != "(none)" else 1.0)
    graph, sink, source = _composition(pixels, width, height, audio, style, band_h, dot, fps, level,
                                        color, glow, trail, vignette, fade_in, fade_out, stereo_mirror, stereo_gap, line_thickness)
    # Reserve a random sibling with normal output permissions, respecting the umask.
    # mkstemp's owner-only mode would survive os.replace and break shared outputs.
    # Close the fd before PyAV opens it, including on Windows.
    output = os.fspath(output)
    temporary = os.path.join(os.path.dirname(os.path.abspath(output)),
                             f".audio-viz-{os.urandom(16).hex()}.{container}")
    os.close(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
    try:
        options = ({"movflags": "+faststart+use_metadata_tags"} if container == "mp4"
                   else {"cues_to_front": "1"})
        with av.open(temporary, "w", format=container, options=options) as container:
            container.metadata.update(metadata or {})
            video, sound = _streams(container, width, height, fps, codec, quality, audio_codec, bitrate, audio)

            def encode_audio(frame):
                # AudioStream resamples to the encoder's sample format/rate.
                for packet in sound.encode(frame):
                    container.mux(packet)

            feed = _Feed(audio, source, encode_audio)
            for n in range(frames):
                # Encode audio alongside video; analysis requests extra lookahead via EAGAIN.
                while feed.position < len(audio) and feed.position / audio.rate < n / fps + .1:
                    feed.step()
                try:
                    frame = feed.read(sink)
                except av.error.EOFError:
                    raise RuntimeError(f"PyAV ended after {n} of {frames} video frames") from None
                frame.pts, frame.time_base = n, Fraction(1, fps)
                if n == math.ceil(fade_in * fps) + 1:
                    frame.pict_type = av.video.frame.PictureType.I
                for packet in video.encode(frame):
                    container.mux(packet)
                if on_frame:
                    on_frame(measure_units + n + 1, total)
            while not feed.eof:
                feed.step()
            for packet in video.encode():
                container.mux(packet)
            encode_audio(None)
        if on_frame:
            on_frame(total, total)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return frames
