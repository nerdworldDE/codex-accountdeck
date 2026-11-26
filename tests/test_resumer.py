"""Account-independent discovery, real transcript search, and safe resume journeys."""
import argparse
from contextlib import closing
from dataclasses import replace
import curses
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from codex_hub import HubApp, load_renamer
from hub_accounts import CommandError, registry_snapshot
from hub_resumer import ResumerView
from hub_threads import SavedThread, ThreadStore, in_directory, resume_plan
from test_hub import make_fixture, PROJECT

FIRST = "00000000-0000-4000-8000-000000000101"
SECOND = "00000000-0000-4000-8000-000000000102"


def add_threads(home):
    project = home / "projects" / "luna"
    nested = project / "door" / "camera"
    nested.mkdir(parents=True)
    sibling = home / "projects" / "lunatic"
    sibling.mkdir()
    rollout = home / "sessions" / "2026" / "11" / "15" / "rollout-fixture.jsonl"
    rollout.parent.mkdir(parents=True)
    meta = {"id": FIRST, "cwd": str(nested), "account_scoped": True,
            "creator_user_id": "user-2", "creator_account_id": "workspace", "source": "vscode"}
    rollout.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n" +
                       json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": "Only in the conversation: Wärmebild STRAẞE and quartz-falcon."}]}}) + "\n")
    with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
        for name, kind in (("created_at", "INTEGER"), ("model", "TEXT"), ("tokens_used", "INTEGER"),
                           ("creator_user_id", "TEXT"), ("creator_account_id", "TEXT"), ("rollout_path", "TEXT")):
            db.execute(f"ALTER TABLE threads ADD COLUMN {name} {kind}")
        now = int(time.time()) + 30
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,0,?,?,?,?,?,?)", (
            FIRST, "Door tracking", "Preview without the needle", str(nested), now, "vscode",
            now - 10, "model-a", 420, "user-2", "workspace", str(rollout)))
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?)", (
            SECOND, "Archived sibling", "Other preview", str(sibling), now - 1, "cli",
            now - 20, "model-b", 120, "user-1", "workspace", ""))
    return project, nested, rollout


def wait_for_view(view):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        view.finish_pending()
        if not view.pending and not view.indexing:
            return
        time.sleep(.01)
    raise AssertionError("Thread search did not finish")


class ResumerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="accountdeck-resumer-test-")
        self.home = Path(self.temporary.name)
        self.fake = make_fixture(self.home)
        self.project, self.nested, self.rollout = add_threads(self.home)
        self.store = ThreadStore(self.home)
        self.accounts = registry_snapshot(self.home)

    def tearDown(self):
        self.temporary.cleanup()

    def target(self):
        return next(row for row in self.store.load() if row.id == FIRST)

    def test_discovery_has_no_active_login_or_recent_page_filter(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.executemany("INSERT INTO threads (id,name,cwd,updated_at,source,archived) VALUES (?,?,?,?,?,0)",
                           [(f"extra-{i}", f"Extra {i}", str(self.project), i, "cli") for i in range(260)])
        rows = self.store.load()
        self.assertEqual(len(rows), 265)
        target = next(row for row in rows if row.id == FIRST)
        self.assertEqual(target.owner_key, "user-2::workspace")
        self.assertFalse(next(a for a in self.accounts if a["account_key"] == target.owner_key)["active"])
        self.assertFalse((self.home / "commands.jsonl").exists())

    def test_directory_filter_recurses_without_matching_sibling_prefix(self):
        self.assertTrue(in_directory(str(self.nested), str(self.project)))
        self.assertTrue(in_directory(str(self.project), str(self.project)))
        self.assertFalse(in_directory(str(self.home / "projects/lunatic"), str(self.project)))
        view = ResumerView(self.home, load_renamer(PROJECT), str(self.project))
        try:
            view.archive = "all"
            view.refresh(self.accounts)
            wait_for_view(view)
            self.assertEqual([row.id for row in view.visible], [FIRST])
        finally:
            view.close()

    def test_search_finds_conversation_only_text_and_unicode_and_phrases(self):
        # Randomized fixture paths can also contain short queries like "qu".
        rows = [replace(row, cwd="") for row in self.store.load()]
        self.assertEqual(self.store.update_index(rows), 0)
        reopened = ThreadStore(self.home)
        with patch.object(reopened, "content_parts", side_effect=AssertionError("A search read the transcript")):
            for query in ("quartz-falcon", "wärmebild strasse", '"Only in the conversation"', "Door quartz-falcon", "ä", "qu"):
                matches, _ = reopened.search(rows, query, self.accounts)
                self.assertEqual([row.id for row in matches], [FIRST], query)
                self.assertIn("conversation", reopened.excerpt(FIRST, query, prompt=False))
        matches, _ = reopened.search(rows, "quartz-falcon", self.accounts, full_text=False)
        self.assertEqual(matches, [])
        self.assertEqual((self.home / "accountdeck/thread-search.sqlite").stat().st_mode & 0o777, 0o600)

    def test_title_prompt_and_text_rank_before_recency_and_migrate_the_cache(self):
        cache = self.home / "accountdeck/thread-search.sqlite"
        cache.parent.mkdir()
        with closing(sqlite3.connect(cache)) as db, db:
            db.execute("CREATE TABLE documents (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, text TEXT NOT NULL)")
        rows = []
        for index, (title, role, message) in enumerate((
                ("Calibration of the camera", "assistant", "Unrelated response"),
                ("Zebra project", "user", "Please fix the CALIBRATION"),
                ("Aardvark project", "assistant", "Calibration is ready"))):
            path = self.home / f"rank-{index}.jsonl"
            path.write_text(json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": role, "content": [{"type": "text", "text": message}]}}) + "\n")
            rows.append(SavedThread(id=f"rank-{index}", title=title, path=str(path), updated=index * 100))
        metadata = SavedThread(id="metadata", title="Metadata only", cwd="/calibration", updated=9999)
        records = [metadata, *reversed(rows)]
        expected = [row.id for row in rows] + [metadata.id]
        self.store.update_index(records)
        for _ in range(2):  # Refreshing the index retains cached text and its rank.
            result = self.store.search(records, "calibration", self.accounts)
            self.assertEqual([row.id for row in result[0]], expected)
            with patch.object(self.store, "content_parts", side_effect=AssertionError("Unchanged transcript reread")):
                self.store.update_index(records)
        with closing(sqlite3.connect(cache)) as db:
            self.assertIn("CALIBRATION", db.execute("SELECT prompts FROM documents WHERE id='rank-1'").fetchone()[0])
        view = ResumerView(self.home, load_renamer(PROJECT))
        try:
            view.records, view.loaded = records, True
            view.refresh(self.accounts, reload=False)
            view.sort, view.reverse = "title", False
            for char in "calibration":
                view.handle_key(char, 5)
            wait_for_view(view)
            self.assertEqual([row.id for row in view.visible], expected)
            self.assertEqual(view.selected.id, expected[0])
            view.handle_key(curses.KEY_F9, 5)
            self.assertEqual([row.id for row in view.visible], expected)
        finally:
            view.close()

    def test_live_typing_treats_old_shortcuts_as_search_text(self):
        view = ResumerView(self.home, load_renamer(PROJECT))
        try:
            view.refresh(self.accounts)
            wait_for_view(view)
            for char in "qrfcstjk[]/":
                view.handle_key(char, 5)
            self.assertEqual(view.query, "qrfcstjk[]/")
            self.assertFalse(view.mode)
            wait_for_view(view)
            self.assertEqual(view.visible, [])
            view.handle_key("\x15", 5)
            wait_for_view(view)
            self.assertEqual(view.query, "")
            self.assertEqual(len(view.visible), 4)
            self.assertEqual(view.handle_key("\x1b", 5), "quit")
        finally:
            view.close()

    def test_long_directory_filter_keeps_the_folder_and_subfolder_hint_visible(self):
        view = ResumerView(self.home, load_renamer(PROJECT))
        screen = Mock()
        view.directory = "/var/folders/" + "temporary-profile-" * 8 + "/projects/luna"
        try:
            for width in (76, 80, 110):
                screen.getmaxyx.return_value = (30, width)
                line = Mock()
                view.draw(screen, line, lambda name: 0, lambda row: 0)
                summary = next(call.args for call in line.call_args_list if call.args[:2] == (7, 5))
                displayed = view.renamer.clip(summary[2], summary[3])
                self.assertIn("luna", displayed)
                self.assertIn("(+ subfolders)", displayed)
                self.assertIn("…", displayed)
        finally:
            view.close()

    def test_typing_during_initial_load_does_not_replace_it_with_empty_records(self):
        view = ResumerView(self.home, load_renamer(PROJECT))
        started, release = threading.Event(), threading.Event()
        original = view.store.load
        def delayed_load():
            started.set()
            release.wait(timeout=3)
            return original()
        try:
            with patch.object(view.store, "load", side_effect=delayed_load):
                view.refresh(self.accounts)
                self.assertTrue(started.wait(timeout=2))
                for char in "quartz-falcon":
                    view.handle_key(char, 5)
                release.set()
                wait_for_view(view)
            self.assertEqual([row.id for row in view.visible], [FIRST])
        finally:
            release.set()
            view.close()

    def test_filter_choices_apply_together_and_escape_discards_changes(self):
        view = ResumerView(self.home, load_renamer(PROJECT))
        try:
            view.refresh(self.accounts)
            wait_for_view(view)
            for apply in (False, True):
                view.handle_key(curses.KEY_F6, 5)
                view.handle_key(curses.KEY_DOWN, 5)
                view.handle_key("\n", 5)
                self.assertEqual(view.mode, "options")
                view.handle_key(curses.KEY_DOWN, 5)
                view.handle_key("\n", 5)
                self.assertEqual(view.archive, "active")
                view.handle_key(curses.KEY_F6 if apply else "\x1b", 5)
                wait_for_view(view)
                self.assertEqual(view.archive, "all" if apply else "active")
                self.assertEqual(len(view.visible), 5 if apply else 4)
            view.handle_key(curses.KEY_F6, 5)
            view.handle_key("\x0c", 5)
            view.handle_key(curses.KEY_F6, 5)
            wait_for_view(view)
            self.assertEqual(view.archive, "active")
        finally:
            view.close()

    def test_legacy_preferences_hide_login_until_it_is_explicitly_enabled_again(self):
        settings = self.home / "accountdeck/resumer.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"columns": ["updated", "title", "cwd", "account"]}))
        view = ResumerView(self.home, load_renamer(PROJECT))
        try:
            self.assertNotIn("account", view.columns)
            view.columns.append("account")
            view.save_settings()
            restored = ResumerView(self.home, load_renamer(PROJECT))
            self.assertIn("account", restored.columns)
            restored.close()
        finally:
            view.close()

    def test_search_reads_paginated_messages_and_tool_outputs_without_original_rollout(self):
        with closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_items (thread_id TEXT, rollout_ordinal INTEGER, item_json TEXT)")
            db.executemany("INSERT INTO thread_items VALUES (?,?,?)", [
                (FIRST, 1, json.dumps({"type": "userMessage", "content": [{"type": "text", "text": "paginated-user-needle"}]})),
                (FIRST, 2, json.dumps({"type": "agentMessage", "text": "paginated-answer-needle"})),
                (FIRST, 3, json.dumps({"type": "commandExecution", "command": "diagnostic-command", "aggregatedOutput": "tool-output-needle"})),
            ])
        self.rollout.unlink()
        records = [self.target()]
        self.assertEqual(self.store.update_index(records), 0)
        for query in ("paginated-user-needle", "paginated-answer-needle", "tool-output-needle"):
            matches, _ = self.store.search(records, query, self.accounts)
            self.assertEqual([row.id for row in matches], [FIRST])
        with closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as db, db:
            db.execute("INSERT INTO thread_items VALUES (?,?,?)", (
                FIRST, 4, json.dumps({"type": "agentMessage", "text": "new-paginated-message"})))
        self.store.update_index(records)
        self.assertEqual(self.store.search(records, "new-paginated-message", self.accounts)[0], records)

    def test_search_cache_invalidates_when_a_new_message_arrives(self):
        records = [self.target()]
        self.store.update_index(records)
        self.assertEqual(len(self.store.search(records, "quartz-falcon", self.accounts)[0]), 1)
        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "brand-new-needle"}}) + "\n")
        self.store.update_index(records)
        self.assertEqual(len(self.store.search(records, "brand-new-needle", self.accounts)[0]), 1)
        self.rollout.write_text(self.rollout.read_text().replace("brand-new-needle", "replacement"))
        self.store.update_index(records)
        self.assertEqual(self.store.search(records, "brand-new-needle", self.accounts)[0], [])
        self.assertEqual(len(self.store.search(records, "replacement", self.accounts)[0]), 1)

    def test_fork_search_includes_inherited_prefix_but_not_later_parent_messages(self):
        target = self.target()
        end = self.rollout.stat().st_size
        with self.rollout.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "after-fork-must-not-match"}}) + "\n")
        fork = self.rollout.with_name("rollout-2026-11-15T12-00-00-" + SECOND + ".jsonl")
        fork.write_text(json.dumps({"type": "session_meta", "payload": {"id": SECOND, "history_base": {
            "thread_id": FIRST, "end_ordinal_exclusive": 2, "end_byte_offset": end}}}) + "\n" +
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "fork-own-message"}}) + "\n")
        record = replace(target, id=SECOND, title="Fork", path=str(fork))
        self.store.update_index([record])
        for query in ("quartz-falcon", "fork-own-message"):
            self.assertEqual([row.id for row in self.store.search([record], query, self.accounts)[0]], [SECOND])
        self.assertEqual(self.store.search([record], "after-fork-must-not-match", self.accounts)[0], [])

    def test_reverted_rollout_search_uses_its_immutable_history_identity(self):
        path = self.rollout.with_name("rollout-2026-11-15T12-00-00-" + FIRST + "_" + SECOND + ".jsonl")
        path.write_text(self.rollout.read_text())
        with closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_items (thread_id TEXT, rollout_ordinal INTEGER, item_json TEXT)")
            db.execute("INSERT INTO thread_items VALUES (?,?,?)", (SECOND, 1,
                       json.dumps({"type": "agentMessage", "text": "reverted-history-needle"})))
        record = replace(self.target(), path=str(path))
        self.store.update_index([record])
        self.assertEqual([row.id for row in self.store.search([record], "reverted-history-needle", self.accounts)[0]], [FIRST])

    def test_search_cancellation_does_not_publish_an_incomplete_result(self):
        cancelled = threading.Event()
        cancelled.set()
        self.assertIsNone(self.store.search(self.store.load(), "needle", self.accounts, cancelled))

    def test_rollout_only_installation_is_discovered_recursively(self):
        (self.home / "state_5.sqlite").unlink()
        (self.home / "session_index.jsonl").write_text(json.dumps({"id": FIRST, "thread_name": "Indexed title"}) + "\n")
        rows = self.store.load()
        self.assertEqual([row.id for row in rows], [FIRST])
        self.assertEqual(rows[0].title, "Indexed title")

    def test_resume_uses_current_login_even_when_creator_is_different_or_missing(self):
        for accounts in (self.accounts, []):
            plan = resume_plan(self.target(), accounts, str(self.fake), offline=True)
            self.assertEqual(plan.command, [str(self.fake), "--cd", str(self.nested), "resume", "--", FIRST])
        with self.assertRaisesRegex(CommandError, "without --offline"):
            resume_plan(replace(self.target(), archived=True), self.accounts, offline=True)
        with self.assertRaisesRegex(CommandError, "directory is unavailable"):
            resume_plan(replace(self.target(), cwd=str(self.home / "missing")), self.accounts)
        with self.assertRaisesRegex(CommandError, "Invalid thread ID"):
            resume_plan(replace(self.target(), id="--other-option"), self.accounts)

    def test_filters_sort_and_columns_work_and_preferences_survive_restart(self):
        renamer = load_renamer(PROJECT)
        view = ResumerView(self.home, renamer)
        try:
            view.archive, view.account = "all", "user-1::workspace"
            view.refresh(self.accounts)
            wait_for_view(view)
            self.assertEqual([row.id for row in view.visible], [SECOND])
            view.handle_key(curses.KEY_F7, 8)
            view.choice = list(__import__("hub_resumer").COLUMNS).index("model")
            view.handle_key(" ", 8)
            view.handle_key("\n", 8)
            self.assertIn("model", view.columns)
            view.handle_key(curses.KEY_F8, 8)
            view.choice = 2  # title
            view.handle_key("\n", 8)
            self.assertEqual(view.sort, "title")
            restored = ResumerView(self.home, renamer)
            self.assertEqual(restored.columns, view.columns)
            self.assertEqual(restored.sort, "title")
            restored.columns = list(__import__("hub_resumer").COLUMNS)
            for _ in range(5):
                restored.handle_key(curses.KEY_SRIGHT, 8)
            self.assertIn("model", [key for key, _ in restored.layout(74)])
            restored.close()
        finally:
            view.close()

    def test_two_tabs_resume_restore_terminal_without_switching_login(self):
        args = argparse.Namespace(codex_home=self.home, renamer=False, resumer=False, limit=200,
                                  codex_bin=str(self.fake), auth_bin=str(self.fake), offline=False,
                                  warning_days=7, interval=180)
        app = HubApp(args, load_renamer(PROJECT))
        app.screen = Mock()
        app.screen.getmaxyx.return_value = (24, 80)
        app.reload_local()
        try:
            app.handle_key(curses.KEY_F2)
            wait_for_view(app.resumer)
            self.assertEqual(app.tab, "threads")
            self.assertEqual(app.resumer.selected.id, FIRST)
            self.assertNotIn("account", app.resumer.columns)
            app.handle_key(curses.KEY_LEFT)
            self.assertEqual(app.tab, "accounts")
            app.handle_key(curses.KEY_LEFT)
            self.assertEqual(app.tab, "threads")
            app.handle_key(curses.KEY_RIGHT)
            self.assertEqual(app.tab, "accounts")
            app.handle_key("\t")
            self.assertEqual(app.tab, "threads")
            with patch.multiple("codex_hub.curses", def_prog_mode=Mock(), endwin=Mock(),
                                reset_prog_mode=Mock(), flushinp=Mock()), \
                    patch.object(app, "refresh"), patch.object(app, "check_daemon"), patch("sys.stdout", new=__import__("io").StringIO()):
                app.resume_selected()
            calls = [json.loads(line) for line in (self.home / "commands.jsonl").read_text().splitlines()]
            self.assertEqual(calls, [["--cd", str(self.nested), "resume", "--", FIRST]])
            self.assertEqual(registry_snapshot(self.home)[0]["active"], True)
            self.assertFalse(app.action)
            app.screen.clearok.assert_called_with(True)
        finally:
            app.close()

    def test_control_r_renames_the_selected_thread_in_the_combined_view(self):
        args = argparse.Namespace(codex_home=self.home, renamer=True, limit=1,
                                  codex_bin=str(self.fake), auth_bin=str(self.fake), offline=True,
                                  warning_days=7, interval=180)
        app = HubApp(args, load_renamer(PROJECT))
        app.screen = Mock()
        app.screen.getmaxyx.return_value = (24, 80)
        try:
            app.set_tab(app.tab)
            wait_for_view(app.resumer)
            self.assertEqual(len(app.resumer.visible), 4)  # No legacy renamer page limit.
            app.handle_key("\x12")
            self.assertEqual(app.threads.mode, "rename")
            for key in [*"Door fixed", curses.KEY_LEFT, "X", "\n"]:
                app.handle_key(key)
            self.assertEqual(app.tab, "threads")
            app.threads.pending.result(timeout=3)
            app.finish_thread_actions()
            wait_for_view(app.resumer)
            self.assertEqual(app.resumer.selected.title, "Door fixeXd")
            self.assertTrue(app.status.startswith("Renamed:"))
            self.assertFalse((self.home / "commands.jsonl").exists())
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
