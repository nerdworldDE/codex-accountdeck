"""The public --demo entry point is usable in an 80x24 screenshot terminal."""
from contextlib import closing
import fcntl
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

from test_hub import PROJECT
from tests.terminal_helpers import wait_for_exit


class DemoTerminalTests(unittest.TestCase):
    def test_demo_keyboard_journey_does_not_use_the_supplied_real_home_or_binaries(self):
        with tempfile.TemporaryDirectory(prefix="accountdeck-demo-pty-") as tmp:
            root = Path(tmp)
            scratch = root / "scratch"
            scratch.mkdir()
            sentinel = root / "auth.json"
            sentinel.write_text("REAL-HOME-SENTINEL")
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            process = subprocess.Popen([sys.executable, str(PROJECT / "codex_hub.py"), "--demo",
                                        "--codex-home", str(root), "--codex-bin", "/no-real-codex",
                                        "--auth-bin", "/no-real-auth"], stdin=slave, stdout=slave, stderr=slave,
                                       cwd=PROJECT, env={**os.environ, "TERM": "xterm-256color",
                                                        "LC_ALL": "C.UTF-8", "TMPDIR": str(scratch)},
                                       start_new_session=True)
            os.close(slave)
            transcript = bytearray()
            def wait_until(predicate, timeout=6):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .04)[0]:
                        try:
                            transcript.extend(os.read(master, 65536))
                        except OSError:
                            break
                    if predicate():
                        return
                    if process.poll() is not None:
                        break
                self.fail("Demo terminal timed out:\n" + transcript[-5000:].decode(errors="replace"))
            def wait_for(text):
                wait_until(lambda: text.encode() in transcript)
            try:
                wait_for("DEMO")
                wait_for("4 ACCOUNTS")
                wait_for("All accounts up to date")
                def indexed_before_opening_threads():
                    index = list(scratch.glob("*/accountdeck/thread-search.sqlite"))
                    if not index:
                        return False
                    try:
                        with closing(sqlite3.connect(index[0].as_uri() + "?mode=ro", uri=True)) as db:
                            return db.execute("SELECT count(*) FROM search_terms").fetchone()[0] == 9
                    except sqlite3.Error:
                        return False
                wait_until(indexed_before_opening_threads)
                os.write(master, b"\x1bOBx")  # Down selects Personal; x opens the credit picker.
                wait_for("Redeem saved reset")
                wait_for("Account: Personal")
                os.write(master, b"\x1b")  # Cancel once, then open for explicit confirmation.
                time.sleep(.10)
                self.assertNotIn(b"Reset redeemed", transcript)
                os.write(master, b"xy")
                wait_for("Reset redeemed for Personal")
                os.write(master, b"\x1bOQ")  # F2
                wait_for("Build a thread resumer")
                os.write(master, b"shutter")  # Typing starts a live full-text search
                wait_for("1 matches / 9 local threads")
                os.write(master, b"\r")
                wait_for("DEMO CONVERSATION")
                wait_for("Use the shutter for NUC calibration")
                os.write(master, b"\r")
                wait_for("Returned from demo conversation")
                os.write(master, b"\x1bOP")  # F1
                wait_for("4 ACCOUNTS")
                os.write(master, b"l")
                wait_for("Demo login added")
                wait_for("Guest")  # curses may update only the digit in the existing account count
                os.write(master, b"\x1bOQ")  # F2
                cleared_at = len(transcript)
                os.write(master, b"\x15")  # Ctrl+U clears the search
                wait_until(lambda: b"8 matches / 9 local threads" in transcript[cleared_at:])
                os.write(master, b"\x12")  # Ctrl+R renames in the same tab
                wait_for("Rename thread")
                os.write(master, b"Screenshot example\r")
                def renamed():
                    homes = list(scratch.glob("codex-accountdeck-demo-*"))
                    if not homes:
                        return False
                    connection = sqlite3.connect(homes[0] / "state_5.sqlite")
                    try:
                        return bool(connection.execute("SELECT 1 FROM threads WHERE name='Screenshot example'").fetchone())
                    finally:
                        connection.close()
                wait_until(renamed)
                wait_for("Renamed: Screenshot example")
                os.write(master, b"\x1b[21~")  # F10
                wait_for_exit(process, master)
                self.assertEqual(process.returncode, 0)
                self.assertNotIn(b"Traceback", transcript)
                self.assertNotIn(b"REAL-HOME-SENTINEL", transcript)
                self.assertEqual(sentinel.read_text(), "REAL-HOME-SENTINEL")
                self.assertEqual(list(scratch.glob("codex-accountdeck-demo-*")), [])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    wait_for_exit(process, master)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
