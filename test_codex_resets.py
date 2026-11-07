import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import codex_resets as app


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)

    def credit(self, days):
        return {"expires_at": (self.now + timedelta(days=days)).isoformat()}

    def test_boundaries_and_expiration(self):
        for days, expected in [(-1, "expired"), (0, "expired"), (1, "critical"),
                               (7, "warning"), (7.01, "ok")]:
            self.assertEqual(app.severity(app.remaining(self.credit(days), self.now)), expected)
        self.assertEqual(app.lifetime(30), "Noch 1 Std.")
        self.assertEqual(app.severity(None), "unknown")
        account = {"credits": [self.credit(-1), self.credit(0), self.credit(1), {"expires_at": None}]}
        self.assertEqual(len(app.valid_credits(account, self.now)), 2)

    def test_sorting_both_directions_with_unknown_expiry(self):
        credits = [self.credit(3), {"expires_at": "invalid"}, self.credit(10)]
        account = {"credits": credits}
        self.assertEqual(app.ordered_credits(account, self.now), [credits[2], credits[0], credits[1]])
        self.assertEqual(app.ordered_credits(account, self.now, True), [credits[0], credits[2], credits[1]])

    def test_status_filter_and_unknown_schema(self):
        data = {"credits": [{"status": "Available", "id": "a"},
                            {"status": "redeemed"}, {"status": "expired"}]}
        self.assertEqual([c["id"] for c in app.available_credits(data)], ["a"])
        with self.assertRaises(ValueError):
            app.available_credits({"available_count": 4})
        self.assertEqual(app.available_credits({"credits": []}), [])

    def test_account_auth_isolation_and_no_secrets_in_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "accounts").mkdir()
            for name in ("one", "two"):
                encoded = app.base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
                (home / "accounts" / f"{encoded}.auth.json").write_text(json.dumps({
                    "tokens": {"access_token": f"secret-{name}", "account_id": name}}))
            seen = []
            class Opener:
                def open(self, request, timeout):
                    seen.append(dict(request.header_items()))
                    return io.BytesIO(b'{"credits": [], "available_count": 0}')
            with patch.object(app.urllib.request, "build_opener", return_value=Opener()):
                results = [app.fetch_account({"account_key": name}, home) for name in ("one", "two")]
            self.assertEqual(seen[0]["Authorization"], "Bearer secret-one")
            self.assertEqual(seen[1]["Authorization"], "Bearer secret-two")
            self.assertNotIn("secret-", json.dumps(results))
            with patch.object(app.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = urllib.error.HTTPError(app.ENDPOINT, 401, "", {}, None)
                result = app.fetch_account({"account_key": "one"}, home)
            self.assertIn("Anmeldung abgelaufen", result["error"])
            self.assertIsNone(result.get("checked_at"))

    def test_notifications_are_deduplicated_and_ignore_failed_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = {"accounts": [{"account_key": "a", "email": "a@example.org", "credits": [
                {"expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}]},
                {"account_key": "b", "error": "offline", "credits": []}]}
            with patch.object(app, "STATE", Path(tmp)), patch.object(app, "send_notification", return_value=True) as run:
                app.notify(snapshot, 7)
                app.notify(snapshot, 7)
                self.assertEqual(run.call_count, 1)

    def test_failed_notification_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = {"accounts": [{"account_key": "a", "email": "a@example.org", "credits": [
                {"expires_at": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()}]}]}
            with patch.object(app, "STATE", Path(tmp)), patch.object(app, "send_notification", return_value=False) as send:
                app.notify(snapshot, 7)
                app.notify(snapshot, 7)
                self.assertEqual(send.call_count, 2)

    def test_no_credential_forwarding_on_redirect(self):
        self.assertIsNone(app.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org"))


class AccountSwitchTests(unittest.TestCase):
    def test_selection_uses_display_order_and_stable_key(self):
        now = datetime.now(timezone.utc)
        snapshot = {"accounts": [
            {"account_key": "soon", "email": "same@example.org", "credits": [
                {"expires_at": (now + timedelta(days=2)).isoformat()}]},
            {"account_key": "later", "email": "same@example.org", "credits": [
                {"expires_at": (now + timedelta(days=12)).isoformat()}]},
        ]}
        output = io.StringIO()
        accounts = app.terminal(snapshot, numbered=True, stream=output)
        self.assertIn("[1] same@example.org", output.getvalue())
        with patch("builtins.input", side_effect=["invalid", "0", "1"]), \
                patch.object(app.subprocess, "run") as run, \
                patch("sys.stdout", new=io.StringIO()):
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps({"switched_to": {"account_key": "later"}})
            self.assertEqual(app.choose_account(accounts, Path("/tmp/test-codex-home")), 0)
        self.assertEqual(run.call_args.args[0], ["codex-auth", "switch", "later", "--json"])
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], "/tmp/test-codex-home")
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_cancel_never_runs_codex_auth(self):
        for choice in ("", "q", "Q"):
            with self.subTest(choice=choice), patch("builtins.input", return_value=choice), \
                    patch.object(app.subprocess, "run") as run, patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(app.choose_account([{"account_key": "one"}], Path("/tmp")), 0)
                run.assert_not_called()

    def test_failed_or_wrong_switch_is_not_reported_as_success(self):
        for code, stdout in ((1, '{"error": {}}'), (0, "unexpected output"),
                             (0, '{"switched_to": {"account_key": "wrong"}}')):
            with self.subTest(code=code, stdout=stdout), patch("builtins.input", return_value="1"), \
                    patch.object(app.subprocess, "run") as run, patch("sys.stderr", new=io.StringIO()) as errors:
                run.return_value.returncode = code
                run.return_value.stdout = stdout
                self.assertEqual(app.choose_account([{"account_key": "one"}], Path("/tmp")), 1)
                self.assertIn("Account switch failed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
