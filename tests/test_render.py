"""Exercise the actual ffmpeg output, without importing ComfyUI or using the GPU."""
import array
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PATH = Path(__file__).resolve().parents[1] / "render.py"
spec = importlib.util.spec_from_file_location("audio_visualizer_render", PATH)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


class DependencyTest(unittest.TestCase):
    def tearDown(self):
        R._ffmpeg_features.cache_clear()

    def test_missing_executable_explains_installation(self):
        R._ffmpeg_features.cache_clear()
        with patch.object(R.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "FFmpeg executable on PATH"):
                R.check_ffmpeg("h264", "aac")

    def test_missing_selected_encoder_and_filter(self):
        features = {"encoders": {"libx264", "aac"}, "filters": R.FILTERS - {"showcqt"}}
        with patch.object(R, "_ffmpeg_features", side_effect=features.__getitem__):
            with self.assertRaisesRegex(RuntimeError, "encoders: libx265; filters: showcqt"):
                R.check_ffmpeg("h265", "aac")

    def test_unselected_encoders_are_not_required(self):
        features = {"encoders": {"libx264", "aac"}, "filters": R.FILTERS}
        with patch.object(R, "_ffmpeg_features", side_effect=features.__getitem__):
            R.check_ffmpeg("h264", "aac")

    def test_capability_query_failure_is_actionable(self):
        R._ffmpeg_features.cache_clear()
        with patch.object(R.shutil, "which", return_value="/ffmpeg"), \
                patch.object(R.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 15)):
            with self.assertRaisesRegex(RuntimeError, "could not inspect FFmpeg"):
                R.check_ffmpeg("h264", "aac")


class BitrateTest(unittest.TestCase):
    def test_codec_quality_and_legacy_override(self):
        for options, expected in [
            ({}, "18"),
            ({"codec": "h264"}, "22"),
            ({"codec": "h264", "h264_crf": 20, "h265_crf": 16}, "20"),
            ({"codec": "h265", "h264_crf": 20, "h265_crf": 16}, "16"),
            ({"codec": "h264", "crf": 0}, "0"),
            ({"codec": "h265", "crf": 25}, "25"),
        ]:
            with self.subTest(options=options), patch.object(R, "check_ffmpeg"), \
                    patch.object(R, "full_scale", return_value=1.0), patch.object(R.subprocess, "Popen") as popen:
                process = MagicMock()
                process.stdout.__iter__.return_value = iter([])
                process.wait.return_value = 0
                popen.return_value = process
                R.render(b"", 640, 144, bytes(48000 * 4), 48000, 1, "unused.mp4", **options)
                command = popen.call_args.args[0]
                self.assertEqual(command[command.index("-crf") + 1], expected)

    def test_defaults_and_independent_bitrates(self):
        for options, encoder, bitrate in [
            ({}, "flac", None),
            ({"audio_codec": "aac"}, "aac", "192k"),
            ({"audio_codec": "aac", "aac_bitrate": 256, "audio_bitrate": 64}, "aac", "256k"),
            ({"audio_codec": "opus", "aac_bitrate": 256}, "libopus", "128k"),
        ]:
            with self.subTest(options=options), patch.object(R, "check_ffmpeg"), \
                    patch.object(R, "full_scale", return_value=1.0), patch.object(R.subprocess, "Popen") as popen:
                process = MagicMock()
                process.stdout.__iter__.return_value = iter([])
                process.wait.return_value = 0
                popen.return_value = process
                R.render(b"", 640, 144, bytes(48000 * 4), 48000, 1, "unused.mp4", **options)
                command = popen.call_args.args[0]
                self.assertEqual(command[command.index("-c:a") + 1], encoder)
                if bitrate:
                    self.assertEqual(command[command.index("-b:a") + 1], bitrate)
                else:
                    self.assertNotIn("-b:a", command)


class VignetteValidationTest(unittest.TestCase):
    def test_rejects_invalid_strength(self):
        for strength in (-0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(strength=strength), self.assertRaisesRegex(ValueError, "vignette"):
                R.render(b"", 640, 144, b"", 48000, 1, "unused.mp4", vignette=strength)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe")
class RenderTest(unittest.TestCase):
    def test_rates_codecs_styles_and_metadata(self):
        rate = 48000
        samples = array.array("f", (0.2 * math.sin(2 * math.pi * 440 * i / rate) for i in range(rate)))
        with tempfile.TemporaryDirectory() as directory:
            for fps in (30, 60):
                for audio_codec in R.AUDIO_CODECS:
                    with self.subTest(fps=fps, audio_codec=audio_codec):
                        output = str(Path(directory) / "viz.mp4")
                        progress = []
                        R.render(bytes([100, 80, 60]) * (640 * 144), 640, 144,
                                 samples.tobytes(), rate, 1, output,
                                 fps=fps, codec="h264" if fps == 30 else "h265",
                                 style="bars" if fps == 30 else "dots", audio_codec=audio_codec,
                                 metadata={"workflow": '{"test":true}'},
                                 on_frame=lambda written, total: progress.append((written, total)))
                        data = json.loads(subprocess.check_output([
                            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", output]))
                        video, audio = data["streams"]
                        self.assertEqual(video["r_frame_rate"], f"{fps}/1")
                        self.assertEqual(int(video["nb_frames"]), fps)
                        self.assertEqual(video["codec_name"], "h264" if fps == 30 else "hevc")
                        self.assertEqual(audio["codec_name"], audio_codec)
                        self.assertEqual(data["format"]["tags"]["workflow"], '{"test":true}')
                        # One bar that measures first, then encodes; it never moves back and ends full.
                        values = [written for written, _ in progress]
                        self.assertEqual(values, sorted(values))
                        self.assertEqual(progress[-1][0], progress[-1][1])
                        self.assertGreater(progress[-1][1], fps)

    def test_vignette_strength_changes_edge_darkening_and_zero_preserves_the_still(self):
        rate, width, height = 8000, 640, 144
        samples = bytes(5 * rate * 4)  # silence: spectrum cannot affect pixel checks
        corners = []
        with tempfile.TemporaryDirectory() as directory:
            for strength in (0, 0.2, R.DEFAULT_VIGNETTE, 1):
                output = str(Path(directory) / "vignette.mp4")
                with patch.object(R, "full_scale", return_value=1.0):
                    R.render(bytes([200, 200, 200]) * (width * height), width, height,
                             samples, rate, 1, output, fps=30, codec="h264", crf=0, vignette=strength)
                # At 2s both fades are inactive. Sample above the visualizer band.
                frame = subprocess.check_output([
                    "ffmpeg", "-v", "error", "-ss", "2", "-i", output, "-frames:v", "1",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
                offset = (8 * width + 8) * 3
                corner = sum(frame[offset:offset + 3]) / 3
                corners.append(corner)
                if strength == 0:
                    self.assertAlmostEqual(corner, 200, delta=3)
        self.assertTrue(all(a > b for a, b in zip(corners, corners[1:])), corners)

    def test_gain_one_makes_the_loudest_moment_reach_the_top_at_any_volume(self):
        rate, fps = 48000, 30
        with tempfile.TemporaryDirectory() as directory:
            for volume in (0.05, 0.6):
                # Bass and a swelling melody note: the loudest moment is late and narrow.
                def sample(i):
                    swell = i / (2 * rate)
                    return volume * (0.5 * math.sin(2 * math.pi * 110 * i / rate)
                                     + swell * math.sin(2 * math.pi * 660 * i / rate))
                samples = array.array("f", map(sample, range(2 * rate)))
                path = Path(directory) / f"audio{volume}.f32"
                path.write_bytes(samples.tobytes())
                inputs = ["-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", str(path)]
                for style in R.STYLES:
                    with self.subTest(volume=volume, style=style):
                        full = R.full_scale(inputs, style, 640, 12, fps, 2 * fps)
                        peak = lambda gain: R._peak(inputs, style, 640, 12, fps, full * gain ** 1.5, 2 * fps, None)
                        self.assertEqual(peak(1.0), 1.0)
                        self.assertLess(peak(0.97), 1.0)
                        self.assertGreater(peak(0.97), 0.95)

    def test_band_height_is_a_share_of_the_video(self):
        rate = 48000
        samples = array.array("f", (0.2 * math.sin(2 * math.pi * 440 * i / rate) for i in range(rate)))
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "viz.mp4")
            R.render(bytes([100, 80, 60]) * (640 * 360), 640, 360, samples.tobytes(), rate, 1, output,
                     fps=30, codec="h264", band_height=0.17, gain=1.15)   # 61.2px rounds to an even 62
            frames = subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                                              "stream=nb_read_frames", "-of", "csv=p=0", output], text=True)
            self.assertEqual(frames.split()[0], "30")

    def test_rejects_a_band_too_low_and_a_gain_without_height(self):
        for options, message in [
            ({"band_height": 0.1}, "too low"),
            ({"band_height": 0.16, "style": "dots", "dot": 40}, "too low"),
            ({"band_height": 0.16, "gain": 12}, "0.25 to 2.0"),
            ({"band_height": 16}, "0.1 to 0.9"),
        ]:
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                R.render(b"", 640, 144, bytes(4), 48000, 1, "unused.mp4", **{"gain": 1.15, **options})

    def test_rejects_unsupported_rate(self):
        with self.assertRaisesRegex(ValueError, "frame rate"):
            R.render(b"", 640, 144, b"", 48000, 1, "unused.mp4", fps=24)


if __name__ == "__main__":
    unittest.main()
