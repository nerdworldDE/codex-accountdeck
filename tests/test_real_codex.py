"""Optional smoke test against the installed Codex CLI in an isolated home."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from codex_renamer import AppServer


@unittest.skipUnless(
    os.environ.get("CODEX_RENAMER_INTEGRATION") == "1" and shutil.which("codex"),
    "Enable with CODEX_RENAMER_INTEGRATION=1 (requires Codex CLI)",
)
class RealCodexTests(unittest.TestCase):
    def test_paginated_name_and_later_native_rename(self):
        with tempfile.TemporaryDirectory(prefix="codex-renamer-paginated-") as directory:
            root = Path(directory)
            client = AppServer(codex_home=root, standalone=True, timeout=10)
            try:
                client.connect()
                thread = client.request("thread/start", {"cwd": str(root), "ephemeral": False})["thread"]
                self.assertEqual(thread["historyMode"], "paginated")
                thread_id = thread["id"]
                client.request("thread/name/set", {"threadId": thread_id, "name": "Nativer Ausgangsname"})
                client.close()
                self.assertTrue(client.rename(thread_id, "Lokal gespeichert 🐴"))
                self.assertIsNone(client.process)
                with sqlite3.connect(root / "state_5.sqlite") as db:
                    self.assertEqual(db.execute("SELECT title, name FROM threads WHERE id=?", (thread_id,)).fetchone(), ("", "Lokal gespeichert 🐴"))
                client.connect()
                self.assertEqual(client.request("thread/read", {"threadId": thread_id})["thread"]["name"], "Lokal gespeichert 🐴")
                client.request("thread/name/set", {"threadId": thread_id, "name": "Danach in Codex umbenannt"})
                client.close()
                self.assertEqual(next(t.name for t in client.list_threads(10) if t.id == thread_id), "Danach in Codex umbenannt")
            finally:
                client.close()

    def test_name_survives_restart_without_inference(self):
        with tempfile.TemporaryDirectory(prefix="codex-renamer-integration-") as directory:
            root = Path(directory)
            folder = root / "sessions" / "2026" / "11" / "01"
            folder.mkdir(parents=True)
            thread_id = "019fbe01-2345-7000-8000-000000000001"
            timestamp = "2026-11-01T12:00:00.000Z"
            events = [
                {"timestamp": timestamp, "type": "session_meta", "payload": {
                    "id": thread_id, "timestamp": timestamp, "cwd": str(root),
                    "originator": "codex_cli_rs", "cli_version": "0.166.0", "source": "cli", "model_provider": "openai",
                }},
                {"timestamp": timestamp, "type": "event_msg", "payload": {
                    "type": "user_message", "message": "Isolierter Testthread", "images": [], "local_images": [], "text_elements": [],
                }},
                {"timestamp": timestamp, "type": "response_item", "payload": {
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Isolierter Testthread"}],
                }},
            ]
            rollout = folder / f"rollout-2026-11-01T12-00-00-{thread_id}.jsonl"
            rollout.write_text("".join(json.dumps(event) + "\n" for event in events))
            client = AppServer(codex_home=root, standalone=True, timeout=10)
            try:
                threads = client.list_threads(10)
                self.assertEqual([t.id for t in threads], [thread_id])
                self.assertEqual(threads[0].title, "Isolierter Testthread")
                self.assertFalse(client.rename(thread_id, "  "))
                self.assertTrue(client.rename(thread_id, "Integration: Änderungen 🐴"))
                client.close()
                threads = client.list_threads(10)
                self.assertEqual(threads[0].name, "Integration: Änderungen 🐴")
                self.assertIsNone(client.process, "Metadata loading must not restart Codex")
                client.connect()
                self.assertEqual(client.request("thread/read", {"threadId": thread_id})["thread"]["name"], threads[0].name)
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
