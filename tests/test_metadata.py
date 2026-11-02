"""Embedded workflow, prompt and optional player info, without ComfyUI."""
import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("visualizer_metadata", Path(__file__).resolve().parents[1] / "metadata.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


class VideoMetadataTest(unittest.TestCase):
    def test_workflow_and_prompt_are_json_and_player_info_is_plain_text(self):
        metadata = M.video_metadata({"workflow": {"nodes": []}}, {"3": {"inputs": {}}},
                                    title=" Neon Lane ", artist="bno84", comment="trance, 158 BPM", lyrics="[verse]\nNeon")
        self.assertEqual(metadata, {"workflow": json.dumps({"nodes": []}), "prompt": json.dumps({"3": {"inputs": {}}}),
                                    "title": "Neon Lane", "artist": "bno84", "comment": "trance, 158 BPM",
                                    "lyrics": "[verse]\nNeon"})

    def test_unconnected_or_empty_player_info_writes_nothing(self):
        self.assertEqual(M.video_metadata({"workflow": {}}, None, title="", artist="  ", comment=None),
                         {"workflow": "{}"})
        self.assertIsNone(M.video_metadata())
        self.assertEqual(M.video_metadata(title="Neon"), {"title": "Neon"})
