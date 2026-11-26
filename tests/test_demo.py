"""The screenshot demo must work without reading real history or calling tools."""
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codex_hub import main
from hub_accounts import registry_snapshot
from hub_demo import DemoAccountService, DemoController, create_demo
from hub_threads import ThreadStore
from tests.test_resumer import wait_for_view


class DemoTests(unittest.TestCase):
    def test_fictional_data_covers_accounts_nested_projects_archives_and_conversation_search(self):
        with tempfile.TemporaryDirectory(prefix="accountdeck-demo-test-") as tmp:
            home = Path(tmp)
            create_demo(home)
            accounts = registry_snapshot(home)
            self.assertEqual(len(accounts), 4)
            self.assertTrue(all(a["email"].endswith("@example.com") for a in accounts))
            self.assertFalse((home / "auth.json").exists())
            self.assertEqual(list(home.rglob("*.auth.json")), [])
            store = ThreadStore(home)
            records = store.load()
            self.assertEqual(len(records), 9)
            self.assertEqual(sum(row.archived for row in records), 1)
            self.assertTrue(any(row.cwd.endswith("luna/door/camera") for row in records))
            self.assertEqual(store.update_index(records), 0)
            found, _ = store.search(records, "shutter", accounts)
            self.assertEqual([row.title for row in found], ["Tune thermal camera palettes"])
            service = DemoAccountService(home)
            credits = [service.fetch(account, "resets")["credits"] for account in accounts]
            self.assertTrue(any(not row for row in credits))
            self.assertTrue(any(row for row in credits))
            controller = DemoController(home)
            controller.switch(accounts[2])
            self.assertEqual(next(a for a in registry_snapshot(home) if a["active"])["account_key"], accounts[2]["account_key"])
            controller.login(device_auth=True)
            self.assertEqual(len(registry_snapshot(home)), 5)

    def test_demo_entrypoint_ignores_real_home_and_never_launches_a_command_or_usage_request(self):
        with tempfile.TemporaryDirectory(prefix="accountdeck-real-sentinel-") as tmp:
            original_home = Path(tmp)
            marker = original_home / "auth.json"
            marker.write_text("REAL PRIVATE DATA MUST NOT BE READ")
            previous_cwd = Path.cwd()
            observed = []
            def demo_loop(run):
                app = run.__self__
                observed.append(app.home)
                self.assertNotEqual(app.home, original_home)
                self.assertTrue(app.demo)
                app.screen = Mock()
                app.refresh()
                deadline = time.monotonic() + 3
                while app.loading and time.monotonic() < deadline:
                    app.finish_messages()
                    time.sleep(.01)
                self.assertFalse(app.loading)
                self.assertTrue(all(a["usage_live"] and a["resets_live"] for a in app.accounts))
                app.set_tab("threads")
                wait_for_view(app.resumer)
                self.assertEqual(len(app.resumer.visible), 8)
                app.resumer.query = "shutter"
                app.resumer.refresh(reload=False)
                wait_for_view(app.resumer)
                self.assertEqual(len(app.resumer.visible), 1)
                with patch.multiple("codex_hub.curses", def_prog_mode=Mock(), endwin=Mock(),
                                    reset_prog_mode=Mock(), flushinp=Mock()), patch("builtins.input", return_value=""), \
                        patch("sys.stdout", new=io.StringIO()):
                    app.resume_selected()
                self.assertEqual(app.status, "Returned from demo conversation")
                app.login(device_auth=True)
                self.assertEqual(len(app.accounts), 5)
                # The local renamer still works, with the network/process fallback blocked.
                identity = app.resumer.selected.id
                app.client.rename(identity, "Fictional renamed thread")
                self.assertEqual(next(row.title for row in app.resumer.store.load() if row.id == identity), "Fictional renamed thread")
            with patch("codex_hub.sys.stdin.isatty", return_value=True), \
                    patch("codex_hub.sys.stdout.isatty", return_value=True), \
                    patch("codex_hub.curses.wrapper", side_effect=demo_loop), \
                    patch("subprocess.Popen", side_effect=AssertionError("Demo spawned an external process")), \
                    patch("hub_accounts.AccountService.fetch", side_effect=AssertionError("Demo called the real usage service")), \
                    patch("hub_accounts.Controller.run", side_effect=AssertionError("Demo used the real account controller")):
                self.assertEqual(main(["--demo", "--codex-home", str(original_home),
                                       "--codex-bin", "/not-a-real-codex", "--auth-bin", "/not-a-real-auth"]), 0)
            self.assertEqual(Path.cwd(), previous_cwd)
            self.assertTrue(observed)
            self.assertFalse(observed[0].exists())
            self.assertEqual(marker.read_text(), "REAL PRIVATE DATA MUST NOT BE READ")


if __name__ == "__main__":
    unittest.main()
