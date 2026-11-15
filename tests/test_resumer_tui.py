"""Run the resumer through a real terminal, with fake Codex/Auth executables."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from test_hub import PROJECT, make_fixture
from test_hub_tui import RUNNER
from tests.test_resumer import FIRST, add_threads
from tests.terminal_helpers import wait_for_exit


class ResumerTerminalTests(unittest.TestCase):
    def test_full_text_search_filters_columns_navigation_and_resume(self):
        with tempfile.TemporaryDirectory(prefix="accountdeck-resumer-pty-") as tmp:
            home = Path(tmp)
            fake = make_fixture(home)
            project, nested, _ = add_threads(home)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
            process = subprocess.Popen([sys.executable, "-c", RUNNER, "--codex-home", str(home),
                                        "--codex-bin", str(fake), "--auth-bin", str(fake)],
                                       stdin=slave, stdout=slave, stderr=slave, cwd=PROJECT,
                                       env={**os.environ, "TERM": "xterm-256color", "LC_ALL": "C.UTF-8"},
                                       start_new_session=True)
            os.close(slave)
            transcript = bytearray()
            def wait_until(predicate, timeout=8):
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
                self.fail("Resumer terminal journey timed out:\n" + transcript[-6000:].decode(errors="replace"))
            def wait_for(text):
                wait_until(lambda: text.encode() in transcript)
            def commands():
                path = home / "commands.jsonl"
                return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            try:
                wait_for("account-4@example.org")
                os.write(master, b"\x1bOQ")  # F2
                wait_for("Door tracking")
                os.write(master, b"quartz-falcon")
                wait_for("1 matches / 5 local threads")
                wait_for("Only in the conversation")
                os.write(master, b"\x1b[17~")  # F6
                wait_for("Thread filters")
                os.write(master, b"\r")
                wait_for("Choose directory")
                os.write(master, b"\x1bOB\r")  # Enter a directory path
                wait_for("Directory filter")
                os.write(master, str(project).encode() + b"\r\x1b[17~")
                wait_for("(+ subfolders)")
                os.write(master, b"\x1b[18~")  # F7
                wait_for("Visible columns")
                os.write(master, b"\x1bOB" * 5 + b" \r")  # model checkbox
                os.write(master, b"\x1b[19~")  # F8
                wait_for("Sort threads")
                os.write(master, b"\r")
                time.sleep(.15)  # allow the filter's background search to publish its result
                os.write(master, b"\r")  # Resume
                wait_until(lambda: ["--cd", str(nested), "resume", "--", FIRST] in commands())
                wait_for("Returned from Codex")
                self.assertFalse(any(call[0] == "switch" or call[:3] == ["app-server", "daemon", "start"] for call in commands()))
                os.write(master, b"\x1bOD")  # left -> Accounts
                wait_for("account-3@example.org")
                os.write(master, b"\x1bOQ")  # F2 again
                time.sleep(.1)
                os.write(master, b"\x1b[21~")  # F10
                wait_for_exit(process, master)
                self.assertEqual(process.returncode, 0)
                self.assertNotIn(b"Traceback", transcript)
                self.assertNotIn(b"secret-", transcript)
                settings = json.loads((home / "accountdeck/resumer.json").read_text())
                self.assertIn("model", settings["columns"])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    wait_for_exit(process, master)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
