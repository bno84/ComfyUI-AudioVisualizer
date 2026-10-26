"""CPU FFmpeg filters and encoding through PyAV; no executable or ComfyUI imports."""
import math
import os
import re
import tempfile
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
STYLES = ("(none)", "bars", "dots")
DEFAULT_FADE_IN = 1.5
DEFAULT_FADE_OUT = 2.5
CODECS = ("h265", "h264")
AUDIO_CODECS = ("flac", "opus", "aac")
DEFAULT_CRF = {"h265": 18, "h264": 22}
COLOR = re.compile(r"^(0x|#)?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$|^[A-Za-z]+$")
VIDEO_ENCODERS = {"h264": "libx264", "h265": "libx265"}
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
                           + ". Install the pack requirements in ComfyUI's Python environment "
                           "using a standard PyAV wheel, or choose an available style/codec. "
                           "Installing an ffmpeg executable does not change PyAV's libraries.")


def _chain(graph, source, *filters):
    for name, args in filters:
        target = graph.add(name, args)
        source.link_to(target)
        source = target
    return source


class _Audio:
    """A view over the input bytes. Only the current chunk is copied to planar floats."""
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
        chunk = np.ascontiguousarray(self.data[position:position + CHUNK_SAMPLES].T, dtype=np.float32)
        frame = av.AudioFrame.from_ndarray(chunk, format="fltp", layout=self.layout)
        frame.sample_rate, frame.time_base, frame.pts = self.rate, Fraction(1, self.rate), position
        return frame

    def source(self, graph):
        return graph.add_abuffer(sample_rate=self.rate, format="fltp", layout=self.layout,
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
    """Keep the original measured-gain algorithm and silence fallback."""
    level = 4.0
    for _ in range(8):
        peak = _peak(audio, style, width, dot, fps, level, frames, on_frame)
        if peak >= 1.0:
            level /= 8
        elif peak < 0.2:
            level *= 64 if peak == 0 else (0.6 / peak) ** 1.5
        else:
            return level * peak ** -1.5
    return 1.0


def _composition(pixels, width, height, audio, style, band_h, dot, fps, level, color,
                 glow, trail, vignette, fade_in, fade_out):
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
           fade_in=DEFAULT_FADE_IN, fade_out=DEFAULT_FADE_OUT):
    """Render MP4 using CPU library filters. Inputs remain rgb24 and interleaved f32le bytes.

    Duration zero means the remainder of the audio. Fades affect video only.
    Progress/cancellation callbacks run during measurement and encoding. Return the frame count.
    """
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
    quality = crf if crf >= 0 else {"h264": h264_crf, "h265": h265_crf}[codec]
    bitrate = aac_bitrate if audio_codec == "aac" else audio_bitrate
    if not 0 <= quality <= 51:
        raise ValueError("CRF must be 0 to 51")
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
    level = (full_scale(audio, style, width, dot, fps, frames, measuring) * gain ** 1.5
             if style != "(none)" else 1.0)
    graph, sink, source = _composition(pixels, width, height, audio, style, band_h, dot, fps, level,
                                        color, glow, trail, vignette, fade_in, fade_out)
    # Close the temporary output fd before PyAV opens it: Windows disallows a second
    # open while a NamedTemporaryFile is held. Publish only a fully closed MP4.
    output = os.fspath(output)
    fd, temporary = tempfile.mkstemp(prefix=".audio-viz-", suffix=".mp4",
                                     dir=os.path.dirname(os.path.abspath(output)))
    os.close(fd)
    try:
        with av.open(temporary, "w", format="mp4", options={"movflags": "+faststart+use_metadata_tags"}) as container:
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
