#!/usr/bin/python3
"""Codex AccountDeck: accounts, quotas, reset credits and thread names in one TUI."""
from __future__ import annotations

import argparse
import curses
from datetime import datetime, timezone
import importlib
import math
import os
from pathlib import Path
from queue import Empty, Queue
import sys
import threading
import time

from codex_resets import timestamp
from hub_accounts import (AccountService, CommandError, Controller, clean, expires_in,
                          merge_local, registry_snapshot, remaining_percent, reset_count,
                          short_time, usable_credits)

ROOT = Path(__file__).resolve().parent
VERSION = "1.2.0"
UTC = timezone.utc


def load_renamer(directory):
    directory = Path(directory).expanduser().resolve()
    if not (directory / "codex_renamer.py").is_file():
        raise OSError(f"Renamer not found in {directory}. Use --renamer-dir PATH.")
    sys.path.insert(0, str(directory))
    return importlib.import_module("codex_renamer")


class HubApp:
    def __init__(self, args, renamer):
        self.args, self.renamer = args, renamer
        self.home = args.codex_home.expanduser().resolve()
        self.accounts = []
        self.index = self.offset = 0
        self.tab = "threads" if args.renamer else "accounts"
        self.messages = Queue()
        self.generation = 0
        self.loading = set()
        self.last_refresh = 0.0
        self.status = "Loading accounts…"
        self.error = False
        self.action = False
        self.action_thread = None
        self.daemon = "checking"
        self.daemon_epoch = 0
        self.status_until = 0.0
        self.closing = threading.Event()
        self.client = renamer.AppServer(args.codex_bin, self.home)
        self.threads = renamer.RenamerApp(self.client, args.limit)
        self.threads_loaded = False
        self.service = AccountService(self.home)
        self.controller = Controller(self.home, args.codex_bin, args.auth_bin,
                                     lambda text: self.messages.put(("progress", text)))
        self.styles = {}
        self.screen = None

    def style(self, name):
        return self.styles.get(name, 0)

    def put(self, y, x, text, style=0, width=None, screen=None):
        screen = self.screen if screen is None else screen
        self.renamer.RenamerApp.put(screen, y, x, clean(text), style, width)

    def line(self, y, x, text, width, style=0):
        # Preserve spaces in aligned tables and progress bars.
        height, columns = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < columns - 1:
            try:
                self.screen.addstr(y, x, self.renamer.clip(text, min(width, columns - x - 1)), style)
            except curses.error:
                pass

    def selected_key(self):
        return self.accounts[self.index]["account_key"] if self.accounts else None

    def reload_local(self):
        key = self.selected_key()
        self.accounts = merge_local(registry_snapshot(self.home), self.accounts)
        self.index = next((i for i, a in enumerate(self.accounts) if a["account_key"] == key),
                          min(self.index, max(0, len(self.accounts) - 1)))

    def refresh(self):
        if self.loading or self.action:
            return
        self.last_refresh = time.monotonic()
        try:
            self.reload_local()
        except (OSError, ValueError, TypeError, KeyError):
            self.status, self.error = "Cannot read Codex-Auth registry · check --codex-home", True
            return
        if not self.accounts:
            self.status, self.error = "No accounts found · press l to add your first account", False
            return
        if self.args.offline:
            self.status, self.error = "Offline view · saved limits only · reset expiry needs a live refresh", False
            return
        self.generation += 1
        generation = self.generation
        self.status, self.error = "Refreshing limits and reset expiry…", False
        for account in self.accounts:
            for section in ("usage", "resets"):
                self.loading.add((account["account_key"], section))
                def fetch(account=dict(account), section=section):
                    try:
                        update = self.service.fetch(account, section)
                    except Exception:
                        update = {"account_key": account["account_key"], section + "_error": "Refresh failed · press r to retry",
                                  section + "_live": False}
                    if not self.closing.is_set():
                        self.messages.put(("account", generation, section, update))
                threading.Thread(target=fetch, daemon=True, name="codex-hub-refresh").start()

    def check_daemon(self):
        epoch = self.daemon_epoch
        def work():
            try:
                state = self.controller.daemon_status()
            except CommandError:
                state = "not connected"
            if not self.closing.is_set():
                self.messages.put(("daemon", state, epoch))
        threading.Thread(target=work, daemon=True, name="codex-hub-status").start()

    def start_action(self, account=None, after_login=False):
        if self.action:
            return
        if self.threads.pending:
            self.status, self.error = "Wait for the current thread operation to finish.", True
            return
        if self.args.offline:
            self.status, self.error = "Offline mode · restart without --offline to switch accounts", True
            return
        # Close a protocol fallback connection before restarting its daemon.
        self.client.close()
        self.action, self.error = True, False
        self.daemon_epoch += 1
        self.status = "Switching account…" if account else "Restarting background service…"
        if after_login:
            self.status = "Login successful · restarting background service…"
        def work():
            try:
                if account:
                    message = self.controller.switch(account)
                else:
                    self.controller.restart()
                    message = "Login successful · background service restarted" if after_login else "Background service restarted"
                self.messages.put(("action", message, False))
            except CommandError as error:
                prefix = "Login successful. " if after_login else ""
                self.messages.put(("action", prefix + str(error), True))
            except Exception:
                self.messages.put(("action", "Operation failed · check Codex-Auth and the daemon", True))
        self.action_thread = threading.Thread(target=work, name="codex-hub-switch")
        self.action_thread.start()

    def login(self, device_auth=False):
        if self.action:
            return
        if self.threads.pending:
            self.status, self.error = "Wait for the current thread operation to finish.", True
            return
        if self.args.offline:
            self.status, self.error = "Offline mode · restart without --offline to log in", True
            return
        self.client.close()
        self.action = True
        self.daemon_epoch += 1
        # Discard responses fetched with the old login, including delayed 401s.
        self.generation += 1
        self.loading.clear()
        success, error = False, None
        curses.def_prog_mode()
        try:
            self.threads.set_cursor(1)
            curses.endwin()
            mode = "device code" if device_auth else "browser"
            print(f"\nCodex AccountDeck · {mode} login\n"
                  "Choose the account to add or renew in your browser.\n"
                  "Ctrl+C cancels and returns to the account overview.\n", flush=True)
            try:
                success = self.controller.login(device_auth)
            except CommandError as failure:
                error = str(failure)
        finally:
            curses.reset_prog_mode()
            self.screen.keypad(True)
            self.screen.timeout(80)
            curses.flushinp()
            self.screen.clearok(True)
            self.threads.set_cursor(0)
            self.action = False
        self.tab = "accounts"
        self.refresh()
        if success:
            self.index = next((i for i, a in enumerate(self.accounts) if a.get("active")), self.index)
            self.start_action(after_login=True)
        else:
            self.status, self.error = error or "Login cancelled · accounts reloaded", bool(error)
            self.status_until = time.monotonic() + 12
            self.check_daemon()

    def finish_messages(self):
        while True:
            try:
                message = self.messages.get_nowait()
            except Empty:
                break
            kind = message[0]
            if kind == "account":
                _, generation, section, update = message
                if generation != self.generation:
                    continue
                self.loading.discard((update["account_key"], section))
                for account in self.accounts:
                    if account["account_key"] == update["account_key"]:
                        account.update(update)
                        break
                if not self.loading and not self.action and time.monotonic() >= self.status_until:
                    errors = sum(bool(a.get("usage_error") or a.get("resets_error")) for a in self.accounts)
                    self.status = (f"{errors} account(s) need attention · cached values remain visible · r retries"
                                   if errors else "All accounts up to date · limits show remaining capacity")
                    self.error = bool(errors)
            elif kind == "progress":
                self.status = message[1]
            elif kind == "daemon":
                if not self.action and message[2] == self.daemon_epoch:
                    self.daemon = message[1]
            elif kind == "action":
                self.action = False
                self.status, self.error = message[1:]
                self.status_until = time.monotonic() + 12
                self.daemon = "not connected" if self.error else "running"
                # Even if restart failed, the account itself may already have changed.
                try:
                    self.reload_local()
                except (OSError, ValueError, TypeError, KeyError):
                    self.status += " · registry could not be refreshed"
                    self.error = True

    def init_colors(self):
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        palette = {"accent": (125, 211, 252), "text": (226, 232, 240), "muted": (148, 163, 184),
                   "border": (71, 85, 105), "green": (134, 239, 172), "amber": (253, 186, 116),
                   "red": (252, 129, 129), "purple": (196, 181, 253)}
        for name, rgb in palette.items():
            self.styles[name] = self.threads.colors.attribute(rgb)
        self.threads.accent, self.threads.failure = self.style("accent"), self.style("red")
        self.threads.background = self.renamer.RenamerApp.terminal_background()

    def draw_chrome(self):
        height, width = self.screen.getmaxyx()
        self.line(0, 2, "◈ CODEX ACCOUNTDECK", 21, self.style("accent") | curses.A_BOLD)
        self.line(0, 25, "YOUR ACCOUNTS. ONE TERMINAL.", max(0, width - 55), self.style("muted"))
        state = "● Server online" if self.daemon == "running" else "○ Server " + self.daemon
        self.line(0, max(18, width - len(state) - 3), state, len(state),
                  self.style("green" if self.daemon == "running" else "muted"))
        for tab, x, label in (("accounts", 2, " Accounts "), ("threads", 17, " Threads ")):
            attr = self.style("accent") | curses.A_BOLD
            if self.tab == tab:
                attr |= curses.A_REVERSE
            self.line(2, x, label, len(label), attr)
        self.line(2, 31, "← / → to change view", width - 34, self.style("muted"))
        self.line(height - 3, 2, "─" * (width - 4), width - 4, self.style("border"))
        if self.tab == "accounts":
            keys = ("↑↓ Select   Enter Switch+restart   → Threads   l Login   L Device login   r Refresh   d Restart   q Quit"
                    if width >= 110 else "↑↓ Select  Enter Switch  → Threads  l Login  r Reload  d Restart  q Quit")
        else:
            keys = "↑↓ Select   Enter Rename   / Search   ← Accounts   r Refresh   q Quit"
        self.line(height - 2, 2, keys, width - 4, self.style("muted"))
        if self.action or self.tab == "accounts":
            message, error = self.status, self.error
        else:
            message, error = self.thread_status(), self.threads.error
        if self.loading or self.action or (self.tab == "threads" and self.threads.pending):
            spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10]
            message = spinner + " " + message
        self.line(height - 1, 2, clean(message), width - 4, self.style("red" if error else "accent"))

    def window_values(self, account, field):
        window = account.get("usage", {}).get(field)
        percent = remaining_percent(window)
        expiry = timestamp(window.get("resets_at")) if isinstance(window, dict) else None
        seconds = (expiry - datetime.now(UTC)).total_seconds() if expiry else None
        return percent, seconds

    def bar(self, y, x, width, title, percent, seconds=None):
        label_width = 7
        time_width = 11
        bar_width = max(3, min(24, width - label_width - time_width - 6))
        filled = 0 if percent is None else round(bar_width * percent / 100)
        color = "muted" if percent is None else "red" if percent <= 10 else "amber" if percent <= 30 else "green"
        self.line(y, x, title, label_width, self.style("muted"))
        self.line(y, x + label_width, "━" * bar_width, bar_width, self.style("border"))
        if filled:
            self.line(y, x + label_width, "━" * filled, filled, self.style(color) | curses.A_BOLD)
        percent_text = " --%" if percent is None else f"{percent:3.0f}%"
        self.line(y, x + label_width + bar_width + 1, percent_text, 4, self.style(color))
        self.line(y, x + width - time_width, "↻ " + short_time(seconds), time_width, self.style("muted"))

    def reset_color(self, seconds):
        return "purple" if seconds is None else "red" if seconds <= 86400 else "amber" if seconds <= self.args.warning_days * 86400 else "green"

    def reset_column(self, y, x, width, rows, account):
        credits = usable_credits(account)
        count = reset_count(account)
        label = "Resets  " + ("…" if count is None else str(count))
        self.line(y, x, label, width, self.style("text") | curses.A_BOLD)
        if account.get("credits") is None:
            hint = "expiry unavailable" if account.get("resets_error") or self.args.offline else "loading expiry…"
            self.line(y + 1, x, hint, width, self.style("muted"))
            return
        if not credits:
            self.line(y + 1, x, "No saved resets available", width, self.style("muted"))
            return
        first = 0
        if rows == 2:
            # Compact cards still fit four accounts in an 80 × 24 terminal.
            seconds = expires_in(credits[0])
            self.line(y, x + len(label) + 3, "↺ next " + short_time(seconds),
                      width - len(label) - 3, self.style(self.reset_color(seconds)))
            if len(credits) == 1:
                expiry = timestamp(credits[0].get("expires_at"))
                detail = expiry.astimezone().strftime("expires %d %b %H:%M") if expiry else "Expiry unknown"
                if credits[0].get("is_supported_by_plan") is False:
                    detail = "Plan unsupported · " + detail
                self.line(y + 1, x, detail, width, self.style("muted"))
                return
            first = 1
        for row in range(1, rows):
            index = first + row - 1
            if index >= len(credits):
                break
            credit = credits[index]
            seconds = expires_in(credit)
            expiry = timestamp(credit.get("expires_at"))
            text = "↺ " + ("next " if index == 0 else "") + short_time(seconds)
            if row == rows - 1 and index + 1 < len(credits):
                # Pack later expiries into the last line rather than dropping them.
                cursor = x
                for later in range(index, len(credits)):
                    seconds = expires_in(credits[later])
                    text = "↺ " + short_time(seconds)
                    available = x + width - cursor
                    more = f" +{len(credits) - later - 1}" if later + 1 < len(credits) else ""
                    if len(text) + len(more) > available:
                        self.line(y + row, cursor, f"+{len(credits) - later} more", available, self.style("muted"))
                        break
                    self.line(y + row, cursor, text, len(text), self.style(self.reset_color(seconds)))
                    cursor += len(text) + 3
                break
            if expiry:
                text += expiry.astimezone().strftime(" (%d %b %H:%M)")
            if credit.get("is_supported_by_plan") is False:
                text = "[plan unsupported] " + text
            self.line(y + row, x, text, width, self.style(self.reset_color(seconds)))
        if not account.get("resets_live") and rows > 2:
            self.line(y, x + len(label) + 3, "cached", width - len(label) - 3, self.style("amber"))

    def draw_accounts(self):
        height, width = self.screen.getmaxyx()
        counts = [reset_count(a) for a in self.accounts]
        known = sum(c for c in counts if c is not None)
        expiries = [expires_in(c) for a in self.accounts for c in usable_credits(a) if expires_in(c) is not None]
        hero = f"{len(self.accounts)} ACCOUNTS   /   {known}{'+' if None in counts else ''} SAVED RESETS"
        if expiries:
            hero += "   /   NEXT EXPIRY IN " + short_time(min(expiries)).upper()
        self.line(4, 2, hero, width - 4, self.style("text") | curses.A_BOLD)
        if not self.accounts:
            self.line(7, 4, self.status, width - 8, self.style("muted"))
            return
        available = height - 8
        card_height = max(4, min(6, available // len(self.accounts)))
        page_size = max(1, available // card_height)
        self.offset = min(self.offset, self.index)
        if self.index >= self.offset + page_size:
            self.offset = self.index - page_size + 1
        for position, account in enumerate(self.accounts[self.offset:self.offset + page_size]):
            y = 5 + position * card_height
            selected = self.offset + position == self.index
            border = self.style("accent" if selected else "border")
            inner_x, inner_width = 4, width - 8
            name = clean(account.get("alias") or account.get("email") or "Unknown account")
            live = account.get("usage_live") and account.get("resets_live")
            state = "LIVE" if live else "CACHED"
            errors = account.get("usage_error") or account.get("resets_error")
            if errors:
                state = "NEEDS ATTENTION"
            if account.get("active"):
                state = "● ACTIVE · " + state
            self.line(y, 2, "╭" + "─" * (width - 6) + "╮", width - 4, border)
            self.line(y, 3, "› " if selected else "  ", 2, border | curses.A_BOLD)
            state_width = len(state) + 4
            heading_width = width - state_width - 12
            self.line(y, 6, " " + self.renamer.clip(name, heading_width) + " ", heading_width + 2,
                      self.style("text") | (curses.A_BOLD if selected else 0))
            self.line(y, width - state_width - 3, " " + state + " ", state_width,
                      self.style("green" if live else "amber" if errors else "muted"))
            for row in range(y + 1, y + card_height - 1):
                self.line(row, 2, "│", 1, border)
                self.line(row, width - 3, "│", 1, border)
            self.line(y + card_height - 1, 2, "╰" + "─" * (width - 6) + "╯", width - 4, border)
            column_width = (inner_width - 3) // 2
            for row in range(y + 1, y + card_height - 1):
                self.line(row, inner_x + column_width + 1, "│", 1, self.style("border"))
            for row, field, label in ((y + 1, "primary", "5h"), (y + 2, "secondary", "Weekly")):
                percent, seconds = self.window_values(account, field)
                self.bar(row, inner_x, column_width, label, percent, seconds)
            resets_x = inner_x + column_width + 3
            self.reset_column(y + 1, resets_x, inner_width - column_width - 3,
                              card_height - 2, account)
            if card_height >= 5:
                if errors:
                    extra, color = errors, "amber"
                else:
                    plan = clean(account.get("plan") or "unknown plan").title()
                    updated = timestamp(account.get("usage_at"))
                    checked = updated.astimezone().strftime("%H:%M") if updated else "unknown"
                    extra, color = f"{plan}  ·  limits {'checked' if live else 'saved'} {checked}", "muted"
                self.line(y + 3, inner_x, extra, column_width, self.style(color))
        if len(self.accounts) > page_size:
            self.line(4, max(2, width - 23), f"{self.offset + 1}–{min(len(self.accounts), self.offset + page_size)} of {len(self.accounts)} · PgUp/Dn", 21, self.style("muted"))

    def thread_status(self):
        app = self.threads
        if app.pending:
            return "Saving thread name…" if app.pending_kind == "rename" else "Loading threads…"
        if app.error:
            return app.status
        if app.status.startswith("Umbenannt:"):
            return "Renamed: " + app.pending_name
        return f"{len(app.visible)} threads · latest activity first" + (f" · filter: {app.query}" if app.query else "")

    def draw_threads(self):
        height, width = self.screen.getmaxyx()
        app = self.threads
        self.line(4, 2, f"THREADS  /  {len(app.visible)}" + ("  /  " + clean(app.query) if app.query else ""), width - 4,
                  self.style("text") | curses.A_BOLD)
        available = width - 6
        project_width = min(28, max(12, available // 4))
        title_width = available - project_width - 20
        header = self.renamer.fit("LAST ACTIVE", 16) + "  " + self.renamer.fit("NAME", title_width) + "  PROJECT"
        self.line(6, 3, header, available, self.style("muted") | curses.A_BOLD)
        page_size = max(1, height - 15)
        app.offset = min(app.offset, app.index)
        if app.index >= app.offset + page_size:
            app.offset = app.index - page_size + 1
        for row, thread in enumerate(app.visible[app.offset:app.offset + page_size]):
            selected = app.offset + row == app.index
            text = self.renamer.fit(thread.last_active, 16) + "  " + self.renamer.fit(thread.title, title_width) + "  " + self.renamer.fit(thread.project, project_width)
            style = app.thread_style(thread) | (curses.A_BOLD | curses.A_UNDERLINE if selected else 0)
            if selected:
                self.line(7 + row, 1, "›", 1, style)
            self.line(7 + row, 3, text, available, style)
        if not app.visible:
            self.line(8, 4, "Loading threads…" if app.pending else "No matching threads.", width - 8, self.style("muted"))
        self.line(height - 7, 2, "─" * (width - 4), width - 4, self.style("border"))
        if app.visible:
            thread = app.visible[app.index]
            self.line(height - 6, 3, thread.title, width - 6, app.thread_style(thread) | curses.A_BOLD)
            self.line(height - 5, 3, clean(thread.cwd), width - 6, self.style("muted"))
            self.line(height - 4, 3, thread.id, width - 6, self.style("muted"))

    def draw_editor(self):
        app = self.threads
        height, width = self.screen.getmaxyx()
        columns = min(width - 6, 94)
        window = curses.newwin(9, columns, (height - 9) // 2, (width - columns) // 2)
        window.box()
        rename = app.mode == "rename"
        put = self.renamer.RenamerApp.put
        put(window, 1, 2, "Rename thread" if rename else "Search threads", self.style("accent") | curses.A_BOLD)
        put(window, 2, 2, "Current: " + app.target.title if rename else "Search name, project path or thread ID", self.style("muted"))
        text, cursor = app.editor.viewport(columns - 6)
        put(window, 4, 2, "> " + self.renamer.fit(text, columns - 6), curses.A_REVERSE)
        put(window, 6, 2, "Enter Save · Empty / Esc Cancel" if rename else "Enter Search · Esc Cancel · Empty Show all", self.style("muted"))
        if app.error:
            put(window, 7, 2, app.status, self.style("red"))
        window.move(4, 4 + cursor)
        window.noutrefresh()

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if width < 76 or height < 18:
            self.threads.set_cursor(0)
            self.put(0, 0, "Resize terminal to at least 76 × 18 (80 × 25 recommended for four accounts).")
            self.put(2, 0, "q / Esc: quit")
            self.screen.refresh()
            return
        self.threads.set_cursor(int(bool(self.tab == "threads" and self.threads.mode)))
        self.draw_chrome()
        if self.tab == "accounts":
            self.draw_accounts()
        else:
            self.draw_threads()
        self.screen.noutrefresh()
        if self.tab == "threads" and self.threads.mode:
            self.draw_editor()
        curses.doupdate()

    def set_tab(self, tab):
        self.tab = tab
        if tab == "threads" and not self.threads_loaded:
            self.threads.refresh()
            self.threads_loaded = True

    def handle_key(self, key):
        if self.tab == "threads" and self.threads.mode:
            return self.threads.handle_key(key, 1)
        if key in ("q", "Q", "\x1b"):
            if self.action:
                self.status = "Finishing account/service operation · please wait before quitting"
                return True
            return False
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT, "\t"):
            tab = ("threads" if self.tab == "accounts" else "accounts") if key == "\t" else ("accounts" if key == curses.KEY_LEFT else "threads")
            self.set_tab(tab)
            return True
        height, _ = self.screen.getmaxyx()
        if self.tab == "threads":
            if not self.action:
                return self.threads.handle_key(key, max(1, height - 15))
            return True
        if key in (curses.KEY_UP, "k"):
            self.index = max(0, self.index - 1)
        elif key in (curses.KEY_DOWN, "j"):
            self.index = min(max(0, len(self.accounts) - 1), self.index + 1)
        elif key == curses.KEY_HOME:
            self.index = 0
        elif key == curses.KEY_END:
            self.index = max(0, len(self.accounts) - 1)
        elif key == curses.KEY_NPAGE:
            self.index = min(max(0, len(self.accounts) - 1), self.index + max(1, (height - 9) // 4))
        elif key == curses.KEY_PPAGE:
            self.index = max(0, self.index - max(1, (height - 9) // 4))
        elif key in ("\n", "\r", curses.KEY_ENTER) and self.accounts:
            self.start_action(dict(self.accounts[self.index]))
        elif key in ("l", "L"):
            self.login(device_auth=key == "L")
        elif key in ("d", "D"):
            self.start_action()
        elif key in ("r", "R"):
            self.refresh()
        return True

    def run(self, screen):
        self.screen = screen
        screen.keypad(True)
        screen.timeout(80)
        curses.set_escdelay(25)
        self.init_colors()
        self.refresh()
        self.check_daemon()
        if self.tab == "threads":
            self.set_tab("threads")
        try:
            while True:
                self.finish_messages()
                self.threads.finish_pending()
                if time.monotonic() - self.last_refresh >= self.args.interval:
                    self.refresh()
                self.draw()
                try:
                    key = screen.get_wch()
                except curses.error:
                    continue
                if key == curses.KEY_RESIZE:
                    continue
                height, width = screen.getmaxyx()
                if (width < 76 or height < 18) and key not in ("q", "Q", "\x1b"):
                    continue
                if not self.handle_key(key):
                    break
        finally:
            self.threads.colors.restore()

    def close(self):
        self.closing.set()
        # Mutations run to completion; network refresh workers can be abandoned.
        if self.action_thread and self.action_thread.is_alive():
            self.action_thread.join()
        self.threads.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="codex-accountdeck", description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--auth-bin", default="codex-auth")
    parser.add_argument("--renamer-dir", type=Path, default=Path(os.environ.get("CODEX_RENAMER_DIR") or ROOT),
                        help="Use a different renamer implementation (default: bundled)")
    parser.add_argument("--renamer", action="store_true", help="Open directly in the Threads tab")
    parser.add_argument("--offline", action="store_true", help="Show saved limits; disable account refresh/switching and daemon restarts")
    parser.add_argument("--interval", type=int, default=180, help="Refresh interval in seconds (minimum 30)")
    parser.add_argument("--warning-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=200, help="Number of recent threads to load")
    parser.add_argument("--version", action="version", version=f"codex-accountdeck {VERSION}")
    args = parser.parse_args(argv)
    if args.interval < 30 or args.warning_days < 1 or args.limit < 1:
        parser.error("interval must be >=30, warning-days and limit must be positive")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("Open Codex AccountDeck in an interactive terminal")
    app = None
    try:
        renamer = load_renamer(args.renamer_dir)
        renamer.configure_locale()
        app = HubApp(args, renamer)
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        return 130
    except (OSError, curses.error) as error:
        print("Codex AccountDeck: " + clean(error), file=sys.stderr)
        return 1
    finally:
        if app:
            app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
