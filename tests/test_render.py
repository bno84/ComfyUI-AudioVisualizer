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
            with self.subTest(options=options), patch.object(R, "check_ffmpeg"), patch.object(R.subprocess, "Popen") as popen:
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
            with self.subTest(options=options), patch.object(R, "check_ffmpeg"), patch.object(R.subprocess, "Popen") as popen:
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
                        self.assertEqual(progress[-1], (fps, fps))

    def test_rejects_unsupported_rate(self):
        with self.assertRaisesRegex(ValueError, "frame rate"):
            R.render(b"", 640, 144, b"", 48000, 1, "unused.mp4", fps=24)


if __name__ == "__main__":
    unittest.main()
