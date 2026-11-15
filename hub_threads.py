"""Local, account-independent thread discovery, content search and resume plans."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import threading

from hub_accounts import CommandError, clean


@contextmanager
def read_only(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


def session_meta(path):
    if not path:
        return {}
    try:
        with Path(path).open(encoding="utf-8") as stream:
            item = json.loads(stream.readline(16 * 1024 * 1024))
        return item.get("payload", {}) if item.get("type") == "session_meta" else {}
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
            if not isinstance(base, dict) or not base.get("thread_id"):
                return list(reversed(result))
            identity = base["thread_id"]
            ordinal, end = base.get("end_ordinal_exclusive"), base.get("end_byte_offset")
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
                result.append(SavedThread(
                    id=meta["id"], title=clean(names.get(meta["id"]) or meta.get("title") or meta["id"]),
                    cwd=meta.get("cwd") or "", updated=int(path.stat().st_mtime), source=str(meta.get("source") or ""),
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
                        rows = db.execute("SELECT item_json FROM thread_items WHERE thread_id=? AND (? IS NULL OR rollout_ordinal < ?) ORDER BY rollout_ordinal", (identity, ordinal, ordinal))
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

    def search(self, records, query, accounts, cancelled=None, progress=None, full_text=True):
        cancelled = cancelled or threading.Event()
        if cancelled.is_set():
            return None
        needles = terms(query)
        if not needles:
            return records, {}, 0, {}
        cache = None
        if full_text:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.cache_dir / "thread-search.sqlite"
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                os.fchmod(descriptor, 0o600)
                os.close(descriptor)
                cache = sqlite3.connect(path, timeout=2)
                cache.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, text TEXT NOT NULL)")
                if "prompts" not in {row[1] for row in cache.execute("PRAGMA table_info(documents)")}:
                    cache.execute("ALTER TABLE documents ADD COLUMN prompts TEXT NOT NULL DEFAULT ''")
                    cache.commit()
            except (OSError, sqlite3.Error):
                if cache:
                    cache.close()
                cache = None
        matches, snippets, unreadable, ranks = [], {}, 0, {}
        try:
            for index, thread in enumerate(records):
                if cancelled.is_set():
                    return None
                metadata = thread.metadata_text(account_label(thread, accounts))
                title = thread.title.casefold()
                body = prompts = ""
                if full_text and not all(needle in title for needle in needles):
                    try:
                        segments = self.history_segments(thread)
                        fingerprints = []
                        missing_file = False
                        for identity, path, ordinal, end in segments:
                            try:
                                stat = Path(path).stat() if path else None
                            except FileNotFoundError:
                                stat = None
                            fingerprints.append(f"{identity}:{ordinal}:{end}:{stat.st_mtime_ns if stat else 0}:{stat.st_size if stat else 0}")
                            missing_file = missing_file or stat is None
                        fingerprint = "ranked-v2:" + str(thread.updated) + ":" + ";".join(fingerprints)
                        if missing_file:
                            for path in (self.home / "thread_history_1.sqlite", self.home / "thread_history_1.sqlite-wal"):
                                if path.exists():
                                    fingerprint += f":{path.stat().st_mtime_ns}"
                        hit = cache.execute("SELECT prompts, text FROM documents WHERE id=? AND fingerprint=?", (thread.id, fingerprint)).fetchone() if cache else None
                        if hit:
                            prompts, body = hit
                        else:
                            parts = self.content_parts(thread, cancelled, segments)
                            if parts is None:
                                return None
                            prompts, body = parts
                            if cache:
                                cache.execute("INSERT OR REPLACE INTO documents (id, fingerprint, text, prompts) VALUES (?,?,?,?)", (thread.id, fingerprint, body, prompts))
                    except (OSError, sqlite3.Error):
                        unreadable += 1
                folded = body.casefold()
                if all(needle in metadata or needle in folded for needle in needles):
                    matches.append(thread)
                    prompt_text = prompts.casefold()
                    ranks[thread.id] = (-sum(needle in title for needle in needles),
                                        -sum(needle in prompt_text for needle in needles),
                                        -sum(needle in folded for needle in needles))
                    excerpt = prompts if any(needle in prompt_text for needle in needles) else body
                    position = next((excerpt.casefold().find(needle) for needle in needles if needle in excerpt.casefold()), 0)
                    snippets[thread.id] = clean(excerpt[max(0, position - 55):position + 160])
                if (index + 1) % 20 == 0 or index + 1 == len(records):
                    if cache:
                        cache.commit()
                    if progress:
                        progress(index + 1, len(records))
            matches.sort(key=lambda row: (ranks[row.id], -row.updated, row.id))
            return matches, snippets, unreadable, ranks
        finally:
            if cache:
                cache.commit()
                cache.close()


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
