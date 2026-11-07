"""Keep the npm entry point portable and the published file allowlist narrow."""
import json
from pathlib import Path
import unittest

from codex_hub import VERSION

ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.package = json.loads((ROOT / "package.json").read_text())

    def test_python_and_npm_versions_match(self):
        self.assertEqual(self.package["version"], VERSION)
        self.assertEqual(self.package["name"], "codex-accountdeck")

    def test_entrypoint_runs_python_directly(self):
        binary = self.package["bin"]["codex-accountdeck"]
        self.assertEqual((ROOT / binary).read_text().splitlines()[0], "#!/usr/bin/env python3")
        self.assertTrue((ROOT / binary).stat().st_mode & 0o111)

    def test_no_install_hooks_and_only_app_files_are_packaged(self):
        self.assertEqual(set(self.package["files"]), {
            "codex-accountdeck", "codex_hub.py", "hub_accounts.py", "codex_resets.py",
            "codex_renamer.py", "codex_colors.py", "docs/preview.svg",
        })
        scripts = self.package.get("scripts", {})
        for lifecycle in ("preinstall", "install", "postinstall", "prepare"):
            self.assertNotIn(lifecycle, scripts)
        self.assertFalse(self.package.get("dependencies"))


if __name__ == "__main__":
    unittest.main()
