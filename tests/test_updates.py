import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from http.client import IncompleteRead
from unittest.mock import patch

from codex_hub import main
from hub_updates import ReleaseCheck, UpdateError, apply_update


def published_release(tag):
    return io.BytesIO(json.dumps({"tag_name": tag, "draft": False, "prerelease": False}).encode())


class ReleaseCheckTests(unittest.TestCase):
    def test_public_release_is_compared_numerically_and_reused_across_launches(self):
        with tempfile.TemporaryDirectory() as directory, patch("hub_updates.urllib.request.build_opener") as opener:
            request = opener.return_value.open
            request.return_value = published_release("v1.10.0")
            latest = ReleaseCheck(directory).check()
            self.assertTrue(latest.available("1.9.0"))
            self.assertFalse(latest.available("1.10.0"))
            self.assertFalse(latest.available("2.0.0"))
            self.assertEqual(ReleaseCheck(directory).check(), latest)
            request.assert_called_once()
            http_request = request.call_args.args[0]
            self.assertEqual(http_request.get_method(), "GET")
            self.assertEqual(http_request.full_url, "https://api.github.com/repos/nerdworldDE/codex-accountdeck/releases/latest")
            self.assertIsNone(http_request.get_header("Authorization"))

    def test_interrupted_http_response_keeps_previous_release(self):
        with tempfile.TemporaryDirectory() as directory, patch("hub_updates.urllib.request.build_opener") as opener:
            request = opener.return_value.open
            request.side_effect = [published_release("v1.5.0"), IncompleteRead(b'{"tag', 100)]
            check = ReleaseCheck(directory)
            check.check()
            failed = check.check(force=True)
            self.assertTrue(failed.failed)
            self.assertEqual(failed.latest, "1.5.0")
            self.assertEqual(ReleaseCheck(directory).check(), failed)
            self.assertEqual(request.call_count, 2)

    def test_check_command_works_without_a_terminal_or_account_registry(self):
        with tempfile.TemporaryDirectory() as directory, patch("hub_updates.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = published_release("v9.0.0")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--codex-home", directory, "--check-update"]), 0)
            self.assertIn("latest stable v9.0.0", output.getvalue())
            self.assertFalse((Path(directory) / "accounts").exists())

    def test_malformed_cache_and_github_response_do_not_crash_the_check(self):
        nested = b"[" * 1500 + b"null" + b"]" * 1500
        with tempfile.TemporaryDirectory() as directory, patch("hub_updates.urllib.request.build_opener") as opener:
            check = ReleaseCheck(directory)
            check.path.parent.mkdir()
            check.path.write_bytes(nested)
            opener.return_value.open.return_value = io.BytesIO(nested)
            result = check.check()
            self.assertTrue(result.failed)
            self.assertIsNone(result.latest)

    def test_version_requires_the_release_tag_used_for_downloads(self):
        with tempfile.TemporaryDirectory() as directory, patch("hub_updates.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = published_release("1.5.0")
            self.assertTrue(ReleaseCheck(directory).check(force=True).failed)


class CheckoutUpdateTests(unittest.TestCase):
    def test_checkout_updates_to_stable_tag_and_refuses_unsaved_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}):
            base = Path(directory)
            upstream, checkout = base / "upstream", base / "checkout"

            def git(root, *args):
                return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()

            upstream.mkdir()
            git(upstream, "init", "--initial-branch=main")
            git(upstream, "config", "user.name", "AccountDeck Test")
            git(upstream, "config", "user.email", "test@example.invalid")
            program = upstream / "codex_hub.py"
            program.write_text('print("codex-accountdeck 1.4.0")\n')
            git(upstream, "add", "codex_hub.py")
            git(upstream, "commit", "-m", "Previous release")
            git(base, "clone", str(upstream), str(checkout))
            official = "https://github.com/nerdworldDE/codex-accountdeck.git"
            git(checkout, "remote", "set-url", "origin", official)
            git(checkout, "config", f"url.{upstream.as_uri()}.insteadOf", official)
            program.write_text('print("codex-accountdeck 1.5.0")\n')
            git(upstream, "commit", "-am", "Stable release")
            git(upstream, "tag", "v1.5.0")
            program.write_text('print("codex-accountdeck 1.6.0")\n')
            git(upstream, "commit", "-am", "Unreleased development")

            draft = checkout / "notes.txt"
            draft.write_text("unsaved work")
            git(checkout, "config", "status.showUntrackedFiles", "no")
            with self.assertRaisesRegex(UpdateError, "checkout changes"):
                apply_update(checkout, "1.5.0")
            self.assertEqual(draft.read_text(), "unsaved work")
            draft.unlink()
            with self.assertRaisesRegex(UpdateError, "configured Git repository"):
                apply_update(checkout, "1.5.0")
            self.assertEqual((checkout / "codex_hub.py").read_text(), 'print("codex-accountdeck 1.4.0")\n')

            git(checkout, "remote", "set-url", "origin", upstream.as_uri())
            with redirect_stdout(io.StringIO()), patch("hub_updates.REPOSITORY", upstream.as_uri()):
                apply_update(checkout, "1.5.0")
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), git(upstream, "rev-parse", "v1.5.0"))
