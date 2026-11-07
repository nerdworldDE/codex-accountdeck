"""Account snapshots and the Codex-Auth/daemon boundary for Codex AccountDeck."""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import urllib.error
import urllib.request

from codex_resets import NoRedirect, available_credits, timestamp

UTC = timezone.utc
BASE_URL = "https://chatgpt.com/backend-api/wham/"


def clean(value):
    return " ".join("".join(c if c.isprintable() else " " for c in str(value or "")).split())


def short_time(seconds):
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return "now"
    minutes = max(1, math.ceil(seconds / 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02}m"
    return f"{minutes}m"


def expires_in(credit, now=None):
    expiry = timestamp(credit.get("expires_at"))
    return (expiry - (now or datetime.now(UTC))).total_seconds() if expiry else None


def usable_credits(account, now=None):
    now = now or datetime.now(UTC)
    return sorted(
        [c for c in account.get("credits") or [] if expires_in(c, now) is None or expires_in(c, now) > 0],
        key=lambda c: (expires_in(c, now) is None, expires_in(c, now) or 0),
    )


def reset_count(account, now=None):
    if account.get("credits") is not None:
        return len(usable_credits(account, now))
    return account.get("reset_count")


def remaining_percent(window, now=None):
    if not isinstance(window, dict):
        return None
    value = window.get("used_percent")
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    expiry = timestamp(window.get("resets_at"))
    if expiry and expiry <= (now or datetime.now(UTC)):
        return 100.0
    return max(0.0, min(100.0, 100 - value))


def registry_snapshot(home: Path):
    try:
        data = json.loads((home / "accounts/registry.json").read_text())
    except FileNotFoundError:
        return []  # First run: the user can add an account with l / L.
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise ValueError("Unsupported Codex-Auth registry. Expected an accounts list.")
    result = []
    for record in data["accounts"]:
        if not isinstance(record, dict) or not isinstance(record.get("account_key"), str):
            raise ValueError("Invalid account in Codex-Auth registry.")
        usage = record.get("last_usage") or {}
        result.append({
            "account_key": record["account_key"], "email": record.get("email"),
            "alias": record.get("alias"), "plan": record.get("plan"),
            "active": record["account_key"] == data.get("active_account_key"),
            "usage": deepcopy(usage), "usage_at": record.get("last_usage_at"),
            "usage_live": False, "usage_error": None,
            "credits": None, "reset_count": usage.get("reset_credits"),
            "resets_at": None, "resets_live": False, "resets_error": None,
        })
    return sorted(result, key=lambda a: clean(a.get("alias") or a.get("email")).casefold())


def merge_local(local, previous):
    """Keep the last successful network data while updating identities/activation."""
    by_key = {a["account_key"]: a for a in previous}
    for account in local:
        old = by_key.get(account["account_key"])
        if old:
            for key in ("usage", "usage_at", "usage_live", "usage_error", "credits", "reset_count",
                        "resets_at", "resets_live", "resets_error"):
                account[key] = old[key]
    return local


class AccountService:
    def __init__(self, home, timeout=12):
        self.home = Path(home)
        self.timeout = timeout

    def _tokens(self, account):
        key = base64.urlsafe_b64encode(account["account_key"].encode()).decode().rstrip("=")
        stored = self.home / "accounts" / f"{key}.auth.json"
        tokens = json.loads(stored.read_text())["tokens"]
        # The active client may have rotated its access token since the snapshot.
        # Only use it when BOTH user and workspace match the stored identity.
        try:
            live = json.loads((self.home / "auth.json").read_text())["tokens"]
            def identity(value):
                payload = value["access_token"].split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                auth = claims.get("https://api.openai.com/auth", {})
                return auth.get("chatgpt_user_id"), value.get("account_id")
            user, workspace = identity(tokens)
            if user and workspace and identity(live) == (user, workspace):
                tokens = live
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            pass
        if not tokens.get("access_token") or not tokens.get("account_id"):
            raise ValueError("Missing ChatGPT authentication")
        return tokens

    def _get(self, endpoint, tokens):
        request = urllib.request.Request(BASE_URL + endpoint, headers={
            "Authorization": "Bearer " + tokens["access_token"],
            "ChatGPT-Account-Id": tokens["account_id"],
            "Accept": "application/json", "User-Agent": "codex-accountdeck/1.2",
        })
        with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
            return json.load(response)

    @staticmethod
    def _error(error):
        if isinstance(error, urllib.error.HTTPError):
            if error.code == 401:
                return "Sign-in expired · press l to log in"
            return f"HTTP {error.code} · press r to retry"
        return "Cannot refresh · check network / login"

    def fetch(self, account, section):
        """Each section can fail independently; never replace a good value with zero."""
        result = {"account_key": account["account_key"]}
        try:
            tokens = self._tokens(account)
            if section == "usage":
                data = self._get("usage", tokens)
                limits = data.get("rate_limit")
                if not isinstance(limits, dict):
                    raise ValueError("Missing rate limit information")
                usage = {"primary": limits.get("primary_window"),
                         "secondary": limits.get("secondary_window")}
                for field in ("primary", "secondary"):
                    window = usage[field]
                    if window is not None:
                        if not isinstance(window, dict) or not isinstance(window.get("used_percent"), (int, float)):
                            raise ValueError("Invalid rate limit window")
                        window = dict(window)
                        window["resets_at"] = window.get("reset_at")
                        window["window_minutes"] = (window.get("limit_window_seconds") or 0) / 60
                        usage[field] = window
                if not any(usage.values()):
                    raise ValueError("Empty rate limit information")
                result.update(usage=usage, usage_at=datetime.now(UTC).timestamp(),
                              usage_live=True, usage_error=None)
            else:
                data = self._get("rate-limit-reset-credits", tokens)
                result.update(credits=available_credits(data), reset_count=data.get("available_count"),
                              resets_at=datetime.now(UTC).timestamp(), resets_live=True, resets_error=None)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            result.update({section + "_error": self._error(error), section + "_live": False})
        return result


class CommandError(Exception):
    pass


class Controller:
    def __init__(self, home, codex="codex", auth="codex-auth", emit=None):
        self.home = Path(home).expanduser().resolve()
        self.codex = codex
        self.auth = auth
        self.emit = emit or (lambda message: None)

    def run(self, command, timeout=40):
        try:
            return subprocess.run(command, env={**os.environ, "CODEX_HOME": str(self.home)},
                                  capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as error:
            raise CommandError(f"Command not found: {command[0]}") from error
        except subprocess.TimeoutExpired as error:
            raise CommandError("Command timed out. Check the daemon before retrying.") from error
        except OSError as error:
            raise CommandError("Command could not run: " + clean(error)) from error

    def daemon_status(self):
        result = self.run([self.codex, "app-server", "daemon", "version"], timeout=6)
        return "running" if result.returncode == 0 else "not connected"

    def login(self, device_auth=False):
        """Let Codex-Auth own OAuth and the terminal; never capture its login codes."""
        command = [self.auth, "login"]
        if device_auth:
            command.append("--device-auth")
        try:
            with subprocess.Popen(command, env={**os.environ, "CODEX_HOME": str(self.home)}) as process:
                try:
                    code = process.wait()
                except KeyboardInterrupt:
                    # The foreground child receives the same terminal SIGINT.
                    # Reap it before returning the terminal to curses.
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    return False
        except FileNotFoundError as error:
            raise CommandError(f"Command not found: {self.auth}") from error
        except OSError as error:
            raise CommandError("Login could not start: " + clean(error)) from error
        if code in (130, -signal.SIGINT):
            return False
        if code:
            raise CommandError("Login did not complete · press l to try again")
        return True

    def restart(self):
        control = self.home / "app-server-control/app-server-control.sock"
        action = "restart" if control.exists() else "start"
        self.emit(("Restarting" if action == "restart" else "Starting") + " Codex background service…")
        result = self.run([self.codex, "app-server", "daemon", action], timeout=60)
        if result.returncode:
            details = clean(result.stderr or result.stdout)[-240:]
            raise CommandError("Service restart failed" + (": " + details if details else "."))

    def switch(self, account):
        label = clean(account.get("alias") or account.get("email") or "selected account")
        self.emit(f"Switching to {label}…")
        result = self.run([self.auth, "switch", account["account_key"], "--json"])
        try:
            data = json.loads(result.stdout)
            switched = data.get("switched_to") or {}
            valid = switched.get("account_key") == account["account_key"]
        except (ValueError, AttributeError):
            valid = False
        if result.returncode or not valid:
            raise CommandError("Account switch failed. The service was not restarted; check Codex-Auth.")
        try:
            self.restart()
        except CommandError as error:
            raise CommandError(f"Account switched to {label}. {error}") from error
        return f"{label} is active · background service restarted"
