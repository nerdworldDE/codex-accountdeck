import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from codex_renamer import AppServer, CodexError, RpcError
from tests.terminal_helpers import wait_for_exit

try:
    import fcntl
    import pty
    import termios
except ImportError:
    pty = None


PROJECT = Path(__file__).resolve().parents[1]


def make_fixture(root, count=3):
    binary = root / "codex-test"
    shutil.copyfile(Path(__file__).with_name("fake_codex.py"), binary)
    binary.chmod(0o755)
    threads = [{
        "id": f"thread-{i}", "name": f"Original {i}", "preview": f"Erste Nachricht {i}",
        "cwd": f"/projects/Projekt-{i}", "updatedAt": 1_790_000_000 + i,
        "source": "cli", "archived": False,
    } for i in range(count)]
    (root / "threads.json").write_text(json.dumps({"threads": threads, "page_size": 2}))
    return binary


def requests(root):
    path = root / "requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="codex-renamer-test-")
        self.root = Path(self.directory.name)
        self.binary = make_fixture(self.root, count=5)
        self.client = AppServer(str(self.binary), self.root, standalone=True, timeout=3)

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def test_recent_order_pagination_and_provider_filter(self):
        threads = self.client.list_threads(5)
        self.assertEqual([t.id for t in threads], [f"thread-{i}" for i in range(4, -1, -1)])
        calls = [r for r in requests(self.root) if r["method"] == "thread/list"]
        self.assertEqual(len(calls), 3)
        self.assertEqual([c["params"].get("cursor") for c in calls], [None, "2", "4"])
        for call in calls:
            self.assertEqual(call["params"]["sortKey"], "updated_at")
            self.assertEqual(call["params"]["sortDirection"], "desc")
            self.assertEqual(call["params"]["modelProviders"], [])
            self.assertFalse(call["params"]["archived"])

    def test_limit_and_interactive_source_filter(self):
        state = json.loads((self.root / "threads.json").read_text())
        state["threads"][-1]["source"] = "subAgent"
        state["threads"][-2]["source"] = "appServer"
        (self.root / "threads.json").write_text(json.dumps(state))
        self.assertEqual([t.id for t in self.client.list_threads(2)], ["thread-3", "thread-2"])

    def test_rename_unicode_persists_across_connections(self):
        self.assertTrue(self.client.rename("thread-1", "  Änderungen für 🐴 馬  "))
        self.client.close()
        thread = next(t for t in self.client.list_threads(5) if t.id == "thread-1")
        self.assertEqual(thread.name, "Änderungen für 🐴 馬")
        mutations = [r for r in requests(self.root) if r["method"] == "thread/name/set"]
        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0]["params"], {"threadId": "thread-1", "name": thread.name})

    def test_empty_names_never_start_server_or_write(self):
        self.assertFalse(self.client.rename("thread-1", ""))
        self.assertFalse(self.client.rename("thread-1", " \t \n "))
        self.assertIsNone(self.client.process)
        self.assertEqual(requests(self.root), [])

    def test_rpc_error_keeps_original_name(self):
        with self.assertRaisesRegex(RpcError, "Testfehler"):
            self.client.rename("thread-1", "ERROR")
        thread = next(t for t in self.client.list_threads(5) if t.id == "thread-1")
        self.assertEqual(thread.name, "Original 1")

    def test_archive_filter(self):
        state = json.loads((self.root / "threads.json").read_text())
        state["threads"][1]["archived"] = True
        (self.root / "threads.json").write_text(json.dumps(state))
        self.assertEqual([t.id for t in self.client.list_threads(5, archived=True)], ["thread-1"])

    def test_timeout_closes_process(self):
        self.client.connect()
        process = self.client.process
        self.client.timeout = 0.1
        with self.assertRaisesRegex(CodexError, "antwortet nicht"):
            self.client.request("test/timeout", {})
        self.assertIsNotNone(process.poll())

    def test_timeout_with_inherited_pipes_stays_bounded(self):
        self.client.connect()
        self.client.timeout = 0.1
        errors = []

        def request():
            try:
                self.client.request("test/inherited-pipes", {})
            except CodexError as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        worker.join(timeout=2)
        stuck = worker.is_alive()
        # A failing regression must not leave a descendant holding the pipes
        # open and block the test suite's own shutdown.
        child_file = self.root / "descendant.pid"
        if child_file.exists():
            try:
                os.kill(int(child_file.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        worker.join(timeout=2)
        self.assertFalse(stuck, "A descendant kept shutdown blocked on a pipe")
        self.assertEqual(len(errors), 1)
        self.assertIn("antwortet nicht", errors[0])

    def test_server_exit_is_reported(self):
        self.client.connect()
        with self.assertRaisesRegex(CodexError, "beendet"):
            self.client.request("test/exit", {})

    def test_malformed_protocol_is_reported(self):
        self.client.connect()
        with self.assertRaisesRegex(CodexError, "Ungültige Antwort"):
            self.client.request("test/malformed", {})

    def test_shutdown_prevents_new_process(self):
        self.client.shutdown()
        with self.assertRaises(CodexError):
            self.client.connect()
        self.assertIsNone(self.client.process)

    def test_missing_cli_is_actionable(self):
        self.client.binary = str(self.root / "missing-codex")
        with self.assertRaisesRegex(CodexError, "Codex nicht gefunden"):
            self.client.list_threads(5)

    def test_noninteractive_list_cli(self):
        result = subprocess.run([
            sys.executable, str(PROJECT / "codex_renamer.py"), "--list", "--limit", "2",
            "--codex-bin", str(self.binary), "--codex-home", str(self.root), "--standalone",
        ], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 2)
        self.assertIn("Original 4", result.stdout.splitlines()[0])


class LocalMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="codex-renamer-metadata-")
        self.root = Path(self.directory.name)
        # Loading the table must work even when no Codex process can start.
        self.client = AppServer(str(self.root / "missing-codex"), self.root)

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def create_database(self, filename="state_5.sqlite", modern=True):
        connection = sqlite3.connect(self.root / filename)
        fields = "id TEXT PRIMARY KEY, cwd TEXT, updated_at INTEGER, source TEXT, archived INTEGER, title TEXT"
        if modern:
            fields += ", name TEXT, preview TEXT, updated_at_ms INTEGER"
        connection.execute(f"CREATE TABLE threads ({fields})")
        return connection

    def add_thread(self, db, thread_id, *, updated=1000, source="cli", archived=0, title="Erste Nachricht", **extra):
        data = dict(id=thread_id, cwd="/projects/test", updated_at=updated, source=source, archived=archived, title=title)
        data.update(extra)
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        db.execute(f"INSERT INTO threads ({columns}) VALUES ({placeholders})", tuple(data.values()))
        db.commit()

    def test_table_loads_without_server_in_recent_order_and_with_limit(self):
        with self.create_database() as db:
            self.add_thread(db, "old", updated=100, name="Alter Name")
            self.add_thread(db, "new", updated=200, name="Änderungen 🐴")
            self.add_thread(db, "agent", updated=500, source='{"subagent":{"other":"guardian"}}')
            self.add_thread(db, "archived", updated=600, archived=1)
            self.add_thread(db, "app", updated=150, source="app-server")
        threads = self.client.list_threads(2)
        self.assertEqual([t.id for t in threads], ["new", "app"])
        self.assertEqual(threads[0].name, "Änderungen 🐴")
        self.assertEqual([t.id for t in self.client.list_threads(10, archived=True)], ["archived"])
        self.assertIsNone(self.client.process)

    def test_legacy_index_names_and_database_names(self):
        with self.create_database() as db:
            self.add_thread(db, "indexed")
            self.add_thread(db, "explicit", name="Aktueller DB-Name")
            self.add_thread(db, "preview", title="Vorschau ohne eigenen Namen")
        index = [
            {"id": "indexed", "thread_name": "Erster Name"},
            {"id": "explicit", "thread_name": "Veralteter Indexname"},
            {"id": "indexed", "thread_name": "Letzter Indexname"},
        ]
        (self.root / "session_index.jsonl").write_text(
            "broken line\nnull\n" + "".join(json.dumps(entry) + "\n" for entry in index),
            encoding="utf-8",
        )
        threads = {t.id: t for t in self.client.list_threads(10)}
        self.assertEqual(threads["indexed"].title, "Letzter Indexname")
        self.assertEqual(threads["explicit"].title, "Aktueller DB-Name")
        self.assertEqual(threads["preview"].title, "Vorschau ohne eigenen Namen")

    def test_older_database_schema_and_numeric_version_order(self):
        with self.create_database("state_9.sqlite", modern=False) as db:
            self.add_thread(db, "old-schema-file")
        with self.create_database("state_10.sqlite", modern=False) as db:
            self.add_thread(db, "new-schema-file", title="Titel aus älterer Tabelle")
        threads = self.client.list_threads(10)
        self.assertEqual([t.id for t in threads], ["new-schema-file"])
        self.assertEqual(threads[0].title, "Titel aus älterer Tabelle")

    def test_refresh_reads_current_committed_wal_data(self):
        db = self.create_database()
        try:
            db.execute("PRAGMA journal_mode = WAL")
            self.add_thread(db, "wal", name="Vorher")
            self.assertEqual(self.client.list_threads(10)[0].name, "Vorher")
            db.execute("UPDATE threads SET name = 'Nachher' WHERE id = 'wal'")
            db.commit()
            self.assertEqual(self.client.list_threads(10)[0].name, "Nachher")
        finally:
            db.close()

    def test_rename_persists_names_without_a_codex_process(self):
        db = self.create_database()
        try:
            self.add_thread(db, "chosen", name="Vorher", updated=1234)
            self.add_thread(db, "other", name="Anderer Thread", updated=2345)
            self.assertTrue(self.client.rename("chosen", "  Änderungen 🐴  "))
            self.assertIsNone(self.client.process)
            self.assertEqual(
                db.execute("SELECT title, name, updated_at FROM threads WHERE id='chosen'").fetchone(),
                ("Änderungen 🐴", "Änderungen 🐴", 1234),
            )
            self.assertEqual(db.execute("SELECT name FROM threads WHERE id='other'").fetchone()[0], "Anderer Thread")
            entries = [json.loads(line) for line in (self.root / "session_index.jsonl").read_text().splitlines()]
            self.assertEqual(entries[-1]["id"], "chosen")
            self.assertEqual(entries[-1]["thread_name"], "Änderungen 🐴")
            self.assertTrue(entries[-1]["updated_at"].endswith("Z"))
            self.client.close()
            self.assertEqual(next(t.name for t in self.client.list_threads(10) if t.id == "chosen"), "Änderungen 🐴")
        finally:
            db.close()

    def test_legacy_rename_and_incomplete_index_tail(self):
        db = self.create_database(modern=False)
        try:
            self.add_thread(db, "legacy", title="Alter Titel")
            index = self.root / "session_index.jsonl"
            original = b'{"incomplete":'
            index.write_bytes(original)
            self.assertTrue(self.client.rename("legacy", "Neuer Titel"))
            self.assertTrue(index.read_bytes().startswith(original + b"\n"))
            self.assertEqual(json.loads(index.read_text().splitlines()[-1])["thread_name"], "Neuer Titel")
            self.assertEqual(db.execute("SELECT title FROM threads WHERE id='legacy'").fetchone()[0], "Neuer Titel")
            self.assertEqual(self.client.list_threads(10)[0].title, "Neuer Titel")
        finally:
            db.close()

    def test_name_field_matches_native_history_mode(self):
        db = self.create_database()
        try:
            db.execute("ALTER TABLE threads ADD COLUMN history_mode TEXT")
            self.add_thread(db, "legacy", title="Legacy-Vorschau", history_mode="legacy")
            self.add_thread(db, "paginated", title="Unveränderte Vorschau", history_mode="paginated")
            self.client.rename("legacy", "Legacy-Name")
            self.client.rename("paginated", "Paginated-Name")
            self.assertEqual(db.execute("SELECT title, name FROM threads WHERE id='legacy'").fetchone(), ("Legacy-Name", None))
            self.assertEqual(db.execute("SELECT title, name FROM threads WHERE id='paginated'").fetchone(), ("Unveränderte Vorschau", "Paginated-Name"))
        finally:
            db.close()

    def test_busy_database_stops_with_error_and_preserves_name(self):
        db = self.create_database()
        try:
            self.add_thread(db, "busy", name="Vorher")
            db.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            with self.assertRaisesRegex(CodexError, "Metadaten"):
                self.client.rename("busy", "Nachher")
            self.assertLess(time.monotonic() - start, 2.5)
            self.assertEqual(db.execute("SELECT name FROM threads WHERE id='busy'").fetchone()[0], "Vorher")
            self.assertFalse((self.root / "session_index.jsonl").exists())
        finally:
            db.close()

    @unittest.skipIf(pty is None, "POSIX file locks required")
    def test_busy_index_preserves_database(self):
        db = self.create_database()
        try:
            self.add_thread(db, "locked", name="Vorher")
            with (self.root / "session_index.jsonl.lock").open("wb") as index:
                fcntl.flock(index.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(CodexError, "Namensindex.*belegt"):
                    self.client.rename("locked", "Nachher")
            self.assertEqual(db.execute("SELECT name FROM threads WHERE id='locked'").fetchone()[0], "Vorher")
            self.assertFalse((self.root / "session_index.jsonl").exists())
        finally:
            db.close()

    def test_index_failure_preserves_database(self):
        db = self.create_database()
        try:
            self.add_thread(db, "blocked", name="Vorher")
            (self.root / "session_index.jsonl").mkdir()
            with self.assertRaisesRegex(CodexError, "konnte nicht gespeichert"):
                self.client.rename("blocked", "Nachher")
            self.assertEqual(db.execute("SELECT name FROM threads WHERE id='blocked'").fetchone()[0], "Vorher")
        finally:
            db.close()

    def test_missing_thread_does_not_write_index(self):
        db = self.create_database()
        db.close()
        with self.assertRaisesRegex(CodexError, "nicht mehr vorhanden"):
            self.client.rename("missing", "Name")
        self.assertFalse((self.root / "session_index.jsonl").exists())

    def test_corrupt_metadata_shows_error_without_server_start(self):
        (self.root / "state_5.sqlite").write_text("not a database")
        with self.assertRaisesRegex(CodexError, "konnte nicht gelesen werden"):
            self.client.list_threads(10)
        self.assertIsNone(self.client.process)

    def test_unknown_schema_falls_back_to_protocol(self):
        db = sqlite3.connect(self.root / "state_5.sqlite")
        db.execute("CREATE TABLE unrelated (id INTEGER)")
        db.close()
        self.client.binary = str(make_fixture(self.root))
        self.assertEqual(len(self.client.list_threads(10)), 3)
        self.assertTrue(any(r["method"] == "thread/list" for r in requests(self.root)))


@unittest.skipIf(pty is None, "PTY support requires Linux/macOS")
class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="codex-renamer-terminal-")
        self.root = Path(self.directory.name)
        self.binary = make_fixture(self.root)
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 120, 0, 0))
        env = os.environ.copy()
        env.update({"TERM": "xterm-256color", "LC_ALL": "C.UTF-8"})
        self.process = subprocess.Popen([
            sys.executable, str(PROJECT / "codex_renamer.py"), "--codex-bin", str(self.binary),
            "--codex-home", str(self.root), "--standalone",
        ], stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        self.output = b""
        self.mark = 0
        self.wait_text("Threads geladen.")

    def tearDown(self):
        if self.process.poll() is None:
            self.process.terminate()
        wait_for_exit(self.process, self.master)
        os.close(self.master)
        self.directory.cleanup()

    def send(self, value):
        self.mark = len(self.output)
        os.write(self.master, value.encode("utf-8") if isinstance(value, str) else value)

    def drain(self, timeout=0.1):
        ready, _, _ = select.select([self.master], [], [], timeout)
        if ready:
            try:
                chunk = os.read(self.master, 65536)
                self.output += chunk
                if b"\x1b]11;?" in chunk:
                    os.write(self.master, b"\x1b]11;rgb:0000/0000/0000\x1b\\")
            except OSError:
                pass

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return
        self.fail(f"Terminal condition timed out (exit={self.process.poll()}):\n{self.output.decode(errors='replace')[-4000:]}")

    def wait_text(self, text):
        self.wait_for(lambda: text.encode() in self.output[self.mark:])

    def mutations(self):
        return [r for r in requests(self.root) if r["method"] == "thread/name/set"]

    def open_editor(self):
        self.send("\r")
        self.wait_text("Thread umbenennen")

    def wait_editor_closed(self):
        # xterm hides the insertion cursor when returning to the table. Unlike
        # an unchanged status message, this is also emitted on repeated aborts.
        self.wait_for(lambda: b"\x1b[?25l" in self.output[self.mark:])

    def test_blank_escape_whitespace_and_fresh_field_after_save(self):
        self.open_editor()
        self.send("\r")
        self.wait_editor_closed()
        self.assertEqual(self.mutations(), [])

        self.open_editor()
        self.send("Verwerfen\x1b")
        self.wait_editor_closed()
        self.assertEqual(self.mutations(), [])

        self.open_editor()
        self.send("   \r")
        self.wait_editor_closed()
        self.assertEqual(self.mutations(), [])

        self.send(b"\x1bOB")  # xterm's Down key in application mode
        self.open_editor()
        self.send("Neuer Name ä 🐴\r")
        self.wait_for(lambda: len(self.mutations()) == 1)
        self.wait_text("Umbenannt:")
        self.assertEqual(self.mutations()[0]["params"], {"threadId": "thread-1", "name": "Neuer Name ä 🐴"})

        self.open_editor()
        self.send("\r")
        self.wait_editor_closed()
        self.assertEqual(len(self.mutations()), 1)
        self.send("q")
        self.assertEqual(wait_for_exit(self.process, self.master), 0)

    def test_search_selects_correct_thread_and_refresh_keeps_it(self):
        self.send("/")
        self.wait_text("Threads durchsuchen")
        self.send("Projekt-0\r")
        self.wait_text("passende Threads.")
        self.send("r")
        self.wait_text("Threads geladen.")
        self.open_editor()
        self.send("Gefunden\r")
        self.wait_for(lambda: len(self.mutations()) == 1)
        self.assertEqual(self.mutations()[0]["params"], {"threadId": "thread-0", "name": "Gefunden"})

    def test_save_error_retains_input_for_correction(self):
        self.open_editor()
        self.send("ERROR\r")
        self.wait_text("Testfehler beim Speichern")
        self.assertEqual(self.mutations()[0]["params"]["name"], "ERROR")
        self.send("\x15Korrigiert\r")
        self.wait_for(lambda: len(self.mutations()) == 2)
        self.wait_text("Umbenannt:")
        state = json.loads((self.root / "threads.json").read_text())
        self.assertEqual(state["threads"][-1]["name"], "Korrigiert")

    def test_local_save_finishes_without_waiting_for_the_server(self):
        db = sqlite3.connect(self.root / "state_5.sqlite")
        try:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, updated_at INTEGER, source TEXT, archived INTEGER, title TEXT, name TEXT)")
            for index in range(3):
                db.execute("INSERT INTO threads VALUES (?, ?, ?, 'cli', 0, ?, ?)", (
                    f"thread-{index}", f"/projects/Projekt-{index}", 1_790_000_000 + index,
                    f"Lokal {index}", f"Lokal {index}",
                ))
            db.commit()
            self.send("r")
            self.wait_text("Lokal 2")
            self.open_editor()
            start = time.monotonic()
            self.send("Direkt gespeichert\r")
            self.wait_text("Umbenannt:")
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(db.execute("SELECT name FROM threads WHERE id='thread-2'").fetchone()[0], "Direkt gespeichert")
            self.assertEqual(self.mutations(), [])
            self.open_editor()
            self.send("\r")
            self.wait_editor_closed()
            self.assertEqual(len((self.root / "session_index.jsonl").read_text().splitlines()), 1)
        finally:
            db.close()

    def test_thread_color_is_drawn_as_foreground(self):
        self.assertRegex(self.output, rb"\x1b\]4;\d+;rgb:")
        self.assertRegex(self.output, rb"\x1b\[[0-9;]*38;5;\d+m")
        self.assertIn("›".encode(), self.output)

    def test_resize_does_not_commit_hidden_input(self):
        self.open_editor()
        self.send("Nicht speichern")
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 30, 0, 0))
        self.process.send_signal(__import__("signal").SIGWINCH)
        self.wait_text("Terminal bitte")
        self.send("\r")
        self.drain(0.2)
        self.assertEqual(self.mutations(), [])
        self.send("q")
        self.assertEqual(wait_for_exit(self.process, self.master), 0)


if __name__ == "__main__":
    unittest.main()
