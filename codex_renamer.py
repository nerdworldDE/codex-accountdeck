#!/usr/bin/env python3
"""A small terminal interface for renaming local Codex threads."""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import locale
import os
from pathlib import Path
from queue import Empty, Queue
import re
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata

from codex_colors import thread_color

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import curses
except ImportError:
    curses = None  # type: ignore[assignment]


VERSION = "0.1.2"
SOURCES = ["cli", "vscode", "appServer"]


class CodexError(Exception):
    """An actionable connection or protocol error."""


class RpcError(CodexError):
    pass


def one_line(value: str) -> str:
    """Keep terminal control characters and multiline previews out of the UI."""
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())


def cell_width(value: str) -> int:
    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
        for c in value
    )


def clip(value: str, width: int) -> str:
    result = []
    used = 0
    for char in value:
        size = cell_width(char)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def fit(value: str, width: int) -> str:
    value = clip(value, width)
    return value + " " * max(0, width - cell_width(value))


class TerminalColors:
    """Use exact RGB colors where ncurses can redefine palette entries."""

    def __init__(self):
        self.styles = {}
        self.saved = {}

    @staticmethod
    def nearest(rgb, count):
        candidates = []
        if count >= 256:
            levels = (0, 95, 135, 175, 215, 255)
            candidates = [
                (16 + 36 * red + 6 * green + blue, (levels[red], levels[green], levels[blue]))
                for red in range(6) for green in range(6) for blue in range(6)
            ]
            candidates.extend((232 + index, (8 + 10 * index,) * 3) for index in range(24))
        else:
            for index in range(min(count, 16)):
                try:
                    candidates.append((index, tuple(round(value * 255 / 1000) for value in curses.color_content(index))))
                except curses.error:
                    pass
        if not candidates:
            return -1
        return min(candidates, key=lambda item: sum((left - right) ** 2 for left, right in zip(rgb, item[1])))[0]

    def attribute(self, rgb):
        if rgb in self.styles:
            return self.styles[rgb]
        attribute = 0
        try:
            if not curses.has_colors():
                return 0
            pair = len(self.styles) + 3
            if pair >= curses.COLOR_PAIRS:
                return 0
            color = len(self.styles) + 16
            if color < curses.COLORS and curses.can_change_color():
                self.saved[color] = curses.color_content(color)
                curses.init_color(color, *(round(value * 1000 / 255) for value in rgb))
            else:
                color = self.nearest(rgb, curses.COLORS)
            curses.init_pair(pair, color, -1)
            attribute = curses.color_pair(pair)
        except curses.error:
            pass
        self.styles[rgb] = attribute
        return attribute

    def restore(self):
        for color, original in self.saved.items():
            try:
                curses.init_color(color, *original)
            except curses.error:
                pass
        self.saved.clear()


@dataclass(frozen=True)
class Thread:
    id: str
    name: str
    preview: str
    cwd: str
    updated_at: int

    @classmethod
    def from_rpc(cls, value: dict) -> Thread:
        try:
            if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                raise ValueError("Missing thread ID")
            for key in ("name", "preview", "cwd"):
                if value.get(key) is not None and not isinstance(value[key], str):
                    raise ValueError(f"Invalid {key}")
            return cls(
                id=value["id"],
                name=value.get("name") or "",
                preview=value.get("preview") or "",
                cwd=value.get("cwd") or "",
                updated_at=int(value["updatedAt"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodexError("Codex hat unvollständige Thread-Daten geliefert.") from exc

    @property
    def title(self) -> str:
        return one_line(self.name or self.preview) or "(Ohne Namen)"

    @property
    def project(self) -> str:
        return one_line(Path(self.cwd).name or self.cwd) or "–"

    @property
    def last_active(self) -> str:
        try:
            return datetime.fromtimestamp(self.updated_at).strftime("%d.%m.%Y %H:%M")
        except (ValueError, OSError, OverflowError):
            return "–"


class AppServer:
    """Local Codex metadata with a sequential JSON-RPC compatibility fallback.

    Known schemas load and save names without a server. The protocol fallback
    reuses a running daemon when possible, otherwise a private stdio process.
    No conversation is resumed and no model request is made by this tool.
    """

    def __init__(self, binary="codex", codex_home=None, standalone=False, timeout=5.0):
        self.binary = binary
        self.home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
        self.standalone = standalone
        self.timeout = timeout
        self.process: subprocess.Popen | None = None
        self.messages: Queue = Queue()
        self.stderr: deque[str] = deque(maxlen=8)
        self.readers: list[threading.Thread] = []
        self.sequence = 0
        self.initialized = False
        self.stopping = threading.Event()
        self.lifecycle = threading.RLock()

    @property
    def connected(self) -> bool:
        return self.initialized and self.process is not None and self.process.poll() is None

    @staticmethod
    def _read_messages(stream, messages):
        try:
            for line in stream:
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("Expected an object")
                    messages.put(message)
                except ValueError:
                    messages.put(CodexError("Ungültige Antwort vom Codex App Server."))
                    return
        except (OSError, ValueError):
            pass
        finally:
            messages.put(None)
            stream.close()

    @staticmethod
    def _read_stderr(stream, lines):
        try:
            for line in stream:
                lines.append(line.strip())
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    def _start(self, proxy: bool):
        if self.stopping.is_set():
            raise CodexError("Verbindung geschlossen.")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.home)
        command = [self.binary, "app-server"]
        command += ["proxy"] if proxy else ["--listen", "stdio://"]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1, env=env,
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError as exc:
            raise CodexError(f"Codex nicht gefunden: {self.binary}. Nutze ggf. --codex-bin PFAD.") from exc
        except OSError as exc:
            raise CodexError(f"Codex konnte nicht gestartet werden: {exc}") from exc
        with self.lifecycle:
            self.process = process
            if self.stopping.is_set():
                self.close()
                raise CodexError("Verbindung geschlossen.")
            self.messages = Queue()
            self.stderr = deque(maxlen=8)
            self.readers = [
                threading.Thread(target=self._read_messages, args=(process.stdout, self.messages), daemon=True),
                threading.Thread(target=self._read_stderr, args=(process.stderr, self.stderr), daemon=True),
            ]
            for reader in self.readers:
                reader.start()
        self.request("initialize", {"clientInfo": {
            "name": "codex_renamer", "title": "Codex Renamer", "version": VERSION,
        }})
        self._send({"method": "initialized", "params": {}})
        self.initialized = True

    def connect(self):
        if self.stopping.is_set():
            raise CodexError("Verbindung geschlossen.")
        if self.connected:
            return
        self.close()
        if not self.home.is_dir():
            raise CodexError(f"Codex-Verzeichnis nicht gefunden: {self.home}")
        socket = self.home / "app-server-control" / "app-server-control.sock"
        if not self.standalone and socket.is_socket():
            try:
                self._start(proxy=True)
                return
            except CodexError:
                self.close()
        try:
            self._start(proxy=False)
        except BaseException:
            self.close()
            raise

    def _send(self, message: dict):
        process = self.process
        if self.stopping.is_set() or process is None or process.stdin is None:
            raise CodexError("Keine Verbindung zu Codex. Mit r erneut verbinden.")
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CodexError("Die Verbindung zu Codex wurde geschlossen. Mit r erneut verbinden.") from exc

    def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request_id = self.sequence
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Empty
                message = self.messages.get(timeout=remaining)
            except Empty as exc:
                self.close()
                raise CodexError("Codex antwortet nicht. Mit r den aktuellen Stand laden.") from exc
            if message is None:
                details = one_line(" ".join(self.stderr))
                raise CodexError("Codex App Server wurde beendet." + (f" {details}" if details else ""))
            if isinstance(message, Exception):
                self.close()
                raise message
            if "method" in message:
                if "id" in message:
                    # Listing/renaming never requires tools, approvals, or inference.
                    self._send({"id": message["id"], "error": {
                        "code": -32601, "message": "This client only supports thread management",
                    }})
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise RpcError(f"Codex: {error.get('message', 'Unbekannter Fehler')} ({error.get('code', '?')})")
            if not isinstance(message.get("result"), dict):
                raise CodexError("Codex hat eine unvollständige Antwort geliefert.")
            return message["result"]

    def _database_path(self) -> Path | None:
        databases = [
            (int(path.stem[6:]), path) for path in self.home.glob("state_*.sqlite")
            if path.stem[6:].isdigit()
        ]
        return max(databases)[1] if databases else None

    def _list_local_threads(self, limit: int, archived: bool) -> list[Thread] | None:
        """Read an existing metadata database without starting a Codex process.

        Unknown/legacy schemas use the protocol fallback. The read-only URI
        also prevents a misspelled path from creating a new database.
        """
        path = self._database_path()
        if path is None:
            return None
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if not {"id", "cwd", "updated_at", "source", "archived"}.issubset(columns):
                return None
            name = "COALESCE(name, '')" if "name" in columns else "''"
            previews = [f"NULLIF({key}, '')" for key in ("title", "preview", "first_user_message") if key in columns]
            preview = "COALESCE(" + ", ".join(previews + ["''"]) + ")" if previews else "''"
            sort = "COALESCE(updated_at_ms, updated_at * 1000)" if "updated_at_ms" in columns else "updated_at"
            # Expressions above are built exclusively from known column names.
            rows = connection.execute(
                f"SELECT id, {name} AS name, {preview} AS preview, "
                "COALESCE(cwd, '') AS cwd, updated_at AS updatedAt FROM threads "
                "WHERE source IN ('cli', 'vscode', 'appServer', 'app-server') AND archived = ? "
                f"ORDER BY {sort} DESC, id DESC LIMIT ?",
                (int(archived), limit),
            ).fetchall()
            threads = [Thread.from_rpc(dict(row)) for row in rows]
        except sqlite3.Error as exc:
            raise CodexError(f"Lokale Thread-Liste konnte nicht gelesen werden: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

        # Older Codex versions kept user-assigned names only in this index.
        missing = {thread.id for thread in threads if not thread.name}
        names = {}
        if missing:
            try:
                with (self.home / "session_index.jsonl").open(encoding="utf-8") as index:
                    for line in index:
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        thread_id, title = entry.get("id"), entry.get("thread_name")
                        if isinstance(thread_id, str) and thread_id in missing and isinstance(title, str) and title.strip():
                            names[thread_id] = title
            except (OSError, UnicodeError):
                pass
        return [replace(thread, name=names.get(thread.id, thread.name)) for thread in threads]

    def list_threads(self, limit: int, archived=False) -> list[Thread]:
        if self.stopping.is_set():
            raise CodexError("Verbindung geschlossen.")
        local = self._list_local_threads(limit, archived)
        if local is not None:
            return local
        self.connect()
        threads = []
        ids = set()
        cursors = set()
        cursor = None
        while len(threads) < limit:
            params = {
                "limit": min(100, limit - len(threads)), "sortKey": "updated_at",
                "sortDirection": "desc", "modelProviders": [],
                "sourceKinds": SOURCES, "archived": archived,
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = self.request("thread/list", params)
            if not isinstance(result.get("data"), list):
                raise CodexError("Codex hat keine gültige Thread-Liste geliefert.")
            for value in result["data"]:
                thread = Thread.from_rpc(value)
                if thread.id not in ids and len(threads) < limit:
                    threads.append(thread)
                    ids.add(thread.id)
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in cursors:
                raise CodexError("Codex hat einen wiederholten Seitencursor geliefert.")
            cursors.add(cursor)
        return threads

    def rename(self, thread_id: str, name: str) -> bool:
        name = name.strip()
        if not name:
            return False
        if self.stopping.is_set():
            raise CodexError("Verbindung geschlossen.")
        local = self._rename_local(thread_id, name)
        if local is not None:
            return local
        self.connect()
        self.request("thread/name/set", {"threadId": thread_id, "name": name})
        return True

    def _rename_local(self, thread_id: str, name: str) -> bool | None:
        """Persist only naming metadata; conversation logs are never rewritten.

        Codex 0.166's legacy threads use `title`; paginated threads use `name`.
        Both formats also append to session_index.jsonl. Leave the other field
        untouched so a later native rename cannot expose a stale custom name.
        SQLite's write lock and a bounded index lock prevent an unresponsive
        app-server or another renamer instance from blocking the interface.
        """
        path = self._database_path()
        if path is None:
            return None
        connection = None
        descriptor = None
        lock_descriptor = None
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            name_fields = [field for field in ("title", "name") if field in columns]
            if "id" not in columns or not name_fields:
                return None
            # Acquire the SQLite lock before creating or changing the index.
            connection.execute("BEGIN IMMEDIATE")
            mode_field = "history_mode" if "history_mode" in columns else "NULL"
            row = connection.execute(f"SELECT id, {mode_field} FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if row is None:
                raise CodexError("Der Thread ist nicht mehr vorhanden. Mit r die Liste aktualisieren.")
            if row[1] in ("legacy", "paginated"):
                field = "title" if row[1] == "legacy" else "name"
                if field not in columns:
                    return None
                name_fields = [field]
            elif row[1] is not None:
                return None
            if fcntl is not None:
                # Codex uses this sidecar for its own index writers as well.
                lock_descriptor = os.open(self.home / "session_index.jsonl.lock", os.O_WRONLY | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise CodexError("Der Namensindex ist gerade belegt. Bitte erneut speichern.") from exc
            descriptor = os.open(self.home / "session_index.jsonl", os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            end = os.lseek(descriptor, 0, os.SEEK_END)
            prefix = b""
            if end:
                os.lseek(descriptor, -1, os.SEEK_END)
                if os.read(descriptor, 1) != b"\n":
                    prefix = b"\n"
            record = prefix + (json.dumps({
                "id": thread_id, "thread_name": name,
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }, ensure_ascii=False) + "\n").encode("utf-8")
            assignments = ", ".join(f"{field} = ?" for field in name_fields)
            connection.execute(f"UPDATE threads SET {assignments} WHERE id = ?", [name] * len(name_fields) + [thread_id])
            if os.write(descriptor, record) != len(record):
                raise CodexError("Der Namensindex konnte nicht vollständig gespeichert werden.")
            os.fsync(descriptor)
            connection.commit()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise CodexError("Codex aktualisiert gerade die Metadaten. Bitte erneut speichern.") from exc
            raise CodexError(f"Name konnte nicht gespeichert werden: {exc}") from exc
        except (sqlite3.Error, OSError) as exc:
            raise CodexError(f"Name konnte nicht gespeichert werden: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()
            if descriptor is not None:
                os.close(descriptor)
            if lock_descriptor is not None:
                os.close(lock_descriptor)
        return True

    def close(self):
        with self.lifecycle:
            self.initialized = False
            process, self.process = self.process, None
            if process is None:
                return
            self.messages.put(None)
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait()
            for reader in self.readers:
                reader.join(timeout=0.5)
            # Readers own stdout/stderr. Closing a TextIOWrapper from here can
            # wait forever on a reader's lock if a descendant retains the pipe.
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass

    def shutdown(self):
        self.stopping.set()
        self.close()


class LineEditor:
    """A single-line Unicode editor; each rename starts with an empty value."""

    def __init__(self, initial=""):
        self.value = initial
        self.cursor = len(initial)
        self.start = 0

    def feed(self, key):
        if key in ("\n", "\r", curses.KEY_ENTER):
            return "submit"
        if key == "\x1b":
            return "cancel"
        if key in (curses.KEY_LEFT, "\x02"):
            self.cursor = max(0, self.cursor - 1)
        elif key in (curses.KEY_RIGHT, "\x06"):
            self.cursor = min(len(self.value), self.cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            self.cursor = 0
        elif key in (curses.KEY_END, "\x05"):
            self.cursor = len(self.value)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if self.cursor:
                self.value = self.value[:self.cursor - 1] + self.value[self.cursor:]
                self.cursor -= 1
        elif key in (curses.KEY_DC, "\x04"):
            self.value = self.value[:self.cursor] + self.value[self.cursor + 1:]
        elif key == "\x15":
            self.value, self.cursor = "", 0
        elif key == "\x17":
            before = self.value[:self.cursor].rstrip()
            position = before.rfind(" ") + 1
            self.value = self.value[:position] + self.value[self.cursor:]
            self.cursor = position
        elif isinstance(key, str) and key.isprintable():
            self.value = self.value[:self.cursor] + key + self.value[self.cursor:]
            self.cursor += len(key)
        return None

    def viewport(self, width):
        self.start = min(self.start, self.cursor)
        while cell_width(self.value[self.start:self.cursor]) >= max(1, width):
            self.start += 1
        return clip(self.value[self.start:], width), cell_width(self.value[self.start:self.cursor])


class RenamerApp:
    def __init__(self, client: AppServer, limit=200, archived=False):
        self.client = client
        self.limit = limit
        self.archived = archived
        self.threads: list[Thread] = []
        self.visible: list[Thread] = []
        self.index = 0
        self.offset = 0
        self.query = ""
        self.status = "Threads werden geladen …"
        self.error = False
        self.mode = None
        self.editor = LineEditor()
        self.target: Thread | None = None
        self.pending: Future | None = None
        self.pending_kind = ""
        self.pending_name = ""
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-renamer")
        self.accent = 0
        self.failure = 0
        self.background = (0, 0, 0)
        self.colors = TerminalColors()

    def thread_style(self, thread):
        return self.colors.attribute(thread_color(thread.id, self.background))

    def refilter(self, selected_id=None):
        query = self.query.casefold()
        self.visible = [t for t in self.threads if query in f"{t.title} {t.cwd} {t.id}".casefold()]
        self.index = next((i for i, t in enumerate(self.visible) if t.id == selected_id), min(self.index, max(0, len(self.visible) - 1)))

    def refresh(self):
        if self.pending:
            return
        self.status, self.error = "Threads werden geladen …", False
        self.pending_kind = "refresh"
        self.pending = self.worker.submit(self.client.list_threads, self.limit, self.archived)

    def finish_pending(self):
        if not self.pending or not self.pending.done():
            return
        future, self.pending = self.pending, None
        try:
            result = future.result()
        except (CodexError, OSError) as exc:
            self.status, self.error = one_line(str(exc)), True
            if self.pending_kind == "rename":
                self.mode, self.editor = "rename", LineEditor(self.pending_name)
            return
        selected_id = self.visible[self.index].id if self.visible else None
        if self.pending_kind == "refresh":
            self.threads = result
            self.status = f"{len(self.threads)} Threads geladen. Neueste Aktivität zuerst."
        else:
            self.threads = [replace(t, name=self.pending_name) if t.id == self.target.id else t for t in self.threads]
            self.status = f"Umbenannt: {self.pending_name}"
        self.error = False
        self.refilter(selected_id)

    @staticmethod
    def put(window, y, x, text, attr=0, width=None):
        height, columns = window.getmaxyx()
        if not (0 <= y < height and 0 <= x < columns - 1):
            return
        width = min(columns - x - 1, width if width is not None else columns)
        try:
            window.addstr(y, x, clip(text, width), attr)
        except curses.error:
            pass  # A resize can occur between getmaxyx() and addstr().

    def draw(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        if width < 48 or height < 14:
            self.set_cursor(0)
            self.put(screen, 0, 0, "Terminal bitte auf mindestens 48 × 14 vergrößern.")
            self.put(screen, 2, 0, "q / ESC: Beenden")
            screen.refresh()
            return
        self.set_cursor(1 if self.mode else 0)
        heading = "CODEX RENAMER" + (" · Archiv" if self.archived else "")
        self.put(screen, 0, 2, heading, curses.A_BOLD | self.accent)
        self.put(screen, 1, 2, "↑↓ Auswahl  Enter Umbenennen  / Suche  r Neu laden  q Ende", curses.A_DIM)
        summary = f"{self.index + 1 if self.visible else 0}/{len(self.visible)}"
        if self.query:
            summary += f" · Suche: {one_line(self.query)}"
        self.put(screen, 2, 2, summary, self.accent)

        available = width - 4
        project_width = min(28, max(10, available // 4))
        title_width = available - 16 - project_width - 4
        header = fit("ZULETZT AKTIV", 16) + "  " + fit("NAME", title_width) + "  " + fit("PROJEKT", project_width)
        self.put(screen, 4, 2, header, curses.A_BOLD | self.accent)
        page_size = height - 11
        self.offset = min(self.offset, self.index)
        if self.index >= self.offset + page_size:
            self.offset = self.index - page_size + 1
        for row, thread in enumerate(self.visible[self.offset:self.offset + page_size]):
            selected = self.offset + row == self.index
            text = fit(thread.last_active, 16) + "  " + fit(thread.title, title_width) + "  " + fit(thread.project, project_width)
            style = self.thread_style(thread)
            if selected:
                style |= curses.A_BOLD | curses.A_UNDERLINE
                self.put(screen, 5 + row, 0, "›", style)
            self.put(screen, 5 + row, 2, text, style)
        if not self.visible:
            message = "Keine passenden Threads." if self.query else "Keine Threads gefunden."
            if self.pending and self.pending_kind == "refresh":
                message = "Threads werden geladen …"
            self.put(screen, 6, 2, message, curses.A_DIM)

        self.put(screen, height - 5, 2, "─" * available, curses.A_DIM)
        if self.visible:
            thread = self.visible[self.index]
            self.put(screen, height - 4, 2, thread.title, curses.A_BOLD | self.thread_style(thread))
            self.put(screen, height - 3, 2, one_line(thread.cwd), curses.A_DIM)
            self.put(screen, height - 2, 2, thread.id, curses.A_DIM)
        self.put(screen, height - 1, 2, one_line(self.status), self.failure if self.error else self.accent)
        screen.noutrefresh()
        if self.mode:
            self.draw_editor(screen)
        curses.doupdate()

    def draw_editor(self, screen):
        height, width = screen.getmaxyx()
        columns = min(width - 4, 94)
        window = curses.newwin(9, columns, (height - 9) // 2, (width - columns) // 2)
        window.erase()
        window.box()
        is_rename = self.mode == "rename"
        self.put(window, 1, 2, "Thread umbenennen" if is_rename else "Threads durchsuchen", curses.A_BOLD | self.accent)
        description = f"Bisher: {self.target.title}" if is_rename else "Suche in Name, Projektpfad und Thread-ID"
        self.put(window, 2, 2, description, self.thread_style(self.target) if is_rename else curses.A_DIM)
        text, cursor = self.editor.viewport(columns - 6)
        self.put(window, 4, 2, "> " + fit(text, columns - 6), curses.A_REVERSE)
        hint = "Enter: Speichern · Leer / ESC: Abbrechen" if is_rename else "Enter: Filtern · ESC: Abbrechen · Leer: Alle"
        self.put(window, 6, 2, hint, curses.A_DIM)
        if self.error:
            self.put(window, 7, 2, self.status, self.failure)
        window.move(4, 4 + cursor)
        window.noutrefresh()

    @staticmethod
    def set_cursor(visible):
        try:
            curses.curs_set(visible)
        except curses.error:
            pass

    def handle_key(self, key, page_size):
        if self.mode:
            action = self.editor.feed(key)
            if action == "cancel":
                self.mode = None
                self.status, self.error = "Eingabe abgebrochen.", False
            elif action == "submit":
                if self.mode == "search":
                    self.query = self.editor.value.strip()
                    self.index, self.offset = 0, 0
                    self.refilter()
                    self.status, self.error = f"{len(self.visible)} passende Threads.", False
                elif self.editor.value.strip():
                    self.pending_name = self.editor.value.strip()
                    self.pending_kind = "rename"
                    self.pending = self.worker.submit(self.client.rename, self.target.id, self.pending_name)
                    self.status, self.error = "Name wird gespeichert …", False
                else:
                    self.status, self.error = "Eingabe abgebrochen.", False
                self.mode = None
            return True
        if key in ("q", "Q", "\x1b"):
            return False
        if key in (curses.KEY_UP, "k"):
            self.index = max(0, self.index - 1)
        elif key in (curses.KEY_DOWN, "j"):
            self.index = min(max(0, len(self.visible) - 1), self.index + 1)
        elif key == curses.KEY_PPAGE:
            self.index = max(0, self.index - page_size)
        elif key == curses.KEY_NPAGE:
            self.index = min(max(0, len(self.visible) - 1), self.index + page_size)
        elif key == curses.KEY_HOME:
            self.index = 0
        elif key == curses.KEY_END:
            self.index = max(0, len(self.visible) - 1)
        elif key in ("\n", "\r", curses.KEY_ENTER) and self.visible and not self.pending:
            self.target = self.visible[self.index]
            self.mode, self.editor = "rename", LineEditor()
            self.error = False
        elif key == "/" and not self.pending:
            self.mode, self.editor = "search", LineEditor(self.query)
            self.error = False
        elif key in ("r", "R"):
            self.refresh()
        return True

    def run(self, screen):
        screen.keypad(True)
        screen.timeout(100)
        curses.set_escdelay(25)
        try:
            curses.curs_set(0)
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            self.accent, self.failure = curses.color_pair(1), curses.color_pair(2)
        except curses.error:
            pass
        self.background = self.terminal_background()
        try:
            self.event_loop(screen)
        finally:
            self.colors.restore()

    @staticmethod
    def terminal_background():
        """Ask once, with a short deadline; return early keystrokes to ncurses."""
        data = b""
        background = (0, 0, 0)
        response = re.compile(rb"\x1b\]11;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})(?:\x07|\x1b\\)")
        try:
            descriptor = sys.stdin.fileno()
            sys.stdout.write("\x1b]11;?\x07")
            sys.stdout.flush()
            deadline = time.monotonic() + 0.1
            while time.monotonic() < deadline:
                if not select.select([descriptor], [], [], max(0, deadline - time.monotonic()))[0]:
                    break
                chunk = os.read(descriptor, 128 - len(data))
                if not chunk:
                    break
                data += chunk
                match = response.search(data)
                if match:
                    background = tuple(round(int(value, 16) * 255 / (16 ** len(value) - 1)) for value in match.groups())
                    data = data[:match.start()] + data[match.end():]
                    break
                if len(data) >= 128:
                    break
        except (OSError, ValueError):
            pass
        finally:
            for byte in reversed(data):
                try:
                    curses.ungetch(byte)
                except curses.error:
                    break
        return background

    def event_loop(self, screen):
        self.refresh()
        while True:
            self.finish_pending()
            self.draw(screen)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key == curses.KEY_RESIZE:
                continue
            height, width = screen.getmaxyx()
            if width < 48 or height < 14:
                if key in ("q", "\x1b"):
                    break
                continue
            if not self.handle_key(key, max(1, height - 11)):
                break

    def close(self):
        # Stop a pending request as well, so exiting never waits for its timeout.
        self.client.shutdown()
        self.worker.shutdown(wait=True, cancel_futures=True)
        self.client.close()


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("muss größer als 0 sein")
    return number


def configure_locale():
    # C.UTF-8 is common on Linux, en_US.UTF-8 on macOS and Homebrew. Python's
    # UTF-8 mode alone does not configure the C library used by ncurses.
    for candidate in ("", "C.UTF-8", "en_US.UTF-8"):
        try:
            locale.setlocale(locale.LC_ALL, candidate)
            encoding = locale.getlocale(locale.LC_CTYPE)[1] or ""
            if encoding.replace("-", "").lower() == "utf8":
                return
        except locale.Error:
            continue
    raise CodexError("Keine UTF-8-Locale verfügbar. Bitte eine UTF-8-Locale für das Terminal aktivieren.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex-Threads im Terminal schnell umbenennen.")
    parser.add_argument("--limit", type=positive_int, default=200, help="Anzahl zuletzt aktiver Threads (Standard: 200)")
    parser.add_argument("--archived", action="store_true", help="archivierte Threads anzeigen")
    parser.add_argument("--codex-home", type=Path, help="Codex-Datenverzeichnis (Standard: CODEX_HOME oder ~/.codex)")
    parser.add_argument("--codex-bin", default="codex", help="Pfad zum Codex-CLI-Programm")
    parser.add_argument("--standalone", action="store_true", help="eigenen App Server statt eines laufenden Daemons verwenden")
    parser.add_argument("--list", action="store_true", help="Threads nur ausgeben, ohne interaktive Oberfläche")
    parser.add_argument("--version", action="version", version=f"codex-renamer {VERSION}")
    args = parser.parse_args(argv)
    if not args.list and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        print("Bitte in einem interaktiven Terminal starten. Für reine Ausgabe: --list", file=sys.stderr)
        return 2
    if not args.list and curses is None:
        print("Python mit curses wird benötigt (unter Linux und macOS normalerweise enthalten).", file=sys.stderr)
        return 2
    client = AppServer(args.codex_bin, args.codex_home, args.standalone)
    app = None
    try:
        if args.list:
            for thread in client.list_threads(args.limit, args.archived):
                print(f"{thread.last_active}  {fit(thread.project, 24)}  {thread.id}  {thread.title}")
        else:
            configure_locale()
            app = RenamerApp(client, args.limit, args.archived)
            curses.wrapper(app.run)
    except KeyboardInterrupt:
        return 130
    except (CodexError, OSError) as exc:
        print(f"Fehler: {one_line(str(exc))}", file=sys.stderr)
        return 1
    except Exception as exc:
        if curses is not None and isinstance(exc, curses.error):
            print(f"Terminal konnte nicht initialisiert werden: {exc}", file=sys.stderr)
            return 1
        raise
    finally:
        if app:
            app.close()
        else:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
