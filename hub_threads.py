"""Local, account-independent thread discovery, content search and resume plans."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime
import json
from itertools import chain, islice
import os
from pathlib import Path
import re
import shlex
import sqlite3
import threading
import time

from hub_accounts import CommandError, clean


@contextmanager
def read_only(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        yield connection


def session_meta(path):
    if not path:
        return {}
    try:
        with Path(path).open(encoding="utf-8") as stream:
            item = json.loads(stream.readline(16 * 1024 * 1024))
        payload = item.get("payload")
        return payload if item.get("type") == "session_meta" and isinstance(payload, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def number(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class SavedThread:
    id: str
    title: str
    cwd: str = ""
    updated: int = 0
    created: int = 0
    model: str = ""
    source: str = ""
    archived: bool = False
    tokens: int = 0
    user_id: str = ""
    account_id: str = ""
    path: str = ""
    agent: bool = False

    @property
    def project(self):
        return Path(self.cwd).name or self.cwd or "—"

    @property
    def owner_key(self):
        return f"{self.user_id}::{self.account_id}" if self.user_id and self.account_id else ""

    def metadata_text(self, account=""):
        return " ".join((self.title, self.cwd, self.id, self.model, self.source, account)).casefold()


def in_directory(directory, parent):
    """Match a directory and its descendants, without matching sibling prefixes."""
    if not parent.strip():
        return True
    if not directory:
        return False
    child = Path(directory).expanduser().resolve()
    root = Path(parent).expanduser().resolve()
    try:
        if os.path.commonpath((str(child), str(root))) == str(root):
            return True
        # WSL drive paths can spell the same directory with different casing.
        return any(root.samefile(candidate) for candidate in (child, *child.parents))
    except (ValueError, OSError):
        return False


def account_label(thread, accounts):
    account = next((a for a in accounts if a["account_key"] == thread.owner_key), None)
    return clean(account.get("alias") or account.get("email") or "Saved login") if account else ("Other login" if thread.owner_key else "Legacy / unknown")


def terms(query):
    try:
        return [word.casefold() for word in shlex.split(query) if word]
    except ValueError:
        return [query.casefold()] if query else []


def search_text(text):
    return text.casefold().replace("\0", " ")


def short_grams(text):
    # A small exact lookup for one- and two-character searches, which FTS5's
    # trigram tokenizer cannot answer. Delimit whole grams for BLOB instr().
    chars = set(text)
    pairs = set(zip(text, islice(text, 1, None)))
    return b"\0" + b"\0".join(gram.encode("utf-8") for gram in chain(chars, map("".join, pairs))) + b"\0"


def text_parts(value):
    """Extract textual content, excluding image data, paths and transport metadata."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from text_parts(item)
    elif isinstance(value, dict):
        for key in ("text", "content", "output", "aggregatedOutput"):
            if key in value:
                yield from text_parts(value[key])


def message_text(item):
    kind = item.get("type", "")
    if kind in ("userMessage", "agentMessage", "user_message", "agent_message"):
        yield from text_parts(item.get("content", item.get("text", item.get("message", ""))))
    elif kind == "message" and item.get("role") in ("user", "assistant"):
        yield from text_parts(item.get("content", []))
    elif kind == "commandExecution":
        yield from text_parts(item.get("command", ""))
        yield from text_parts(item.get("aggregatedOutput", ""))
    elif kind == "mcpToolCall":
        yield from text_parts(item.get("result", {}))
    elif kind in ("function_call", "custom_tool_call"):
        yield from text_parts(item.get("arguments", item.get("input", "")))
    elif kind in ("function_call_output", "custom_tool_call_output"):
        yield from text_parts(item.get("output", ""))
    elif kind in ("exec_command_end", "mcp_tool_call_end"):
        yield from text_parts(item.get("aggregated_output", item.get("result", {})))
    elif kind == "fileChange":
        for change in item.get("changes", []):
            if isinstance(change, dict):
                yield from text_parts(change.get("diff", ""))


class ThreadStore:
    def __init__(self, home, cache_dir=None):
        self.home = Path(home).expanduser().resolve()
        self.cache_dir = Path(cache_dir) if cache_dir else self.home / "accountdeck"
        self.cache_path = self.cache_dir / "thread-search.sqlite"
        self.paths = {}

    @staticmethod
    def rollout_id(path, fallback):
        ids = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", Path(path).name if path else "")
        return ids[-1] if ids else fallback

    def history_segments(self, thread):
        """Include the inherited prefix of paginated forks, bounded at fork time."""
        path, identity, ordinal, end = thread.path, self.rollout_id(thread.path, thread.id), None, None
        result, seen = [], set()
        while identity not in seen:
            seen.add(identity)
            result.append((identity, path, ordinal, end))
            base = session_meta(path).get("history_base")
            if not isinstance(base, dict) or not isinstance(base.get("thread_id"), str) or not base["thread_id"]:
                return list(reversed(result))
            identity = base["thread_id"]
            ordinal, end = base.get("end_ordinal_exclusive"), base.get("end_byte_offset")
            if any(bound is not None and (not isinstance(bound, int) or bound < 0) for bound in (ordinal, end)):
                raise OSError("Invalid inherited transcript boundary")
            path = self.paths.get(identity, "")
            if not path:
                for collection in ("sessions", "archived_sessions"):
                    for candidate in (self.home / collection).rglob("rollout-*.jsonl"):
                        self.paths[self.rollout_id(str(candidate), "")] = str(candidate)
                path = self.paths.get(identity, "")
            if not path:
                raise OSError("Inherited transcript is unavailable")
        raise OSError("Cyclic inherited transcript")

    def database(self):
        paths = [(int(p.stem[6:]), p) for p in self.home.glob("state_*.sqlite") if p.stem[6:].isdigit()]
        return max(paths)[1] if paths else None

    def load(self):
        path = self.database()
        names = {}
        try:
            with (self.home / "session_index.jsonl").open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict) and item.get("thread_name"):
                            names[item["id"]] = item["thread_name"]
                    except (ValueError, KeyError):
                        continue
        except (OSError, UnicodeError):
            pass
        if path:
            try:
                with read_only(path) as db:
                    rows = db.execute("SELECT * FROM threads").fetchall()
                result = []
                for row in rows:
                    data = dict(row)
                    source = data.get("source") or ""
                    agent = source.startswith("{") or source.startswith("sub") or bool(data.get("agent_path") or data.get("attached_thread_parent_id"))
                    result.append(SavedThread(
                        id=str(data["id"]),
                        title=clean(data.get("name") or names.get(data["id"]) or data.get("title") or data.get("preview") or data["id"]),
                        cwd=data.get("cwd") or "", updated=number(data.get("updated_at")),
                        created=number(data.get("created_at")), model=data.get("model") or "",
                        source="subagent" if agent else source, archived=bool(data.get("archived")),
                        tokens=number(data.get("tokens_used")), user_id=data.get("creator_user_id") or "",
                        account_id=data.get("creator_account_id") or "", path=data.get("rollout_path") or "", agent=agent,
                    ))
                for thread in result:
                    if thread.path:
                        self.paths[self.rollout_id(thread.path, thread.id)] = thread.path
                return result
            except sqlite3.Error as error:
                raise CommandError("Cannot read local thread database: " + clean(error)) from error
        result = []
        for collection, archived in (("sessions", False), ("archived_sessions", True)):
            for path in (self.home / collection).rglob("rollout-*.jsonl"):
                meta = session_meta(path)
                if not meta.get("id"):
                    continue
                try:
                    updated = int(path.stat().st_mtime)
                except OSError:
                    continue
                result.append(SavedThread(
                    id=meta["id"], title=clean(names.get(meta["id"]) or meta.get("title") or meta["id"]),
                    cwd=meta.get("cwd") or "", updated=updated, source=str(meta.get("source") or ""),
                    archived=archived, user_id=meta.get("creator_user_id") or "",
                    account_id=meta.get("creator_account_id") or "", path=str(path),
                    agent=bool(meta.get("parent_thread_id") or meta.get("agent_path")),
                ))
        return result

    def content_parts(self, thread, cancelled, segments=None):
        """Paginated history is canonical; older installations use rollout JSONL."""
        history = self.home / "thread_history_1.sqlite"
        chunks, prompts = [], []
        def append(item, segment, questions):
            text = list(message_text(item))
            segment.extend(text)
            if item.get("type") in ("userMessage", "user_message") or (item.get("type") == "message" and item.get("role") == "user"):
                questions.extend(text)
        for identity, path, ordinal, end in segments or self.history_segments(thread):
            segment, questions = [], []
            if history.exists():
                try:
                    with read_only(history) as db:
                        scope, parameters = self.history_scope(identity, ordinal)
                        rows = db.execute(f"SELECT item_json FROM thread_items WHERE {scope} ORDER BY rollout_ordinal", parameters)
                        for row in rows:
                            if cancelled.is_set():
                                return None
                            try:
                                append(json.loads(row[0]), segment, questions)
                            except (ValueError, AttributeError):
                                continue
                except sqlite3.Error:
                    pass
            if not segment and path:
                with Path(path).open("rb") as stream:
                    while end is None or stream.tell() < end:
                        if cancelled.is_set():
                            return None
                        line = stream.readline()
                        if not line:
                            break
                        try:
                            record = json.loads(line)
                            if record.get("type") in ("event_msg", "response_item"):
                                append(record.get("payload", {}), segment, questions)
                        except (ValueError, AttributeError):
                            continue
            chunks.extend(segment)
            prompts.extend(questions)
        return "\n".join(prompts), "\n".join(chunks)

    def content(self, thread, cancelled, segments=None):
        parts = self.content_parts(thread, cancelled, segments)
        return parts[1] if parts is not None else None

    @staticmethod
    def history_scope(identity, ordinal):
        if ordinal is None:
            return "thread_id=?", (identity,)
        return "thread_id=? AND rollout_ordinal < ?", (identity, ordinal)

    def fingerprints(self, thread, segments, history, has_revisions):
        rollouts, files, revisions = [], [], []
        missing_file = False
        for identity, path, ordinal, end in segments:
            try:
                stat = Path(path).stat() if path else None
            except FileNotFoundError:
                stat = None
            rollouts.append((identity, ordinal, end, stat.st_mtime_ns if stat else 0, stat.st_size if stat else 0))
            files.append((stat.st_dev, stat.st_ino) if stat else None)
            missing_file |= stat is None
            if history:
                scope, parameters = self.history_scope(identity, ordinal)
                maximum = history.execute(f"SELECT max(rollout_ordinal) FROM thread_items WHERE {scope}", parameters).fetchone()[0]
                revision = (history.execute(f"SELECT max(updated_at_ordinal) FROM thread_items WHERE {scope}", parameters).fetchone()[0]
                            if maximum is not None and has_revisions else None)
                revisions.append((maximum, revision))
        # Keep recognizing the previous cache so the first upgrade can index
        # its text without rereading unchanged transcripts.
        legacy_parts = ";".join(":".join(map(str, part)) for part in rollouts)
        legacy = f"ranked-v2:{thread.updated}:{legacy_parts}"
        if missing_file:
            for name in ("thread_history_1.sqlite", "thread_history_1.sqlite-wal"):
                try:
                    legacy += f":{(self.home / name).stat().st_mtime_ns}"
                except FileNotFoundError:
                    continue
        fingerprint = json.dumps((3, rollouts, files, revisions), separators=(",", ":"))
        return fingerprint, legacy

    @contextmanager
    def cache_writer(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.cache_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with closing(sqlite3.connect(self.cache_path, timeout=2)) as cache:
            cache.execute("PRAGMA journal_mode=WAL")
            cache.execute("PRAGMA synchronous=NORMAL")
            with cache:
                cache.execute("BEGIN IMMEDIATE")
                cache.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, text TEXT NOT NULL, prompts TEXT NOT NULL)")
                if "prompts" not in {row[1] for row in cache.execute("PRAGMA table_info(documents)")}:
                    cache.execute("ALTER TABLE documents ADD COLUMN prompts TEXT NOT NULL DEFAULT ''")
                cache.execute("CREATE TABLE IF NOT EXISTS search_terms (document INTEGER PRIMARY KEY, body BLOB NOT NULL, prompts BLOB NOT NULL)")
                try:
                    # The cached originals live in documents. FTS stores just
                    # the case-folded index, including positions for exact phrases.
                    cache.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_text USING fts5(body, prompts, content='', tokenize='trigram case_sensitive 1')")
                    cache.execute("SELECT rowid FROM search_text WHERE search_text MATCH 'abc' LIMIT 1").fetchone()
                    fts = True
                except sqlite3.OperationalError as error:
                    if "no such module" not in str(error) and "no such tokenizer" not in str(error):
                        raise
                    fts = False
                version = 1 if fts else 2
                if cache.execute("PRAGMA user_version").fetchone()[0] != version:
                    if fts:
                        cache.execute("INSERT INTO search_text(search_text) VALUES('delete-all')")
                        cache.execute("DELETE FROM search_terms")
                    cache.execute(f"PRAGMA user_version={version}")
            yield cache, fts

    @staticmethod
    def remove_from_index(cache, fts, document, old_text, old_prompts):
        if fts:
            cache.execute("INSERT INTO search_text(search_text, rowid, body, prompts) VALUES('delete', ?, ?, ?)",
                          (document, search_text(old_text), search_text(old_prompts)))
        cache.execute("DELETE FROM search_terms WHERE document=?", (document,))

    def update_document(self, cache, fts, thread, history, has_revisions, cancelled):
        segments = self.history_segments(thread)
        fingerprint, legacy = self.fingerprints(thread, segments, history, has_revisions)
        cached = cache.execute("""SELECT d.rowid, d.fingerprint, t.document IS NOT NULL
                                FROM documents d LEFT JOIN search_terms t ON t.document=d.rowid
                                WHERE d.id=?""", (thread.id,)).fetchone()
        if cached and cached[1] == fingerprint and cached[2]:
            return False
        if cached and cached[1] in (fingerprint, legacy):
            prompts, body = cache.execute("SELECT prompts, text FROM documents WHERE rowid=?", (cached[0],)).fetchone()
        else:
            parts = self.content_parts(thread, cancelled, segments)
            if parts is None:
                return False
            prompts, body = parts
        folded_body, folded_prompts = search_text(body), search_text(prompts)
        short_body, short_prompts = short_grams(folded_body), short_grams(folded_prompts)
        if cancelled.is_set():
            return False
        with cache:
            cache.execute("BEGIN IMMEDIATE")
            old = cache.execute("""SELECT d.rowid, d.fingerprint, t.document IS NOT NULL
                                  FROM documents d LEFT JOIN search_terms t ON t.document=d.rowid
                                  WHERE d.id=?""", (thread.id,)).fetchone()
            if old != cached:
                return False  # Another AccountDeck indexed this document in the meantime.
            if old and old[2]:
                old_text, old_prompts = cache.execute("SELECT text, prompts FROM documents WHERE rowid=?", (old[0],)).fetchone()
                self.remove_from_index(cache, fts, old[0], old_text, old_prompts)
            if old:
                document = old[0]
                if cached[1] in (fingerprint, legacy):
                    cache.execute("UPDATE documents SET fingerprint=? WHERE rowid=?", (fingerprint, document))
                else:
                    cache.execute("UPDATE documents SET fingerprint=?, text=?, prompts=? WHERE rowid=?",
                                  (fingerprint, body, prompts, document))
            else:
                document = cache.execute("INSERT INTO documents(id, fingerprint, text, prompts) VALUES(?, ?, ?, ?)",
                                         (thread.id, fingerprint, body, prompts)).lastrowid
            cache.execute("INSERT INTO search_terms VALUES(?, ?, ?)", (document, short_body, short_prompts))
            if fts:
                cache.execute("INSERT INTO search_text(rowid, body, prompts) VALUES(?, ?, ?)",
                              (document, folded_body, folded_prompts))
        return True

    def prune_index(self, cache, fts, records, cancelled):
        current = {row.id for row in records}
        for (identity,) in cache.execute("SELECT id FROM documents").fetchall():
            if cancelled.is_set():
                break
            if identity in current:
                continue
            with cache:
                cache.execute("BEGIN IMMEDIATE")
                old = cache.execute("""SELECT d.rowid, d.text, d.prompts, t.document IS NOT NULL
                                     FROM documents d LEFT JOIN search_terms t ON t.document=d.rowid
                                     WHERE d.id=?""", (identity,)).fetchone()
                if old:
                    if old[3]:
                        self.remove_from_index(cache, fts, *old[:3])
                    cache.execute("DELETE FROM documents WHERE rowid=?", (old[0],))

    def update_index(self, records, cancelled=None, progress=None, prune=False):
        """Refresh the persistent index independently of any search query."""
        cancelled = cancelled or threading.Event()
        if cancelled.is_set() or not (records or self.cache_path.is_file()):
            return 0
        unreadable, changed, last_report = 0, False, 0.0
        history_path = self.home / "thread_history_1.sqlite"
        with ExitStack() as stack:
            history, has_revisions = None, False
            if history_path.is_file():
                db = stack.enter_context(read_only(history_path))
                columns = {row[1] for row in db.execute("PRAGMA table_info(thread_items)")}
                if {"thread_id", "rollout_ordinal"} <= columns:
                    history, has_revisions = db, "updated_at_ordinal" in columns
            cache, fts = stack.enter_context(self.cache_writer())
            for done, thread in enumerate(sorted(records, key=lambda row: row.updated, reverse=True), 1):
                if cancelled.is_set():
                    return unreadable
                try:
                    changed |= self.update_document(cache, fts, thread, history, has_revisions, cancelled)
                except (OSError, UnicodeError):
                    unreadable += 1
                now = time.monotonic()
                if progress and (done == len(records) or now - last_report >= .5):
                    progress(done, len(records), changed)
                    changed, last_report = False, now
            if prune and not cancelled.is_set():
                self.prune_index(cache, fts, records, cancelled)
        return unreadable

    def cached_hits(self, records, needles, cancelled):
        if not self.cache_path.is_file():
            return {}, {}
        ids = {row.id for row in records}
        with read_only(self.cache_path) as cache:
            cache.set_progress_handler(cancelled.is_set, 1000)
            cache.execute("BEGIN")  # All terms must see the same index revision.
            version = cache.execute("PRAGMA user_version").fetchone()[0]
            if not version:
                return {}, {}
            documents = {row[0]: row[1] for row in cache.execute("SELECT rowid, id FROM documents") if row[1] in ids}
            body, prompts = {}, {}
            for needle in set(needles):
                if len(needle) < 3:
                    gram = b"\0" + needle.encode("utf-8") + b"\0"
                    for field, result in (("body", body), ("prompts", prompts)):
                        result[needle] = {documents[row[0]] for row in cache.execute(
                            f"SELECT document FROM search_terms WHERE instr({field}, ?)", (gram,)) if row[0] in documents}
                    continue
                if version == 1:
                    phrase = '"' + needle.replace('"', '""') + '"'
                    try:
                        for field, result in (("body", body), ("prompts", prompts)):
                            result[needle] = {documents[row[0]] for row in cache.execute(
                                "SELECT rowid FROM search_text WHERE search_text MATCH ?", (f"{field}: {phrase}",))
                                              if row[0] in documents}
                        continue
                    except sqlite3.OperationalError as error:
                        if "no such module" not in str(error) and "no such tokenizer" not in str(error):
                            raise
                # Older SQLite builds still search cached text without touching
                # the Codex databases or reading any rollout files.
                body[needle], prompts[needle] = set(), set()
                for row in cache.execute("SELECT rowid, text, prompts FROM documents"):
                    if cancelled.is_set():
                        return {}, {}
                    document, text, prompt = row
                    if document in documents:
                        if needle in search_text(text):
                            body[needle].add(documents[document])
                        if needle in search_text(prompt):
                            prompts[needle].add(documents[document])
            return body, prompts

    def search(self, records, query, accounts, cancelled=None, full_text=True):
        cancelled = cancelled or threading.Event()
        if cancelled.is_set():
            return None
        needles = terms(query)
        if not needles:
            return list(records), {}
        try:
            body, prompts = self.cached_hits(records, needles, cancelled) if full_text else ({}, {})
        except sqlite3.Error:
            if cancelled.is_set():
                return None
            raise
        matches, ranks = [], {}
        for thread in records:
            metadata = thread.metadata_text(account_label(thread, accounts))
            if all(needle in metadata or thread.id in body.get(needle, ()) for needle in needles):
                matches.append(thread)
                title = thread.title.casefold()
                ranks[thread.id] = (-sum(needle in title for needle in needles),
                                    -sum(thread.id in prompts.get(needle, ()) for needle in needles),
                                    -sum(thread.id in body.get(needle, ()) for needle in needles))
        matches.sort(key=lambda row: (ranks[row.id], -row.updated, row.id))
        return (matches, ranks) if not cancelled.is_set() else None

    def excerpt(self, identity, query, prompt):
        field = "prompts" if prompt else "text"
        with read_only(self.cache_path) as cache:
            row = cache.execute(f"SELECT {field} FROM documents WHERE id=?", (identity,)).fetchone()
        if row is None:
            return ""
        text = row[0]
        folded = search_text(text)
        position = next((folded.find(needle) for needle in terms(query) if needle in folded), -1)
        return clean(text[max(0, position - 55):position + 160]) if position >= 0 else ""


@dataclass(frozen=True)
class ResumePlan:
    command: list[str]
    cwd: str
    archived: bool


def resume_plan(thread, accounts, binary="codex", offline=False):
    if thread is None:
        raise CommandError("Select a thread to resume")
    if not thread.id or thread.id.startswith("-"):
        raise CommandError("Invalid thread ID")
    if thread.agent:
        raise CommandError("Resume the parent conversation for this subagent or attached chat")
    if not thread.cwd or not Path(thread.cwd).is_dir():
        raise CommandError("Project directory is unavailable: " + clean(thread.cwd))
    if offline and thread.archived:
        raise CommandError("Archived thread · restart without --offline to restore and resume")
    return ResumePlan([binary, "--cd", thread.cwd, "resume", "--", thread.id], thread.cwd, thread.archived)


def time_label(value):
    try:
        return datetime.fromtimestamp(value).strftime("%d %b %H:%M") if value else "—"
    except (OSError, ValueError, OverflowError):
        return "—"
