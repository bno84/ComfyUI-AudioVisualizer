"""Real library-filter and codec tests: no FFmpeg/FFprobe executable is required."""
import importlib.util
import math
import os
import stat
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import av
import numpy as np
# Windows/Python 3.10 may run an OS probe while importing these helpers.
# Import before setUp blocks subprocesses during renderer execution.
import numpy.testing as npt

spec = importlib.util.spec_from_file_location("visualizer_render", Path(__file__).resolve().parents[1] / "render.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def tone(seconds=1, rate=48000, channels=1):
    t = np.arange(round(seconds * rate)) / rate
    return np.stack([.2 * np.sin(2 * math.pi * (110 + 440*c) * t) for c in range(channels)], axis=1).astype("<f4")


def audio_samples(path):
    with av.open(str(path)) as source:
        stream = source.streams.audio[0]
        channels = len(stream.layout.channels)
        resampler = av.AudioResampler(format="flt", layout=stream.layout.name, rate=stream.sample_rate)
        chunks = [p.to_ndarray().reshape(-1, channels)
                  for frame in source.decode(audio=0) for p in resampler.resample(frame)]
        chunks += [p.to_ndarray().reshape(-1, channels) for p in resampler.resample(None)]
        return np.concatenate(chunks, axis=0)


class DependencyTest(unittest.TestCase):
    def test_missing_selected_encoder_and_filter(self):
        real = av.Codec
        def codec(name, mode):
            if name == "libx265":
                raise ValueError("unavailable")
            return real(name, mode)
        with patch.object(av, "Codec", side_effect=codec), \
                patch.object(av.filter, "filters_available", av.filter.filters_available - {"showcqt"}):
            with self.assertRaisesRegex(RuntimeError, "encoders: libx265; filters: showcqt"):
                R.check_features("h265", "aac", "bars")

    def test_none_needs_only_scene_filters_and_selected_encoders(self):
        real = av.Codec
        def codec(name, mode):
            self.assertIn(name, ("libx264", "aac"))
            return real(name, mode)
        with patch.object(av, "Codec", side_effect=codec), \
                patch.object(av.filter, "filters_available", R.SCENE_FILTERS):
            R.check_features("h264", "aac", "(none)")
            with self.assertRaisesRegex(RuntimeError, "filters: fade"):
                R.check_features("h264", "aac", "(none)", fade_in=1)
            with self.assertRaisesRegex(RuntimeError, "filters: vignette"):
                R.check_features("h264", "aac", "(none)", vignette=.2)


class RenderTest(unittest.TestCase):
    def setUp(self):
        # A regression to subprocess rendering fails, even when FFmpeg happens to be installed.
        self.process_guard = patch.object(subprocess, "Popen", side_effect=AssertionError("external process"))
        self.process_guard.start()
        self.addCleanup(self.process_guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def render(self, filename="video.mp4", width=640, height=144, data=None, rate=48000, **opts):
        data = tone() if data is None else data
        path = self.directory / filename
        written = R.render(bytes([180, 180, 180]) * width * height, width, height,
                           data.tobytes(), rate, data.shape[1], path,
                           **{"codec": "h264", "fps": 30, **opts})
        return path, written

    def test_styles_rates_codecs_metadata_and_progress(self):
        for style in R.STYLES:
            for fps in R.FRAME_RATES:
                for codec in R.AUDIO_CODECS:
                    with self.subTest(style=style, fps=fps, codec=codec):
                        progress = []
                        path, count = self.render(style=style, fps=fps, audio_codec=codec,
                                                  codec="h264" if fps == 30 else "h265",
                                                  metadata={"workflow": '{"test":true}'},
                                                  on_frame=lambda n, total: progress.append((n, total)))
                        self.assertEqual(count, fps)
                        with av.open(str(path)) as source:
                            v, a = source.streams.video[0], source.streams.audio[0]
                            self.assertEqual(v.average_rate, fps)
                            self.assertEqual(v.codec_context.name, "h264" if fps == 30 else "hevc")
                            self.assertEqual(a.codec_context.name, codec)
                            self.assertEqual(source.metadata["workflow"], '{"test":true}')
                            frames = list(source.decode(video=0))
                            self.assertEqual(len(frames), fps)
                            for n, frame in enumerate(frames):
                                self.assertAlmostEqual(float(frame.time), n / fps)
                        self.assertEqual(progress[-1][0], progress[-1][1])
                        self.assertEqual([p[0] for p in progress], sorted(p[0] for p in progress))
                        self.assertEqual(progress[-1][1] > fps, style != "(none)")

    def test_fades_and_unmodified_audio(self):
        data = tone(2, rate=8000)
        for fade_in, fade_out in ((0, 0), (.5, 0), (0, .5), (.5, .5), (3, 3)):
            with self.subTest(fade_in=fade_in, fade_out=fade_out):
                path, _ = self.render(width=64, height=48, rate=8000, data=data,
                                      style="(none)", crf=0, fade_in=fade_in, fade_out=fade_out)
                with av.open(str(path)) as source:
                    levels = [float(frame.to_ndarray(format="rgb24").mean()) for frame in source.decode(video=0)]
                self.assertEqual(len(levels), 60)
                self.assertAlmostEqual(levels[0], 0 if fade_in else 180, delta=3)
                if fade_out:
                    self.assertLess(levels[-1], 20)
                else:
                    self.assertAlmostEqual(levels[-1], 180, delta=3)
                if max(fade_in, fade_out) < 2:
                    self.assertAlmostEqual(levels[30], 180, delta=3)
                npt.assert_allclose(audio_samples(path), data, atol=1.2e-7)

    def test_trim_stereo_and_noninteger_duration(self):
        rate = 44100
        data = tone(2, rate, 2)
        start, duration = .137, .713
        path, frames = self.render(data=data, rate=rate, style="(none)", start=start, duration=duration)
        expected = data[round(start*rate):round(start*rate)+round(duration*rate)]
        self.assertEqual(frames, int(len(expected) / rate * 30))
        npt.assert_allclose(audio_samples(path), expected, atol=1.2e-7)
        with av.open(str(path)) as source:
            stream = source.streams.audio[0]
            self.assertAlmostEqual(float(stream.duration * stream.time_base), len(expected) / rate)

    def test_opus_resamples_44100_stereo_and_preserves_duration(self):
        path, _ = self.render(data=tone(1.013, 44100, 2), rate=44100, style="(none)", audio_codec="opus")
        with av.open(str(path)) as source:
            audio = source.streams.audio[0]
            self.assertEqual(audio.sample_rate, 48000)
            self.assertAlmostEqual(float(audio.duration * audio.time_base), 1.013, delta=1/48000)
            self.assertTrue(list(source.decode(audio=0)))

    def test_none_bypasses_spectrum_and_unused_controls(self):
        for style in ("none", "(none)"):
            with self.subTest(style=style), patch.object(R, "full_scale", side_effect=AssertionError()), \
                    patch.object(R, "_spectrum", side_effect=AssertionError()), \
                    patch.object(av.filter, "filters_available", R.SCENE_FILTERS):
                path, frames = self.render(width=16, height=16, style=style, fade_in=0, fade_out=0,
                                           color="unused", band_height=0, gain=99, dot=0)
                self.assertTrue(path.exists())
                self.assertEqual(frames, 30)

    def test_vignette_darkens_edges_and_zero_preserves_the_still(self):
        corners = []
        for strength in (0, .2, .4, 1):
            path, _ = self.render(style="(none)", data=tone(1), crf=0, vignette=strength, fade_in=0, fade_out=0)
            with av.open(str(path)) as source:
                frame = next(source.decode(video=0)).to_ndarray(format="rgb24")
            corners.append(float(frame[8, 8].mean()))
        self.assertAlmostEqual(corners[0], 180, delta=3)
        self.assertTrue(all(a > b for a, b in zip(corners, corners[1:])), corners)

    def test_gain_one_reaches_the_band_top_at_any_volume(self):
        rate, fps = 48000, 30
        t = np.arange(2*rate) / rate
        base = .5*np.sin(2*math.pi*110*t) + t/2*np.sin(2*math.pi*660*t)
        for style in ("bars", "dots"):
            for volume in (.0001, .05, .6):
                with self.subTest(style=style, volume=volume):
                    audio = R._Audio((volume*base).astype("<f4").tobytes(), rate, 1)
                    full = R.full_scale(audio, style, 640, 12, fps, 2*fps)
                    self.assertEqual(R._peak(audio, style, 640, 12, fps, full, 2*fps, None), 1)
                    smaller = R._peak(audio, style, 640, 12, fps, full*.97**1.5, 2*fps, None)
                    self.assertGreater(smaller, .95)
                    self.assertLess(smaller, 1)

    def test_silence_normalization_and_subsecond_clips(self):
        audio = R._Audio(bytes(4800*4), 48000, 1)
        for style in ("bars", "dots"):
            self.assertEqual(R.full_scale(audio, style, 640, 12, 60, 6), 1)
        for style in R.STYLES:
            for fps, seconds in ((30, .05), (60, .02), (60, .1)):
                with self.subTest(style=style, fps=fps, seconds=seconds):
                    _, count = self.render(data=tone(seconds), style=style, fps=fps)
                    self.assertEqual(count, int(seconds*fps))

    def test_band_height_glow_trail_and_color(self):
        data = tone(3)
        data[48000:] = 0
        audio = R._Audio(data.tobytes(), 48000, 1)
        def frames(style, glow=0, trail=0):
            graph, sink, source = R._composition(bytes([180]*3)*640*360, 640, 360,
                                                 audio, style, 62, 12, 30, 64, "#ff0000",
                                                 glow, trail, 0, 0, 0)
            feed = R._Feed(audio, source)
            selected = {}
            for n in range(90):
                frame = feed.read(sink)
                if n in (15, 89) or 33 <= n < 45:
                    selected[n] = frame.to_ndarray(format="rgb24")
            return selected
        for style in ("bars", "dots"):
            plain, glow, trail = frames(style), frames(style, glow=1), frames(style, trail=.8)
            self.assertTrue(np.any(plain[15][-65:] != 180))
            npt.assert_array_equal(plain[15][:-73], 180)
            self.assertGreater(np.abs(plain[15].astype(int)-glow[15]).max(), 0)
            self.assertGreater(max(np.abs(a.astype(int)-b).max() for a,b in ((plain[n], trail[n]) for n in range(33, 45))), 0)
            npt.assert_array_equal(plain[89], trail[89])

    def test_quality_controls_and_legacy_override(self):
        for options, expected in [
            ({}, 18), ({"codec": "h264"}, 22),
            ({"codec": "h264", "h264_crf": 20, "h265_crf": 16}, 20),
            ({"codec": "h265", "h264_crf": 20, "h265_crf": 16}, 16),
            ({"codec": "h264", "crf": 0}, 0), ({"codec": "h265", "crf": 25}, 25),
        ]:
            with self.subTest(options=options), patch.object(R, "_streams", wraps=R._streams) as streams:
                self.render(width=16, height=16, style="(none)", data=tone(.1), **{"codec": "h265", **options})
                self.assertEqual(streams.call_args.args[5], expected)
        for options, expected in [
            ({"audio_codec": "aac"}, 192),
            ({"audio_codec": "aac", "aac_bitrate": 256, "audio_bitrate": 64}, 256),
            ({"audio_codec": "opus", "aac_bitrate": 256}, 128),
        ]:
            with self.subTest(options=options), patch.object(R, "_streams", wraps=R._streams) as streams:
                self.render(width=16, height=16, style="(none)", data=tone(.1), **options)
                self.assertEqual(streams.call_args.args[7], expected)

    def test_large_metadata_unicode_filename_and_post_fade_keyframe(self):
        metadata = {"workflow": '{"payload":"' + "x"*524288 + '"}', "prompt": "音楽 café\n#=;\\"}
        for codec in R.CODECS:
            path, _ = self.render(filename="音楽 café [test].mp4", codec=codec, style="(none)",
                                  data=tone(2), fps=60, fade_in=.5, fade_out=0, metadata=metadata)
            with av.open(str(path)) as source:
                self.assertEqual(source.metadata["workflow"], metadata["workflow"])
                self.assertEqual(source.metadata["prompt"], metadata["prompt"])
                frames = list(source.decode(video=0))
                self.assertTrue(frames[31].key_frame)
                if codec == "h265":
                    self.assertEqual(source.streams.video[0].codec_context.codec_tag, "hvc1")

    def test_cleanup_on_cancel_and_encoder_failure_preserves_existing_output(self):
        class Cancelled(BaseException):
            pass
        path = self.directory / "video.mp4"
        path.write_bytes(b"existing")
        def interrupt(n, total):
            if n > 5:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            self.render(style="(none)", on_frame=interrupt)
        self.assertEqual(path.read_bytes(), b"existing")
        self.assertFalse(list(self.directory.glob(".audio-viz-*")))
        with patch.object(R, "_streams", side_effect=RuntimeError("encoder failure")):
            with self.assertRaisesRegex(RuntimeError, "encoder failure"):
                self.render(style="(none)")
        self.assertEqual(path.read_bytes(), b"existing")
        self.assertFalse(list(self.directory.glob(".audio-viz-*")))

    @unittest.skipIf(os.name == "nt", "POSIX umask semantics")
    def test_output_permissions_follow_umask(self):
        previous = os.umask(0o027)
        try:
            path, _ = self.render(width=16, height=16, style="(none)", data=tone(.1))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        finally:
            os.umask(previous)

    def test_eight_channel_audio_in_all_styles_and_codecs(self):
        data = tone(.2, channels=8)
        for style in R.STYLES:
            for codec in R.AUDIO_CODECS:
                with self.subTest(style=style, audio_codec=codec):
                    path, count = self.render(data=data, style=style, audio_codec=codec,
                                              fade_in=0, fade_out=0)
                    self.assertEqual(count, 6)
                    with av.open(str(path)) as source:
                        self.assertEqual(source.streams.audio[0].layout.name, "7.1")
                        self.assertTrue(list(source.decode(audio=0)))
                    if codec == "flac":
                        npt.assert_allclose(audio_samples(path), data, atol=1.2e-7)

    def test_measurement_cancellation_does_not_create_an_output(self):
        def interrupt(n, total):
            if n:
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            self.render(on_frame=interrupt)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_inputs_fail_without_creating_an_output(self):
        for options, message in [
            ({"vignette": float("nan")}, "vignette"),
            ({"fade_in": 3.1}, "fade_in"), ({"fade_out": float("inf")}, "fade_out"),
            ({"band_height": .1}, "too low"), ({"band_height": .16, "style": "dots", "dot": 40}, "too low"),
            ({"gain": 12}, "0.25 to 2.0"), ({"band_height": 16}, "0.1 to 0.9"),
            ({"fps": 24}, "frame rate"), ({"start": -1}, "start"),
            ({"duration": float("nan")}, "duration"), ({"start": 2}, "past the end"),
            ({"duration": .001}, "shorter than one"), ({"crf": 52}, "CRF"),
        ]:
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                self.render(**options)
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
