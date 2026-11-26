"""Fictional, disposable AccountDeck data and simulated account actions."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import uuid

from hub_accounts import CommandError, clean

UTC = timezone.utc


def write_registry(home, data):
    path = Path(home) / "accounts" / "registry.json"
    path.write_text(json.dumps(data, indent=2))


def demo_account(index, now):
    labels = ("Work", "Personal", "Studio", "Lab", "Guest")
    label = labels[index] if index < len(labels) else f"Demo {index + 1}"
    return {
        "account_key": f"demo-user-{index}::demo-workspace",
        "email": f"{label.lower().replace(' ', '-')}@example.com", "alias": label,
        "plan": ("business", "plus", "pro", "business")[index % 4],
        "last_usage_at": now.timestamp(),
        "last_usage": {
            "primary": {"used_percent": (18, 55, 88, 4)[index % 4], "window_minutes": 300,
                        "resets_at": (now + timedelta(hours=3, minutes=24)).timestamp()},
            "secondary": {"used_percent": (30, 72, 95, 12)[index % 4], "window_minutes": 10080,
                          "resets_at": (now + timedelta(days=3, hours=5)).timestamp()},
            "reset_credits": (2, 1, 3, 0)[index % 4],
        },
    }


def create_demo(home):
    """Only the caller's fresh temporary directory is populated; no auth is read."""
    home = Path(home)
    (home / "accounts").mkdir(parents=True)
    now = datetime.now(UTC)
    accounts = [demo_account(index, now) for index in range(4)]
    write_registry(home, {"schema_version": 4, "active_account_key": accounts[0]["account_key"], "accounts": accounts})
    examples = [
        ("Build a thread resumer", "accountdeck", 0, "gpt-6.1-sol", 5,
         "Find every local conversation, including projects in subfolders.",
         "The resumer now supports login filters, saved columns and full-text search.", False),
        ("Track visitors at the front door", "luna/door", 1, "gpt-6.1-sol", 18,
         "The animated eye should follow visitors when they approach the camera.",
         "Face tracking starts on motion and continues for 60 seconds after the last detection.", False),
        ("Polish the product dashboard", "studio/dashboard", 2, "gpt-6-astra", 42,
         "Make the dashboard navigation easier to scan on a small display.",
         "The keyboard shortcuts and responsive layout are ready for review.", False),
        ("Tune thermal camera palettes", "heat/camera", 3, "gpt-6.1-sol", 65,
         "Can we calibrate the sensor without changing the temperature scale?",
         "Use the shutter for NUC calibration. The iron palette and fixed scale remain independent.", False),
        ("Improve checkout accessibility", "shop/frontend", 2, "gpt-6.1-sol", 95,
         "Audit keyboard focus and screen-reader labels in the checkout form.",
         "All controls have accessible names; the error summary links to each invalid field.", False),
        ("Add offline search indexing", "accountdeck/search", 0, "gpt-6.1-sol", 140,
         "Cache conversation text locally so repeated searches remain fast.",
         "The incremental index refreshes changed transcripts without calling a model.", False),
        ("Plan the garden watering schedule", "home/garden", 1, "gpt-6-sol", 200,
         "Compare soil-moisture thresholds with a morning watering timer.",
         "The proposed schedule skips irrigation after rainfall and logs each valve opening.", False),
        ("Analyze energy consumption", "home/energy", 3, "gpt-6.1-sol", 310,
         "Group the smart-meter readings by room and hour.",
         "The heatmap highlights the evening peak and the workshop's standby load.", False),
        ("Prototype the camera service", "luna/door/camera", 1, "gpt-6-sol", 1440,
         "Create a first prototype of the camera event service.",
         "The prototype is archived; the newer visitor-tracking thread continues this work.", True),
    ]
    with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, preview TEXT, cwd TEXT, "
                   "updated_at INTEGER, created_at INTEGER, source TEXT, archived INTEGER, model TEXT, "
                   "tokens_used INTEGER, creator_user_id TEXT, creator_account_id TEXT, rollout_path TEXT, history_mode TEXT)")
        index = []
        for position, (title, directory, owner, model, minutes, prompt, answer, archived) in enumerate(examples):
            identity = str(uuid.UUID(int=position + 101))
            relative = "projects/" + directory
            (home / relative).mkdir(parents=True, exist_ok=True)
            collection = "archived_sessions" if archived else "sessions"
            path = home / collection / "2026" / "11" / "15" / f"rollout-2026-11-15T12-00-00-{identity}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            records = [
                {"type": "session_meta", "payload": {"id": identity, "cwd": relative, "source": "vscode",
                    "account_scoped": True, "creator_user_id": f"demo-user-{owner}",
                    "creator_account_id": "demo-workspace", "history_mode": "paginated"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": answer}},
            ]
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
            updated = int((now - timedelta(minutes=minutes)).timestamp())
            created = int((now - timedelta(days=position + 1)).timestamp())
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identity, title, prompt, prompt, relative, updated, created, "vscode" if position % 2 else "cli",
                int(archived), model, 8200 + position * 4100, f"demo-user-{owner}", "demo-workspace", str(path), "paginated"))
            index.append({"id": identity, "thread_name": title})
    (home / "session_index.jsonl").write_text("\n".join(json.dumps(item) for item in index) + "\n")


class DemoAccountService:
    def __init__(self, home):
        self.home = Path(home)
        self.credits = {}
        self.usage = {}

    def saved_credits(self, account):
        key = account["account_key"]
        if key not in self.credits:
            index = int(key.split("::")[0].rsplit("-", 1)[1])
            now = datetime.now(UTC)
            first_expiry = (8, 2, .4, 12)[index % 4]
            self.credits[key] = [{"id": f"demo-credit-{index}-{i}", "title": "Full reset",
                                 "expires_at": (now + timedelta(days=first_expiry + i * 7)).isoformat()}
                                for i in range(account.get("reset_count") or 0)]
        return self.credits[key]

    def fetch(self, account, section):
        now = datetime.now(UTC)
        result = {"account_key": account["account_key"]}
        if section == "usage":
            result.update(usage=self.usage.get(account["account_key"], account["usage"]),
                          usage_live=True, usage_error=None, usage_at=now.timestamp())
        else:
            credits = self.saved_credits(account)
            result.update(credits=credits, reset_count=len(credits), resets_live=True, resets_error=None,
                          resets_at=now.timestamp())
        return result

    def redeem(self, account, credit_id):
        credits = self.saved_credits(account)
        remaining = [credit for credit in credits if credit["id"] != credit_id]
        if len(remaining) == len(credits):
            raise CommandError("This demo reset is no longer available")
        key = account["account_key"]
        usage = dict(self.usage.get(key, account["usage"]))
        for field in ("primary", "secondary"):
            usage[field] = dict(usage[field], used_percent=0)
        self.credits[key], self.usage[key] = remaining, usage


class DemoController:
    def __init__(self, home, emit=None):
        self.home = Path(home)
        self.emit = emit or (lambda message: None)

    def daemon_status(self):
        return "running"

    def restart(self):
        self.emit("Demo background service restarted")

    def switch(self, account):
        data = json.loads((self.home / "accounts/registry.json").read_text())
        if not any(a["account_key"] == account["account_key"] for a in data["accounts"]):
            raise CommandError("Unknown demo account")
        data["active_account_key"] = account["account_key"]
        write_registry(self.home, data)
        return clean(account.get("alias") or account.get("email")) + " is active · background service restarted (demo)"

    def login(self, device_auth=False):
        data = json.loads((self.home / "accounts/registry.json").read_text())
        account = demo_account(len(data["accounts"]), datetime.now(UTC))
        data["accounts"].append(account)
        data["active_account_key"] = account["account_key"]
        write_registry(self.home, data)
        return True

    def run(self, command, timeout=40):
        if len(command) >= 4 and command[1] == "unarchive":
            with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
                db.execute("UPDATE threads SET archived=0 WHERE id=?", (command[-1],))
            return subprocess.CompletedProcess(command, 0, "", "")
        raise CommandError("External commands are disabled in the demo")


def demo_client(renamer, binary, home):
    class LocalDemoClient(renamer.AppServer):
        def connect(self):
            raise renamer.CodexError("Demo thread data is unavailable; no Codex process was started")
    return LocalDemoClient(binary, home)
