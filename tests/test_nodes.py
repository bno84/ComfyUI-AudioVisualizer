"""The node's inputs without ComfyUI: the optional still and which lazy inputs it asks for."""
import importlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name, attrs in (("folder_paths", {}), ("comfy_execution", {}),
                    ("comfy_execution.graph_utils", {"ExecutionBlocker": object})):
    sys.modules.setdefault(name, types.ModuleType(name)).__dict__.update(attrs)
package = types.ModuleType("visualizer_test_pack")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
nodes = importlib.import_module(package.__name__ + ".nodes")


class StillTests(unittest.TestCase):
    def test_the_image_is_optional(self):
        types_ = nodes.AudioVisualizer.INPUT_TYPES()
        self.assertNotIn("image", types_["required"])
        self.assertTrue(types_["optional"]["image"][1]["lazy"])

    def test_asks_only_for_connected_inputs_that_are_still_missing(self):
        node = nodes.AudioVisualizer()
        # Unconnected: the key is absent, and asking for it would make ComfyUI raise.
        self.assertEqual(node.check_lazy_status(True, audio=None), ["audio"])
        self.assertEqual(node.check_lazy_status(True, image=None, audio=None), ["image", "audio"])
        self.assertEqual(node.check_lazy_status(True, image="still", audio="song"), [])
        self.assertEqual(node.check_lazy_status(False, image=None, audio=None), [])

    def test_the_default_still_ships_with_the_pack(self):
        data = nodes.DEFAULT_STILL.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual((int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")), (1280, 720))

    @unittest.skipUnless(importlib.util.find_spec("torch") and importlib.util.find_spec("PIL"), "needs torch and Pillow")
    def test_the_default_still_loads_as_an_image_batch(self):
        still = nodes.default_still()
        self.assertEqual(tuple(still.shape), (1, 720, 1280, 3))
        self.assertLessEqual(float(still.max()), 1.0)


if __name__ == "__main__":
    unittest.main()
