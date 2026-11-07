import json
from pathlib import Path
import unittest

from codex_colors import contrast, thread_color


class ColorTests(unittest.TestCase):
    def test_fixture_identifiers_are_synthetic(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/cli_colors_1337.json").read_text())
        for sample in fixture["samples"]:
            self.assertTrue(sample["id"].startswith("00000000-0000-4000-8000-"))

    def test_colors_match_native_cli_captures(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/cli_colors_1337.json").read_text())
        for sample in fixture["samples"]:
            with self.subTest(thread=sample["id"], background=sample["background"]):
                self.assertEqual(thread_color(sample["id"], tuple(sample["background"])), tuple(sample["rgb"]))

    def test_uuid_spelling_does_not_change_color(self):
        identifier = "01a12345-6789-7abc-8123-456789abcdef"
        self.assertEqual(thread_color(identifier), thread_color(identifier.upper()))

    def test_colors_remain_readable_on_light_and_dark_backgrounds(self):
        for background in ((0, 0, 0), (30, 30, 30), (128, 128, 128), (255, 255, 255)):
            for number in range(64):
                color = thread_color(f"019fbe01-2345-7000-8000-{number:012x}", background)
                self.assertGreaterEqual(contrast(color, background), 4.5)


if __name__ == "__main__":
    unittest.main()
