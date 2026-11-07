"""Exercise the actual curses app through a PTY, with isolated accounts/threads."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from test_hub import make_fixture, PROJECT, RENAMER
from tests.terminal_helpers import wait_for_exit

RUNNER = """
import sys
from hub_accounts import AccountService
from test_hub import live_fetch
from codex_hub import main
AccountService.fetch = live_fetch
raise SystemExit(main(sys.argv[1:]))
"""


@unittest.skipUnless((RENAMER / "codex_renamer.py").exists(), "Local renamer unavailable")
class TerminalJourneyTests(unittest.TestCase):
    def test_rename_tab_navigation_switch_and_login_restart(self):
        with tempfile.TemporaryDirectory(prefix="codex-hub-tui-") as tmp:
            home = Path(tmp)
            fake = make_fixture(home)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
            process = subprocess.Popen([sys.executable, "-c", RUNNER, "--codex-home", str(home),
                                        "--codex-bin", str(fake), "--auth-bin", str(fake)],
                                       stdin=slave, stdout=slave, stderr=slave, cwd=PROJECT,
                                       env={**os.environ, "TERM": "xterm-256color", "LC_ALL": "C.UTF-8"},
                                       start_new_session=True)
            os.close(slave)
            transcript = bytearray()
            def wait_until(predicate, timeout=6):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .05)[0]:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            break
                        transcript.extend(chunk)
                    if predicate():
                        return
                    if process.poll() is not None:
                        break
                self.fail("Terminal journey timed out:\n" + transcript[-5000:].decode(errors="replace"))
            def title():
                with sqlite3.connect(home / "state_5.sqlite") as db:
                    return db.execute("SELECT name FROM threads WHERE id='thread-0'").fetchone()[0]
            try:
                wait_until(lambda: b"account-4@example.org" in transcript and b"SAVED RESETS" in transcript)
                self.assertNotIn(b"Traceback", transcript)
                os.write(master, b"\x1bOC")  # right: Threads
                wait_until(lambda: b"Original thread 0" in transcript)
                os.write(master, b"\r")
                wait_until(lambda: b"Rename thread" in transcript)
                os.write(master, b"Renamed title\x1bODX\r")  # left stays in the editor
                wait_until(lambda: title() == "Renamed titlXe")
                os.write(master, b"\x1bOD")  # left: Accounts
                time.sleep(.12)
                os.write(master, b"\x1bOB\r")  # down, Enter: second account
                def switched():
                    data = json.loads((home / "accounts/registry.json").read_text())
                    calls = (home / "commands.jsonl").read_text()
                    return data["active_account_key"] == "user-1::workspace" and '"daemon", "start"' in calls
                wait_until(switched)
                wait_until(lambda: b"background service restarted" in transcript)
                os.write(master, b"l")
                wait_until(lambda: b"FAKE LOGIN READY" in transcript)
                os.write(master, b"\r")  # answer the child process's sign-in prompt
                wait_until(lambda: json.loads((home / "accounts/registry.json").read_text())["active_account_key"] == "user-4::workspace")
                wait_until(lambda: b"Login successful" in transcript)
                wait_until(lambda: (home / "commands.jsonl").read_text().count('"daemon", "start"') == 2)
                wait_until(lambda: b"account-5@example.org" in transcript)
                os.write(master, b"q")
                wait_for_exit(process, master)
                self.assertEqual(process.returncode, 0)
                with sqlite3.connect(home / "state_5.sqlite") as db:
                    self.assertEqual(db.execute("SELECT name FROM threads WHERE id='thread-1'").fetchone()[0], "Original thread 1")
                calls = [json.loads(line) for line in (home / "commands.jsonl").read_text().splitlines()]
                self.assertIn(["switch", "user-1::workspace", "--json"], calls)
                self.assertIn(["login"], calls)
                self.assertNotIn(b"Traceback", transcript)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    wait_for_exit(process, master)
                os.close(master)

    def test_device_login_ctrl_c_returns_to_usable_tui_without_restart(self):
        with tempfile.TemporaryDirectory(prefix="codex-hub-login-") as tmp:
            home = Path(tmp)
            fake = make_fixture(home)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            process = subprocess.Popen([sys.executable, "-c", RUNNER, "--codex-home", str(home),
                                        "--codex-bin", str(fake), "--auth-bin", str(fake)],
                                       stdin=slave, stdout=slave, stderr=slave, cwd=PROJECT,
                                       env={**os.environ, "TERM": "xterm-256color", "LC_ALL": "C.UTF-8"},
                                       start_new_session=True)
            os.close(slave)
            transcript = bytearray()
            def wait_for(token, timeout=6):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .05)[0]:
                        try:
                            transcript.extend(os.read(master, 65536))
                        except OSError:
                            break
                    if token in transcript:
                        return
                    if process.poll() is not None:
                        break
                self.fail("Login journey timed out:\n" + transcript[-4000:].decode(errors="replace"))
            try:
                wait_for(b"account-4@example.org")
                os.write(master, b"L")
                wait_for(b"FAKE LOGIN READY")
                calls = [json.loads(line) for line in (home / "commands.jsonl").read_text().splitlines()]
                self.assertIn(["login", "--device-auth"], calls)
                os.killpg(process.pid, signal.SIGINT)
                wait_for(b"Login cancelled")
                self.assertIsNone(process.poll())
                self.assertEqual(len(json.loads((home / "accounts/registry.json").read_text())["accounts"]), 4)
                calls = [json.loads(line) for line in (home / "commands.jsonl").read_text().splitlines()]
                self.assertNotIn(["app-server", "daemon", "start"], calls)
                os.write(master, b"\x1bOC")
                wait_for(b"Original thread 0")
                os.write(master, b"q")
                wait_for_exit(process, master)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    wait_for_exit(process, master)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
