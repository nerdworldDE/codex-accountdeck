import argparse
import base64
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from hub_accounts import (AccountService, CommandError, Controller, expires_in, merge_local,
                          registry_snapshot, remaining_percent, reset_count, usable_credits)
from install_hub import install
from codex_hub import HubApp, load_renamer

PROJECT = Path(__file__).resolve().parent
RENAMER = PROJECT
UTC = timezone.utc


def make_fixture(home):
    now = datetime.now(UTC)
    records = []
    (home / "accounts").mkdir(parents=True)
    for i in range(4):
        account = {"account_key": f"user-{i}::workspace", "email": f"account-{i + 1}@example.org",
                   "alias": None, "plan": "business", "last_usage_at": now.timestamp(),
                   "last_usage": {"primary": {"used_percent": 18 + i * 25, "window_minutes": 300,
                                               "resets_at": (now + timedelta(hours=3)).timestamp()},
                                  "secondary": {"used_percent": 30 + i * 20, "window_minutes": 10080,
                                                "resets_at": (now + timedelta(days=3)).timestamp()},
                                  "reset_credits": 2}}
        records.append(account)
        key = base64.urlsafe_b64encode(account["account_key"].encode()).decode().rstrip("=")
        (home / "accounts" / f"{key}.auth.json").write_text(json.dumps({
            "tokens": {"access_token": f"secret-{i}", "account_id": "workspace"}}))
    (home / "accounts/registry.json").write_text(json.dumps({"schema_version": 4,
        "active_account_key": records[0]["account_key"], "accounts": records}))
    database = sqlite3.connect(home / "state_5.sqlite")
    database.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, cwd TEXT, "
                     "updated_at INTEGER, source TEXT, archived INTEGER)")
    for i in range(3):
        database.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, 0)",
                         (f"thread-{i}", f"Original thread {i}", "A preview", f"/projects/project-{i}",
                          int(now.timestamp()) - i, "cli"))
    database.commit()
    database.close()
    fake = home / "fake-cli"
    fake.write_text('''#!/usr/bin/python3
import base64,json,os,pathlib,sys
home=pathlib.Path(os.environ['CODEX_HOME'])
with (home/'commands.jsonl').open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='switch':
 p=home/'accounts/registry.json'; r=json.loads(p.read_text()); r['active_account_key']=sys.argv[2]; p.write_text(json.dumps(r))
 print(json.dumps({'switched_to':{'account_key':sys.argv[2]}}))
elif sys.argv[1]=='login':
 print('FAKE LOGIN READY',flush=True)
 try: answer=input('Finish sign-in [Enter]: ')
 except KeyboardInterrupt: sys.exit(130)
 if answer=='fail': sys.exit(1)
 p=home/'accounts/registry.json'; r=json.loads(p.read_text()); i=len(r['accounts'])
 account=dict(r['accounts'][0]); account.update(account_key=f'user-{i}::workspace',email=f'account-{i+1}@example.org')
 r['accounts'].append(account); r['active_account_key']=account['account_key']; p.write_text(json.dumps(r))
 key=base64.urlsafe_b64encode(account['account_key'].encode()).decode().rstrip('=')
 (home/'accounts'/f'{key}.auth.json').write_text(json.dumps({'tokens':{'access_token':f'secret-{i}','account_id':'workspace'}}))
 print('Fake sign-in complete',flush=True)
else: print('{}')
''')
    fake.chmod(0o755)
    return fake


def live_fetch(self, account, section):
    """UI fixture; no network and no real credentials."""
    now = datetime.now(UTC)
    result = {"account_key": account["account_key"]}
    if section == "usage":
        result.update(usage=account["usage"], usage_live=True, usage_error=None, usage_at=now.timestamp())
    else:
        i = int(account["account_key"].split("::")[0].split("-")[-1])
        result.update(credits=[{"id": f"credit-{i}-{days}", "title": "Full reset",
                                "granted_at": (now - timedelta(days=20)).isoformat(),
                                "expires_at": (now + timedelta(days=days)).isoformat()}
                               for days in (i + .5, 12)],
                      reset_count=2, resets_live=True, resets_error=None, resets_at=now.timestamp())
    return result


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-hub-test-")
        self.home = Path(self.tmp.name)
        self.fake = make_fixture(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_usage_schema_reset_at_and_finite_capacity(self):
        account = registry_snapshot(self.home)[0]
        expiry = int(datetime.now(UTC).timestamp()) + 1000
        usage = {"rate_limit": {"primary_window": {"used_percent": 62, "reset_at": expiry,
                                                     "limit_window_seconds": 18000},
                                 "secondary_window": {"used_percent": 90, "reset_at": expiry,
                                                       "limit_window_seconds": 604800}}}
        service = AccountService(self.home)
        with patch.object(service, "_get", return_value=usage):
            result = service.fetch(account, "usage")
        self.assertEqual(result["usage"]["primary"]["resets_at"], expiry)
        self.assertEqual(result["usage"]["primary"]["window_minutes"], 300)
        self.assertEqual(remaining_percent(result["usage"]["primary"]), 38)
        self.assertNotIn("secret-", json.dumps(result))
        self.assertIsNone(remaining_percent({"used_percent": float("nan")}))
        self.assertEqual(remaining_percent({"used_percent": 999}), 0)
        self.assertEqual(remaining_percent({"used_percent": 100, "resets_at": 1}), 100)

    def test_expired_resets_and_unknown_expiry(self):
        now = datetime.now(UTC)
        account = {"credits": [{"expires_at": (now - timedelta(days=1)).isoformat()},
                               {"expires_at": None}, {"expires_at": (now + timedelta(days=2)).isoformat()}]}
        self.assertEqual(reset_count(account), 2)
        self.assertIsNone(expires_in(usable_credits(account)[-1]))
        self.assertIsNone(reset_count({"credits": None}))

    def test_first_run_without_registry_shows_an_empty_account_list(self):
        self.assertEqual(registry_snapshot(self.home / "new-install"), [])

    def test_partial_failure_keeps_last_successful_data(self):
        account = registry_snapshot(self.home)[0]
        service = AccountService(self.home)
        with patch.object(service, "_get", side_effect=urllib.error.HTTPError("", 401, "", {}, None)):
            failed = service.fetch(account, "usage")
        account.update(failed)
        self.assertEqual(account["usage"]["primary"]["used_percent"], 18)
        self.assertIn("Sign-in expired", account["usage_error"])
        self.assertFalse(account["usage_live"])
        service_data = {"credits": [], "available_count": 0}
        with patch.object(service, "_get", return_value=service_data):
            account.update(service.fetch(account, "resets"))
        self.assertTrue(account["resets_live"])
        self.assertEqual(account["credits"], [])

    def test_active_token_rotation_never_crosses_user_identity(self):
        account = registry_snapshot(self.home)[0]
        def token(user, suffix):
            body = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {
                "chatgpt_user_id": user}}).encode()).decode().rstrip("=")
            return "header." + body + "." + suffix
        stored = self.home / "accounts" / (base64.urlsafe_b64encode(account["account_key"].encode()).decode().rstrip("=") + ".auth.json")
        stored.write_text(json.dumps({"tokens": {"access_token": token("user-0", "stored"), "account_id": "workspace"}}))
        live = self.home / "auth.json"
        live.write_text(json.dumps({"tokens": {"access_token": token("user-1", "live"), "account_id": "workspace"}}))
        service = AccountService(self.home)
        self.assertTrue(service._tokens(account)["access_token"].endswith("stored"))
        live.write_text(json.dumps({"tokens": {"access_token": token("user-0", "live"), "account_id": "workspace"}}))
        self.assertTrue(service._tokens(account)["access_token"].endswith("live"))

    def test_merging_updates_activation_without_losing_live_resets(self):
        old = registry_snapshot(self.home)
        old[0]["credits"] = [{"expires_at": None}]
        old[0]["resets_live"] = True
        new = registry_snapshot(self.home)
        new[0]["active"] = False
        result = merge_local(new, old)
        self.assertFalse(result[0]["active"])
        self.assertEqual(result[0]["credits"], [{"expires_at": None}])

    def test_real_subprocess_switch_then_start_uses_isolated_home(self):
        controller = Controller(self.home, str(self.fake), str(self.fake))
        account = registry_snapshot(self.home)[2]
        result = controller.switch(account)
        commands = [json.loads(line) for line in (self.home / "commands.jsonl").read_text().splitlines()]
        self.assertEqual(commands, [["switch", account["account_key"], "--json"], ["app-server", "daemon", "start"]])
        self.assertTrue(next(a for a in registry_snapshot(self.home) if a["account_key"] == account["account_key"])["active"])
        self.assertIn("is active", result)

    def test_switch_failure_never_restarts_daemon(self):
        controller = Controller(self.home)
        with patch.object(controller, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, '{"error":{}}', "failure")
            with self.assertRaisesRegex(CommandError, "switch failed"):
                controller.switch(registry_snapshot(self.home)[0])
            self.assertEqual(run.call_count, 1)

    def test_login_inherits_the_terminal_and_supports_device_auth(self):
        controller = Controller(self.home)
        for device_auth in (False, True):
            with self.subTest(device_auth=device_auth), patch("hub_accounts.subprocess.Popen") as spawn:
                spawn.return_value.__enter__.return_value.wait.return_value = 0
                self.assertTrue(controller.login(device_auth))
                expected = ["codex-auth", "login"] + (["--device-auth"] if device_auth else [])
                self.assertEqual(spawn.call_args.args[0], expected)
                self.assertEqual(spawn.call_args.kwargs["env"]["CODEX_HOME"], str(self.home.resolve()))
                for stream in ("stdin", "stdout", "stderr"):
                    self.assertNotIn(stream, spawn.call_args.kwargs)

    def test_login_interrupt_reaps_child_and_reports_cancelled(self):
        with patch("hub_accounts.subprocess.Popen") as spawn:
            process = spawn.return_value.__enter__.return_value
            process.wait.side_effect = [KeyboardInterrupt, 0]
            self.assertFalse(Controller(self.home).login())
            process.terminate.assert_called_once()

    def test_login_failure_is_reported(self):
        with patch("hub_accounts.subprocess.Popen") as spawn:
            spawn.return_value.__enter__.return_value.wait.return_value = 1
            with self.assertRaisesRegex(CommandError, "Login did not complete"):
                Controller(self.home).login()

    def test_restart_failure_reports_account_already_changed(self):
        controller = Controller(self.home)
        account = registry_snapshot(self.home)[0]
        control = self.home / "app-server-control/app-server-control.sock"
        control.parent.mkdir()
        control.touch()
        with patch.object(controller, "run", side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps({"switched_to": account}), ""),
            subprocess.CompletedProcess([], 1, "", "daemon failure")]) as run:
            with self.assertRaisesRegex(CommandError, "Account switched.*restart failed"):
                controller.switch(account)
            self.assertEqual(run.call_args.args[0][-1], "restart")

    def test_installer_preserves_unrelated_profile_lines_and_backs_up(self):
        profile = self.home / ".profile"
        original = "# keep this\nexport EXAMPLE=hello\nalias sw=\"old command\"\nalias cr=old-renamer\n"
        profile.write_text(original)
        launcher = install(self.home, PROJECT, shortcuts=True)
        updated = profile.read_text()
        self.assertIn("export EXAMPLE=hello", updated)
        self.assertIn("alias sw='codex-accountdeck'", updated)
        self.assertIn("alias cr='codex-accountdeck --renamer'", updated)
        self.assertEqual(next(self.home.glob(".profile.before-codex-accountdeck-*")).read_text(), original)
        self.assertTrue(os.access(launcher, os.X_OK))

    def test_installer_leaves_profile_untouched_by_default(self):
        profile = self.home / ".profile"
        profile.write_text("alias sw='existing-command'\n")
        launcher = install(self.home, PROJECT)
        self.assertEqual(profile.read_text(), "alias sw='existing-command'\n")
        self.assertEqual(launcher.name, "codex-accountdeck")


@unittest.skipUnless((RENAMER / "codex_renamer.py").exists(), "Local renamer unavailable")
class LayoutTests(unittest.TestCase):
    def test_cancelled_login_restores_terminal_and_invalidates_old_responses(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fake = make_fixture(home)
            args = argparse.Namespace(codex_home=home, renamer=False, limit=200, codex_bin=str(fake),
                                      auth_bin=str(fake), offline=False, warning_days=7, interval=180)
            app = HubApp(args, load_renamer(RENAMER))
            app.screen = Mock()
            app.reload_local()
            app.loading.add((app.accounts[0]["account_key"], "usage"))
            app.messages.put(("account", app.generation, "usage", {
                "account_key": app.accounts[0]["account_key"], "usage_error": "stale response"}))
            try:
                with patch.multiple("codex_hub.curses", def_prog_mode=Mock(), endwin=Mock(),
                                    reset_prog_mode=Mock(), flushinp=Mock()), \
                        patch.object(app.controller, "login", return_value=False), \
                        patch.object(app, "refresh"), patch.object(app, "check_daemon"), \
                        patch.object(app, "start_action") as start, patch("sys.stdout", new=io.StringIO()):
                    app.login()
                    app.finish_messages()
                self.assertFalse(app.action)
                self.assertFalse(app.loading)
                self.assertIn("cancelled", app.status)
                self.assertIsNone(app.accounts[0]["usage_error"])
                app.screen.keypad.assert_called_with(True)
                app.screen.timeout.assert_called_with(80)
                app.screen.clearok.assert_called_with(True)
                start.assert_not_called()
            finally:
                app.close()

    def test_all_four_accounts_fit_in_80_by_24(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fake = make_fixture(home)
            renamer = load_renamer(RENAMER)
            args = argparse.Namespace(codex_home=home, renamer=False, limit=200, codex_bin=str(fake),
                                      auth_bin=str(fake), offline=True, warning_days=7, interval=180)
            app = HubApp(args, renamer)
            class Screen:
                def __init__(self):
                    self.lines = [list(" " * 80) for _ in range(24)]
                def getmaxyx(self): return 24, 80
                def addstr(self, y, x, text, style=0):
                    self.lines[y][x:x + len(text)] = text
            screen = Screen()
            app.screen = screen
            app.reload_local()
            for account in app.accounts:
                account.update(live_fetch(None, account, "resets"))
            try:
                app.draw_accounts()
                text = "\n".join("".join(line) for line in screen.lines)
                for i in range(4):
                    self.assertIn(f"account-{i + 1}@example.org", text)
                self.assertEqual(text.count("Weekly"), 4)
                self.assertEqual(text.count("↺ next"), 4)
                self.assertTrue(all("".join(line).strip() == "" for line in screen.lines[21:]))
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
