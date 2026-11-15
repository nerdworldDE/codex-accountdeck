"""Thread browser UI; slow content searches stay off the curses event loop."""
from __future__ import annotations

import curses
import json
import os
from pathlib import Path
from queue import Empty, Queue
import threading
import time

from hub_accounts import clean
from hub_threads import ThreadStore, account_label, in_directory, time_label

COLUMNS = {
    "updated": ("LAST ACTIVE", 16, 12), "title": ("TITLE", 45, 18),
    "cwd": ("DIRECTORY", 32, 14), "account": ("LOGIN", 25, 12),
    "created": ("CREATED", 16, 12), "model": ("MODEL", 20, 12),
    "tokens": ("TOKENS", 12, 8), "source": ("SOURCE", 12, 8),
    "archived": ("STATUS", 10, 8), "id": ("THREAD ID", 36, 12),
}
DEFAULT_COLUMNS = ["updated", "title", "cwd"]
SORTS = ["updated", "created", "title", "cwd", "account", "model", "tokens", "source"]


class ResumerView:
    def __init__(self, home, renamer, directory=""):
        self.store, self.renamer = ThreadStore(home), renamer
        self.records, self.visible, self.accounts, self.snippets, self.ranks = [], [], [], {}, {}
        self.index = self.offset = self.choice = 0
        self.column_offset = 0
        self.mode = self.query = ""
        self.editor = None
        self.search_editor = renamer.LineEditor()
        self.search_due, self.loaded = 0.0, False
        self.filter_draft, self.filter_choice = None, 0
        self.options, self.option_editor = [], renamer.LineEditor()
        self.directory, self.archive, self.account, self.source = directory, "active", "", ""
        self.include_agents, self.full_text = False, True
        self.columns, self.sort, self.reverse = list(DEFAULT_COLUMNS), "updated", True
        self.status, self.error, self.pending = "Press F5 to load threads", False, False
        self.messages, self.generation, self.cancelled = Queue(), 0, threading.Event()
        self.workers = []
        self.settings = self.store.home / "accountdeck" / "resumer.json"
        try:
            data = json.loads(self.settings.read_text())
            columns = list(dict.fromkeys(key for key in data.get("columns", []) if key in COLUMNS))
            if data.get("version", 1) < 2:
                columns = [key for key in columns if key != "account"]
            if columns:
                self.columns = columns
            if data.get("sort") in SORTS:
                self.sort = data["sort"]
            self.reverse = bool(data.get("reverse", True))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @property
    def selected(self):
        return self.visible[self.index] if self.visible else None

    def save_settings(self):
        temporary = None
        try:
            self.settings.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.settings.with_suffix(f".{os.getpid()}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"version": 2, "columns": self.columns, "sort": self.sort, "reverse": self.reverse}, stream)
            temporary.replace(self.settings)
        except OSError:
            self.status, self.error = "View works, but column/sort preferences could not be saved", True
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def sort_visible(self, selected_id=None):
        def value(row):
            field = account_label(row, self.accounts) if self.sort == "account" else getattr(row, self.sort)
            return field.casefold() if isinstance(field, str) else field
        self.visible.sort(key=lambda row: (value(row), row.id), reverse=self.reverse)
        if self.query.strip():
            # Stable sorting keeps the chosen column as the tie-breaker.
            self.visible.sort(key=lambda row: self.ranks.get(row.id, (0, 0, 0)))
        self.index = next((i for i, row in enumerate(self.visible) if row.id == selected_id), 0)
        self.index = min(self.index, max(0, len(self.visible) - 1))
        self.offset = min(self.offset, self.index)

    def refresh(self, accounts=None, reload=True, preserve_selection=True):
        if accounts is not None:
            self.accounts = list(accounts)
        self.search_due = 0.0
        reload = reload or not self.loaded
        selected_id = self.selected.id if preserve_selection and self.selected else None
        self.cancelled.set()
        self.cancelled = cancelled = threading.Event()
        self.generation += 1
        generation = self.generation
        self.pending, self.error = True, False
        self.status = "Loading local threads…" if reload else "Searching titles, then prompts and conversation text…"
        # Capture mutable view state before the worker starts.
        old_records, accounts, query, full_text = self.records, list(self.accounts), self.query, self.full_text
        directory, archive, account, source, agents = self.directory, self.archive, self.account, self.source, self.include_agents
        def work():
            try:
                records = self.store.load() if reload else old_records
                if cancelled.is_set():
                    return
                candidates = [row for row in records if (agents or not row.agent)
                              and (archive == "all" or row.archived == (archive == "archived"))
                              and (not account or row.owner_key == account)
                              and (not source or row.source == source)
                              and (not directory or in_directory(row.cwd, directory))]
                def progress(done, total):
                    self.messages.put((generation, "progress", f"Searching thread text · {done}/{total} indexed"))
                result = self.store.search(candidates, query, accounts, cancelled, progress, full_text)
                if result is not None and not cancelled.is_set():
                    self.messages.put((generation, "done", (records, result, selected_id)))
            except Exception as error:
                if not cancelled.is_set():
                    self.messages.put((generation, "error", clean(error)))
        self.workers = [worker for worker in self.workers if worker.is_alive()]
        worker = threading.Thread(target=work, daemon=True, name="accountdeck-thread-search")
        self.workers.append(worker)
        worker.start()

    def finish_pending(self):
        if self.search_due and time.monotonic() >= self.search_due:
            self.refresh(reload=False, preserve_selection=False)
        while True:
            try:
                generation, kind, payload = self.messages.get_nowait()
            except Empty:
                return
            if generation != self.generation:
                continue
            if kind == "progress":
                self.status = payload
            elif kind == "error":
                self.pending, self.error, self.status = False, True, payload
            else:
                self.records, (self.visible, self.snippets, unreadable, self.ranks), selected_id = payload
                self.loaded = True
                self.sort_visible(selected_id)
                self.pending, self.error = False, bool(unreadable)
                self.status = f"{len(self.visible)} matches / {len(self.records)} local threads"
                if self.query.strip():
                    self.status += " · title → prompt → text"
                if unreadable:
                    self.status += f" · {unreadable} conversations could not be searched"

    def value(self, row, key):
        if key in ("updated", "created"):
            return time_label(getattr(row, key))
        if key == "account":
            return account_label(row, self.accounts)
        if key == "tokens":
            return f"{row.tokens:,}"
        if key == "archived":
            return "Archived" if row.archived else "Active"
        if key == "cwd":
            parts = Path(row.cwd).parts
            return "…/" + str(Path(*parts[-3:])) if len(parts) > 4 else clean(row.cwd or "—")
        return clean(getattr(row, key) or "—")

    def layout(self, available):
        columns = self.columns[min(self.column_offset, len(self.columns) - 1):]
        widths = [COLUMNS[key][1] for key in columns]
        while sum(widths) + 3 * (len(widths) - 1) > available:
            shrinkable = [i for i, key in enumerate(columns) if widths[i] > COLUMNS[key][2]]
            if not shrinkable:
                break
            widest = max(shrinkable, key=lambda i: widths[i])
            widths[widest] -= 1
        result, used = [], 0
        for key, width in zip(columns, widths):
            if used + width > available:
                break
            result.append((key, width))
            used += width + 3
        return result

    @staticmethod
    def page_size(height):
        return max(1, height - (19 if height >= 22 else 15))

    def queue_search(self):
        self.cancelled.set()
        self.generation += 1
        self.search_due = time.monotonic() + .12
        self.pending, self.error = True, False
        self.status = "Searching titles, then prompts and conversation text…"
        self.index = self.offset = 0

    def short_path(self, path, width):
        text = clean(path)
        if self.renamer.cell_width(text) <= width:
            return text
        while text and self.renamer.cell_width(text) > max(0, width - 1):
            text = text[1:]
        return "…" + text

    def draw(self, screen, line, style, thread_style):
        height, width = screen.getmaxyx()
        line(4, 2, f"{len(self.visible)} THREADS", width - 4, style("text") | curses.A_BOLD)
        order = "RELEVANCE" if self.query.strip() else COLUMNS[self.sort][0]
        order += "  ↓" if self.reverse else "  ↑"
        line(4, max(18, width - len(order) - 3), order, len(order), style("purple"))

        line(5, 2, "╭" + "─" * (width - 6) + "╮", width - 4, style("accent"))
        line(5, 5, " SEARCH · type to filter ", 26, style("accent") | curses.A_BOLD)
        active = self.filter_count()
        button = f" F6 Filters{f' · {active}' if active else ''} "
        line(5, width - len(button) - 5, button, len(button), style("amber" if active else "accent"))
        line(6, 2, "│", 1, style("accent"))
        line(6, width - 3, "│", 1, style("accent"))
        if self.search_editor.value != self.query:
            self.search_editor = self.renamer.LineEditor(self.query)
        text, _ = self.search_editor.viewport(width - 11)
        line(6, 4, "› " + (text or "Search titles, prompts and conversations…"), width - 8,
             style("text" if text else "muted"))
        line(7, 2, "╰" + "─" * (width - 6) + "╯", width - 4, style("accent"))
        scope = self.archive.title() + " · " + ("Full text" if self.full_text else "Titles & paths")
        if self.directory:
            suffix = " (+ subfolders)"
            path_width = width - 14 - self.renamer.cell_width(scope + " · " + suffix)
            scope += " · " + self.short_path(self.directory, max(1, path_width)) + suffix
        if self.account:
            scope += " · Login filter"
        if self.source:
            scope += " · " + self.source
        if self.include_agents:
            scope += " · Agents included"
        line(7, 5, " " + scope + " ", width - 12, style("muted"))

        bottom, details = height - 4, height >= 22
        line(8, 2, "╭" + "─" * (width - 6) + "╮", width - 4, style("border"))
        line(8, 5, " CONVERSATIONS ", 15, style("muted"))
        page = self.page_size(height)
        self.offset = min(self.offset, self.index)
        if self.index >= self.offset + page:
            self.offset = self.index - page + 1
        showing = f" {self.offset + 1 if self.visible else 0}–{min(len(self.visible), self.offset + page)} / {len(self.visible)} "
        line(8, width - len(showing) - 5, showing, len(showing), style("muted"))
        for y in range(9, bottom):
            line(y, 2, "│", 1, style("border"))
            line(y, width - 3, "│", 1, style("border"))
        columns = self.layout(width - 10)
        x = 5
        for position, (key, size) in enumerate(columns):
            label = COLUMNS[key][0] + ((" ↓" if self.reverse else " ↑") if key == self.sort else "")
            line(9, x, self.renamer.fit(label, size), size, style("accent" if key == self.sort else "muted") | curses.A_BOLD)
            x += size
            if position + 1 < len(columns):
                line(9, x, " │ ", 3, style("border"))
            x += 3
        line(10, 2, "├" + "─" * (width - 6) + "┤", width - 4, style("border"))
        for index, row in enumerate(self.visible[self.offset:self.offset + page]):
            selected = self.offset + index == self.index
            attr = thread_style(row) | (curses.A_BOLD | curses.A_REVERSE if selected else 0)
            y = 11 + index
            if selected:
                line(y, 3, " " * (width - 6), width - 6, attr)
                line(y, 3, "›", 1, attr)
            x = 5
            for position, (key, size) in enumerate(columns):
                line(y, x, self.renamer.fit(self.value(row, key), size), size, attr)
                x += size
                if position + 1 < len(columns):
                    line(y, x, " │ ", 3, attr if selected else style("border"))
                x += 3
        if not self.visible:
            line(11, 5, "Searching…" if self.pending else "No matches · clear the search or adjust F6 Filters", width - 10, style("muted"))
        row = self.selected
        if details:
            line(height - 8, 2, "├" + "─" * (width - 6) + "┤", width - 4, style("border"))
            if row:
                line(height - 7, 4, row.title, width - 8, thread_style(row) | curses.A_BOLD)
                line(height - 6, 4, row.cwd, width - 8, style("muted"))
                snippet = self.snippets.get(row.id, "")
                if self.query.strip():
                    rank = self.ranks.get(row.id, (0, 0, 0))
                    match = "TITLE" if rank[0] else "PROMPT" if rank[1] else "TEXT" if rank[2] else "PATH / METADATA"
                    detail = match + " · " + (snippet or row.id)
                else:
                    detail = row.id + (" · Archived" if row.archived else "")
                line(height - 5, 4, detail, width - 8, style("accent"))
        line(bottom, 2, "╰" + "─" * (width - 6) + "╯", width - 4, style("border"))
        if self.column_offset or len(columns) < len(self.columns):
            hint = " Shift+←/→ scroll columns "
            line(bottom, width - len(hint) - 5, hint, len(hint), style("muted"))

    def place_cursor(self, screen):
        _, width = screen.getmaxyx()
        _, cursor = self.search_editor.viewport(width - 11)
        screen.move(6, 6 + cursor)

    @staticmethod
    def default_filters():
        return dict(directory="", archive="active", account="", source="", include_agents=False)

    def current_filters(self):
        return {key: getattr(self, key) for key in self.default_filters()}

    def filter_count(self):
        defaults = self.default_filters()
        return sum(value != defaults[key] for key, value in self.current_filters().items())

    def filter_rows(self, values=None):
        values = self.current_filters() if values is None else values
        account = next((a for a in self.accounts if a["account_key"] == values["account"]), {})
        login = clean(account.get("alias") or account.get("email") or "Other login") if values["account"] else "All logins"
        return [("Directory", values["directory"] or "All directories"),
                ("Status", values["archive"].title()), ("Login", login),
                ("Source", values["source"] or "All sources"),
                ("Subagents / side chats", "Included" if values["include_agents"] else "Hidden")]

    def apply_filters(self):
        for key, value in self.filter_draft.items():
            setattr(self, key, value)
        self.mode, self.filter_draft = "", None
        self.refresh(reload=False)

    def open_options(self):
        self.filter_choice = self.choice
        if self.choice == 0:
            directories = {row.cwd for row in self.records if row.cwd}
            for directory in list(directories):
                parent = str(Path(directory).parent)
                if parent not in (".", "/"):
                    directories.add(parent)
            self.options = [("All directories", ""), ("Enter a directory path…", None)] + [(p, p) for p in sorted(directories)]
        elif self.choice == 1:
            self.options = [("Active threads", "active"), ("Active and archived threads", "all"), ("Archived threads", "archived")]
        elif self.choice == 2:
            self.options = [("All logins", "")] + [(clean(a.get("alias") or a.get("email") or a["account_key"]), a["account_key"]) for a in self.accounts]
        elif self.choice == 3:
            sources = sorted({row.source for row in self.records if row.source} | {"cli", "vscode", "appServer", "exec", "subagent"})
            self.options = [("All sources", "")] + [(source, source) for source in sources]
        else:
            self.options = [("Hide subagents and attached side chats", False), ("Include subagents and attached side chats", True)]
        self.option_editor = self.renamer.LineEditor()
        key = list(self.default_filters())[self.filter_choice]
        self.choice = next((i for i, (_, value) in enumerate(self.options) if value == self.filter_draft[key]), 0)
        self.mode = "options"

    def matching_options(self):
        words = self.option_editor.value.casefold().split()
        return [(label, value) for label, value in self.options if all(word in label.casefold() for word in words)]

    def modal_window(self, screen, count, title, style):
        height, width = screen.getmaxyx()
        columns = min(width - 6, 98)
        window = curses.newwin(count, columns, (height - count) // 2, (width - columns) // 2)
        window.bkgd(" ", style("text"))
        put = self.renamer.RenamerApp.put
        put(window, 0, 0, "╭" + "─" * (columns - 3) + "╮", style("accent"), columns)
        for y in range(1, count - 1):
            put(window, y, 0, "│", style("accent"), 1)
            put(window, y, columns - 2, "│", style("accent"), 1)
        put(window, count - 1, 0, "╰" + "─" * (columns - 3) + "╯", style("accent"), columns)
        put(window, 1, 2, title, style("accent") | curses.A_BOLD, columns - 4)
        return window, columns, put

    def draw_modal(self, screen, style):
        height, width = screen.getmaxyx()
        if self.mode == "filters":
            count = min(height - 2, 20)
            window, columns, put = self.modal_window(screen, count, "Thread filters", style)
            put(window, 2, 2, "Choose a field to see its options. Changes apply together.", style("muted"), columns - 5)
            for index, (label, value) in enumerate(self.filter_rows(self.filter_draft)):
                y, selected = 4 + index * 2, index == self.choice
                attr = style("accent" if selected else "text") | (curses.A_BOLD if selected else 0)
                put(window, y, 2, "› " if selected else "  ", attr, 2)
                put(window, y, 4, label, attr, 22)
                put(window, y, 27, value, style("green") | (curses.A_REVERSE if selected else 0), columns - 33)
                put(window, y, columns - 5, "›", attr, 1)
                if index < 4:
                    put(window, y + 1, 3, "─" * (columns - 7), style("border"), columns - 7)
            x = 3
            for index, label in enumerate((" Apply · F6 ", " Reset · Ctrl+L ", " Cancel · Esc ")):
                put(window, count - 3, x, label, style("accent") | (curses.A_REVERSE if self.choice == index + 5 else 0), len(label))
                x += len(label) + 2
            put(window, count - 2, 3, "↑↓ / Tab Select · Enter Open / Apply · Esc Cancel", style("muted"), columns - 7)
        elif self.mode == "options":
            count = min(height - 2, 20)
            title = self.filter_rows(self.filter_draft)[self.filter_choice][0]
            window, columns, put = self.modal_window(screen, count, "Choose " + title.lower(), style)
            put(window, 2, 2, "Type to narrow choices · ↑↓ Select · Enter Choose · Esc Back", style("muted"), columns - 5)
            put(window, 3, 2, "› " + (self.option_editor.value or "Filter choices…"), style("accent"), columns - 5)
            put(window, 4, 2, "─" * (columns - 5), style("border"), columns - 5)
            rows = self.matching_options()
            capacity = max(1, count - 9)
            start = max(0, self.choice - capacity + 1)
            key = list(self.default_filters())[self.filter_choice]
            for index, (label, value) in enumerate(rows[start:start + capacity]):
                selected = start + index == self.choice
                marker = "● " if value == self.filter_draft[key] else "○ "
                put(window, 5 + index, 3, marker + label, style("accent" if selected else "text") | (curses.A_REVERSE if selected else 0), columns - 7)
            if not rows:
                put(window, 5, 3, "No matching choices", style("muted"), columns - 7)
            detail = rows[self.choice][0] if rows else ""
            put(window, count - 3, 3, detail, style("muted"), columns - 7)
            put(window, count - 2, 3, "Directory choices include subfolders." if self.filter_choice == 0 else "The filled circle marks the current filter.", style("muted"), columns - 7)
        elif self.mode == "directory":
            window, columns, put = self.modal_window(screen, 9, "Directory filter", style)
            put(window, 2, 2, "Includes this directory and every subfolder. Empty = all.", style("muted"), columns - 5)
            text, cursor = self.editor.viewport(columns - 9)
            put(window, 4, 2, "> " + self.renamer.fit(text, columns - 8), style("accent") | curses.A_REVERSE, columns - 5)
            put(window, 6, 2, "Enter Choose · Esc Back · Ctrl+U Clear", style("muted"), columns - 5)
            window.move(4, 4 + cursor)
        else:
            rows = [("☑ " if key in self.columns else "☐ ") + title for key, (title, _, _) in COLUMNS.items()] if self.mode == "columns" else [COLUMNS[key][0] for key in SORTS]
            count = min(height - 2, len(rows) + 6)
            window, columns, put = self.modal_window(screen, count, "Visible columns" if self.mode == "columns" else "Sort threads", style)
            note = "Space Toggle · Enter Done" if self.mode == "columns" else "Enter Choose · Esc Cancel · Relevance stays first during search"
            put(window, 2, 2, note, style("muted"), columns - 5)
            capacity = max(1, count - 6)
            start = max(0, self.choice - capacity + 1)
            for index, label in enumerate(rows[start:start + capacity]):
                selected = start + index == self.choice
                put(window, 4 + index, 3, label, style("accent" if selected else "text") | (curses.A_REVERSE if selected else 0), columns - 7)
            put(window, count - 2, 3, "↑↓ Select · Enter / Esc Close", style("muted"), columns - 7)
        window.noutrefresh()

    def handle_key(self, key, page_size):
        if self.mode == "directory":
            result = self.editor.feed(key)
            if result == "submit":
                self.filter_draft["directory"] = self.editor.value.strip()
            if result in ("submit", "cancel"):
                self.mode, self.choice = "filters", self.filter_choice
            return None
        if self.mode == "options":
            rows = self.matching_options()
            if key == "\x1b":
                self.mode, self.choice = "filters", self.filter_choice
            elif key == curses.KEY_UP:
                self.choice = max(0, self.choice - 1)
            elif key == curses.KEY_DOWN:
                self.choice = min(max(0, len(rows) - 1), self.choice + 1)
            elif key in ("\n", "\r", curses.KEY_ENTER) and rows:
                _, value = rows[self.choice]
                if self.filter_choice == 0 and value is None:
                    self.mode, self.editor = "directory", self.renamer.LineEditor(self.filter_draft["directory"])
                else:
                    self.filter_draft[list(self.default_filters())[self.filter_choice]] = value
                    self.mode, self.choice = "filters", self.filter_choice
            else:
                old = self.option_editor.value
                self.option_editor.feed(key)
                if self.option_editor.value != old:
                    self.choice = 0
            return None
        if self.mode == "filters":
            if key == "\x1b":
                self.mode, self.filter_draft = "", None
            elif key == curses.KEY_F6:
                self.apply_filters()
            elif key == "\x0c":
                self.filter_draft = self.default_filters()
            elif key == curses.KEY_UP:
                self.choice = max(0, self.choice - 1)
            elif key in (curses.KEY_DOWN, "\t"):
                self.choice = (self.choice + 1) % 8
            elif self.choice >= 5 and key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                self.choice = 5 + (self.choice - 5 + (-1 if key == curses.KEY_LEFT else 1)) % 3
            elif key in ("\n", "\r", " ", curses.KEY_ENTER, curses.KEY_RIGHT):
                if self.choice < 5:
                    self.open_options()
                elif self.choice == 5:
                    self.apply_filters()
                elif self.choice == 6:
                    self.filter_draft = self.default_filters()
                else:
                    self.mode, self.filter_draft = "", None
            return None
        if self.mode:
            count = len(COLUMNS) if self.mode == "columns" else len(SORTS)
            if key in (curses.KEY_UP, "k"):
                self.choice = max(0, self.choice - 1)
            elif key in (curses.KEY_DOWN, "j"):
                self.choice = min(count - 1, self.choice + 1)
            elif key == "\x1b" or (self.mode == "columns" and key in ("\n", "\r", curses.KEY_ENTER)):
                self.mode = ""
                self.save_settings()
            elif self.mode == "columns" and key == " ":
                column = list(COLUMNS)[self.choice]
                if column in self.columns:
                    if len(self.columns) > 1:
                        self.columns.remove(column)
                else:
                    self.columns.append(column)
                self.column_offset = min(self.column_offset, len(self.columns) - 1)
            elif self.mode == "sort" and key in ("\n", "\r", curses.KEY_ENTER):
                self.sort, self.mode = SORTS[self.choice], ""
                self.reverse = self.sort in ("updated", "created", "tokens")
                self.sort_visible()
                self.save_settings()
            return None
        if key == curses.KEY_F4:
            self.full_text = not self.full_text
            self.refresh(reload=False)
        elif key == curses.KEY_F6:
            self.mode, self.choice, self.filter_draft = "filters", 0, self.current_filters()
        elif key == curses.KEY_F7:
            self.mode, self.choice = "columns", 0
        elif key == curses.KEY_F8:
            self.mode, self.choice = "sort", SORTS.index(self.sort)
        elif key == curses.KEY_F9:
            self.reverse = not self.reverse
            self.sort_visible(self.selected.id if self.selected else None)
            self.save_settings()
        elif key in (curses.KEY_SLEFT, curses.KEY_SRIGHT):
            self.column_offset = max(0, min(len(self.columns) - 1, self.column_offset + (1 if key == curses.KEY_SRIGHT else -1)))
        elif key == curses.KEY_F5:
            self.refresh()
        elif key == curses.KEY_UP:
            self.index = max(0, self.index - 1)
        elif key == curses.KEY_DOWN:
            self.index = min(max(0, len(self.visible) - 1), self.index + 1)
        elif key == curses.KEY_HOME:
            self.index = 0
        elif key == curses.KEY_END:
            self.index = max(0, len(self.visible) - 1)
        elif key == curses.KEY_NPAGE:
            self.index = min(max(0, len(self.visible) - 1), self.index + page_size)
        elif key == curses.KEY_PPAGE:
            self.index = max(0, self.index - page_size)
        elif key in ("\n", "\r", curses.KEY_ENTER) and self.selected:
            if self.pending:
                self.status = "Search is still running · wait for the results, then press Enter"
            else:
                return "resume"
        elif key == "\x12":
            return "rename"
        elif key == "\x1b":
            if not self.query:
                return "quit"
            self.query, self.search_editor = "", self.renamer.LineEditor()
            self.queue_search()
        else:
            if self.search_editor.value != self.query:
                self.search_editor = self.renamer.LineEditor(self.query)
            old = self.search_editor.value
            self.search_editor.feed(key)
            if self.search_editor.value != old:
                self.query = self.search_editor.value
                self.queue_search()
        return None

    def close(self):
        self.cancelled.set()
        for worker in self.workers:
            worker.join(timeout=3)
