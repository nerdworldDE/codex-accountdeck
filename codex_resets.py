#!/usr/bin/python3
"""Read-only desktop and terminal overview of Codex-Auth banked reset credits."""
import argparse
import base64
import concurrent.futures
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

UTC = timezone.utc
ENDPOINT = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "codex-resets"


def timestamp(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result
    except (ValueError, TypeError, OverflowError):
        return None


def remaining(credit, now):
    expiry = timestamp(credit.get("expires_at"))
    return (expiry - now).total_seconds() if expiry else None


def lifetime(seconds):
    if seconds is None:
        return "Ablaufdatum unbekannt"
    if seconds <= 0:
        return "Abgelaufen"
    hours = max(1, math.ceil(seconds / 3600))
    if seconds < 86400:
        return f"Noch {hours} Std."
    days = int(seconds // 86400)
    hours = int(seconds % 86400 // 3600)
    return f"Noch {days} {'Tag' if days == 1 else 'Tage'} {hours} Std."


def severity(seconds, warning_days=7):
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return "expired"
    if seconds <= 86400:
        return "critical"
    if seconds <= warning_days * 86400:
        return "warning"
    return "ok"


def available_credits(data):
    if not isinstance(data, dict) or not isinstance(data.get("credits"), list):
        raise ValueError("Unerwartetes Antwortformat der Reset-Abfrage")
    result = []
    for credit in data["credits"]:
        if not isinstance(credit, dict):
            raise ValueError("Unerwartetes Reset-Format")
        if str(credit.get("status", "")).lower() == "available":
            result.append({k: credit.get(k) for k in
                           ("id", "title", "expires_at", "granted_at", "is_supported_by_plan")})
    return result


def valid_credits(account, now):
    return [c for c in account.get("credits", [])
            if remaining(c, now) is None or remaining(c, now) > 0]


def ordered_credits(account, now, urgent=False):
    def key(credit):
        seconds = remaining(credit, now)
        return (seconds is None, seconds if urgent else -seconds if seconds is not None else 0)
    return sorted(account.get("credits", []), key=key)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_account(account, home):
    result = {k: account.get(k) for k in ("account_key", "email", "alias", "plan")}
    result["credits"] = []
    try:
        key = account["account_key"]
        encoded = base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")
        auth = json.loads((home / "accounts" / f"{encoded}.auth.json").read_text())
        tokens = auth["tokens"]
        request = urllib.request.Request(ENDPOINT, headers={
            "Authorization": "Bearer " + tokens["access_token"],
            "ChatGPT-Account-Id": tokens["account_id"],
            "Accept": "application/json", "User-Agent": "codex-resets/1.0",
        })
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            data = json.load(response)
        result["credits"] = available_credits(data)
        result["reported_count"] = data.get("available_count")
        result["checked_at"] = datetime.now(UTC).isoformat()
        result["error"] = None
    except urllib.error.HTTPError as error:
        result["http_status"] = error.code
        result["error"] = ("Anmeldung abgelaufen. In Codex-Auth aktualisieren/anmelden."
                           if error.code == 401 else f"Abfrage fehlgeschlagen (HTTP {error.code}).")
    except (OSError, ValueError, KeyError, TypeError):
        result["error"] = "Abfrage fehlgeschlagen. Netzwerk und Codex-Auth-Anmeldung prüfen."
    return result


def load_snapshot(home):
    registry = json.loads((home / "accounts/registry.json").read_text())
    if not isinstance(registry.get("accounts"), list):
        raise ValueError("Unbekanntes Codex-Auth-Registry-Format")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        accounts = list(pool.map(lambda a: fetch_account(a, home), registry["accounts"]))
    for account in accounts:
        account["active"] = account["account_key"] == registry.get("active_account_key")
    return {"checked_at": datetime.now(UTC).isoformat(), "accounts": accounts}


def terminal(snapshot, warning_days=7, color="auto", urgent=False, stream=None, numbered=False):
    stream = sys.stdout if stream is None else stream
    colors = color == "always" or (color == "auto" and stream.isatty()
                                     and "NO_COLOR" not in os.environ
                                     and os.environ.get("TERM") != "dumb")
    palette = {"critical": "31", "warning": "33", "ok": "32", "expired": "90",
               "unknown": "35", "heading": "1", "error": "31"}

    def paint(text, tag):
        return f"\033[{palette[tag]}m{text}\033[0m" if colors else text

    def safe(value):
        return "".join(c for c in str(value) if c.isprintable())

    def duration(seconds):
        if seconds is None:
            return "Expiry unknown"
        if seconds <= 0:
            return "Expired"
        days, rest = divmod(int(seconds), 86400)
        hours, rest = divmod(rest, 3600)
        minutes = int(rest // 60)
        return f"{days}d {hours:02}h {minutes:02}m left" if days else f"{hours}h {minutes:02}m left" if hours else f"{minutes}m left" if minutes else "<1m left"

    now = datetime.now(UTC)
    accounts = list(snapshot["accounts"])
    def account_order(account):
        values = [remaining(c, now) for c in valid_credits(account, now)
                  if remaining(c, now) is not None]
        return (not bool(values), min(values) if urgent and values else -max(values) if values else 0)

    accounts.sort(key=account_order)
    total = sum(len(valid_credits(a, now)) for a in accounts if not a.get("error"))
    failed = sum(bool(a.get("error")) for a in accounts)
    print(paint("CODEX RESETS", "heading"), file=stream)
    print(f"{len(accounts)} accounts | {total} available resets" +
          (f" | {failed} account(s) unavailable" if failed else ""), file=stream)
    print("Order: " + ("soonest expiry first" if urgent else "remaining time descending"), file=stream)
    print(paint("Red: <=1 day", "critical") + " | " +
          paint(f"Yellow: <= {warning_days} days", "warning") + " | " +
          paint(f"Green: > {warning_days} days", "ok"), file=stream)
    for number, account in enumerate(accounts, 1):
        email = account.get("email") or "Unknown account"
        label = f"{account['alias']} ({email})" if account.get("alias") else email
        if account.get("active"):
            label += " [active]"
        if numbered:
            label = f"[{number}] {label}"
        print("\n" + paint(safe(label), "heading"), file=stream)
        if account.get("error"):
            status = account.get("http_status")
            message = ("Authentication expired. Refresh or sign in using Codex-Auth."
                       if status == 401 else f"Request failed (HTTP {status})."
                       if status else "Request failed. Check your network and Codex-Auth sign-in.")
            print("  " + paint("ERROR: " + message, "error"), file=stream)
            continue
        credits = ordered_credits(account, now, urgent)
        print(f"  {len(valid_credits(account, now))} available reset(s)", file=stream)
        for index, credit in enumerate(credits, 1):
            seconds = remaining(credit, now)
            expiry = timestamp(credit.get("expires_at"))
            tag = severity(seconds, warning_days)
            when = expiry.astimezone().strftime("%Y-%m-%d %H:%M %Z") if expiry else "unknown"
            title = safe(credit.get("title") or "Reset")
            state = {"critical": "EXPIRING TODAY", "warning": "EXPIRING SOON", "ok": "Available",
                     "unknown": "Unknown expiry", "expired": "Expired"}[tag]
            if credit.get("is_supported_by_plan") is False:
                state += "; unsupported by plan"
            print("  " + paint(f"{index}. {duration(seconds):<20} | {when} | {state}", tag), file=stream)
            print(f"     {title}", file=stream)
        if not credits:
            print("  No saved resets.", file=stream)
        count = account.get("reported_count")
        if count is not None and count != len(credits):
            print("  " + paint(f"WARNING: Server reports {count} reset(s); details differ.", "warning"), file=stream)
    expiring = [remaining(c, now) for a in accounts if not a.get("error")
                for c in valid_credits(a, now) if remaining(c, now) is not None]
    if expiring:
        first = min(expiring)
        print("\n" + paint("Next expiry: " + duration(first), severity(first, warning_days)), file=stream)
    checked = timestamp(snapshot.get("checked_at"))
    if checked:
        print("Checked: " + checked.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"), file=stream)
    return accounts


def choose_account(accounts, home):
    """Switch by the stable key, using Codex-Auth's non-interactive JSON command."""
    if not accounts:
        print("No accounts available to switch.")
        return 1
    while True:
        try:
            choice = input("\nSwitch to account [1-" + str(len(accounts)) + "] (Enter/q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 0
        if not choice or choice.lower() == "q":
            print("Cancelled.")
            return 0
        try:
            number = int(choice)
        except ValueError:
            number = 0
        if 1 <= number <= len(accounts):
            account = accounts[number - 1]
            break
        print(f"Enter an account number between 1 and {len(accounts)}, or q to cancel.")
    try:
        result = subprocess.run(["codex-auth", "switch", account["account_key"], "--json"],
                                env={**os.environ, "CODEX_HOME": str(home.expanduser().resolve())},
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        print("Could not run codex-auth. Check that it is installed and available in PATH.", file=sys.stderr)
        return 1
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = {}
    if (result.returncode != 0 or not isinstance(data, dict)
            or not isinstance(data.get("switched_to"), dict)
            or data["switched_to"].get("account_key") != account["account_key"]):
        print("Account switch failed. Check the account with codex-auth.", file=sys.stderr)
        return 1
    label = account.get("email") or account.get("alias") or "selected account"
    print("Switched to " + "".join(c for c in label if c.isprintable()) + ".")
    return 0


def send_notification(title, body):
    if os.environ.get("WSL_DISTRO_NAME") and shutil.which("powershell.exe"):
        # Base64 literals keep account labels out of PowerShell syntax.
        def literal(value):
            encoded = base64.b64encode(value.encode()).decode()
            return f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))"
        script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Warning
$icon.Visible = $true
$icon.BalloonTipTitle = {literal(title)}
$icon.BalloonTipText = {literal(body)}
$icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
$icon.ShowBalloonTip(10000)
Start-Sleep -Seconds 8
$icon.Dispose()
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                   "-EncodedCommand", encoded]
    else:
        command = ["notify-send", "--app-name=Codex Resets", "--urgency=critical", title, body]
    try:
        return subprocess.run(command, capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def notify(snapshot, warning_days):
    """One reminder per account/day; no credentials or backend data are persisted."""
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "notifications.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = STATE / "notifications.json"
        try:
            sent = json.loads(path.read_text())
        except (OSError, ValueError):
            sent = {}
        now = datetime.now(UTC)
        today = now.astimezone().date().isoformat()
        for account in snapshot["accounts"]:
            if account.get("error"):
                continue
            soon = [c for c in valid_credits(account, now)
                    if remaining(c, now) is not None and remaining(c, now) <= warning_days * 86400]
            key = account["account_key"]
            if not soon or sent.get(key) == today:
                continue
            first = min(soon, key=lambda c: remaining(c, now))
            body = (f"{account.get('alias') or account['email']}: {len(soon)} Reset(s) laufen bald ab.\n"
                    f"Frühester: {lifetime(remaining(first, now))}.\nCodex Resets öffnen, um alle Ablaufdaten zu sehen.")
            if send_notification("Codex-Reset läuft bald ab", body):
                sent[key] = today
        fd, temp = tempfile.mkstemp(dir=STATE)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(sent, stream)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


def gui(args):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Codex Resets")
    root.geometry("1120x690")
    root.minsize(850, 480)
    bg, panel, fg, muted = "#111827", "#1f2937", "#f3f4f6", "#a5b4c8"
    root.configure(bg=bg)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg,
                    rowheight=36, font=("DejaVu Sans", 10), borderwidth=0)
    style.configure("Treeview.Heading", background="#334155", foreground=fg,
                    font=("DejaVu Sans", 10, "bold"), padding=10)
    style.map("Treeview", background=[("selected", "#334155")], foreground=[("selected", "white")])
    header = tk.Frame(root, bg=bg)
    header.pack(fill="x", padx=26, pady=(24, 12))
    tk.Label(header, text="Codex Resets", font=("DejaVu Sans", 24, "bold"), bg=bg, fg=fg).pack(anchor="w")
    tk.Label(header, text="Deine gespeicherten Resets · alle Accounts aus Codex-Auth",
             font=("DejaVu Sans", 11), bg=bg, fg=muted).pack(anchor="w", pady=(5, 0))
    summary = tk.StringVar(value="Accounts und Ablaufdaten werden geladen …")
    tk.Label(root, textvariable=summary, bg=panel, fg=fg, font=("DejaVu Sans", 12),
             anchor="w", padx=18, pady=16, wraplength=1000).pack(fill="x", padx=26, pady=10)
    toolbar = tk.Frame(root, bg=bg)
    toolbar.pack(fill="x", padx=26, pady=(6, 12))
    sort = tk.StringVar(value="Restlaufzeit absteigend")
    menu = ttk.Combobox(toolbar, textvariable=sort, state="readonly", width=29,
                        values=("Restlaufzeit absteigend", "Bald ablaufend zuerst", "Anzahl Resets absteigend"))
    menu.pack(side="left")
    tk.Label(toolbar, text=f"Rot ≤ 1 Tag   ·   Orange ≤ {args.warning_days} Tage   ·   Grün > {args.warning_days} Tage",
             bg=bg, fg=muted, font=("DejaVu Sans", 9)).pack(side="left", padx=15)
    table_frame = tk.Frame(root, bg=bg)
    table_frame.pack(fill="both", expand=True, padx=26)
    tree = ttk.Treeview(table_frame, columns=("remaining", "expiry", "state"), selectmode="browse")
    tree.heading("#0", text="Account / Reset")
    tree.heading("remaining", text="Restlaufzeit")
    tree.heading("expiry", text="Gültig bis (lokale Zeit)")
    tree.heading("state", text="Status")
    for col, width in (("#0", 355), ("remaining", 185), ("expiry", 180), ("state", 225)):
        tree.column(col, width=width, minwidth=100)
    scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    tree.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    for tag, color in (("critical", "#ff8585"), ("warning", "#ffc078"), ("ok", "#86efac"),
                       ("expired", "#94a3b8"), ("unknown", "#c4b5fd"), ("error", "#ff8585")):
        tree.tag_configure(tag, foreground=color)
    tree.tag_configure("account", font=("DejaVu Sans", 10, "bold"))
    footer = tk.Frame(root, bg=bg)
    footer.pack(fill="x", padx=26, pady=18)
    status = tk.StringVar(value="Verbindung wird hergestellt …")
    tk.Label(footer, textvariable=status, fg=muted, bg=bg, anchor="w", wraplength=840).pack(side="left")
    messages = queue.Queue()
    model = {"snapshot": None, "busy": False, "error": None}

    def render(*_):
        snapshot = model["snapshot"]
        if snapshot is None:
            return
        closed = {tree.item(i, "text") for i in tree.get_children() if not tree.item(i, "open")}
        tree.delete(*tree.get_children())
        now = datetime.now(UTC)
        accounts = list(snapshot["accounts"])
        def account_key(account):
            values = [remaining(c, now) for c in valid_credits(account, now) if remaining(c, now) is not None]
            if sort.get() == "Anzahl Resets absteigend":
                return (bool(account.get("error")), -len(valid_credits(account, now)))
            urgent = sort.get() == "Bald ablaufend zuerst"
            return (not bool(values), min(values) if urgent and values else -max(values) if values else 0)
        accounts.sort(key=account_key)
        total, errors, soon = 0, 0, []
        for account in accounts:
            credits = valid_credits(account, now)
            error = account.get("error")
            total += len(credits) if not error else 0
            errors += bool(error)
            label = account.get("alias") or account.get("email") or "Unbekannter Account"
            if account.get("active"):
                label += "  ● aktiv"
            parent = tree.insert("", "end", text=label, open=label not in closed, tags=("account",),
                                 values=("Unbekannt" if error else f"{len(credits)} Reset(s)", "",
                                         "Abfrage fehlgeschlagen" if error else account.get("plan") or ""))
            if error:
                tree.insert(parent, "end", text=error, tags=("error",))
            for index, credit in enumerate(ordered_credits(account, now, sort.get() == "Bald ablaufend zuerst"), 1):
                seconds = remaining(credit, now)
                expiry = timestamp(credit.get("expires_at"))
                tag = severity(seconds, args.warning_days)
                if tag in ("warning", "critical") and not error:
                    soon.append(seconds)
                state = {"ok": "Verfügbar", "warning": "Bald verwenden", "critical": "Läuft heute ab!",
                         "expired": "Abgelaufen", "unknown": "Ablauf unbekannt"}[tag]
                if credit.get("is_supported_by_plan") is False:
                    state += " · Tarif nicht unterstützt"
                title = credit.get("title") or "Reset"
                if title == "Full reset (Weekly + 5 hr)":
                    title = "Vollständiger Reset (Woche + 5 Std.)"
                tree.insert(parent, "end", text=f"{index}. {title}", tags=(tag,),
                            values=(lifetime(seconds), expiry.astimezone().strftime("%d.%m.%Y  %H:%M") if expiry else "Unbekannt", state))
            if not error and not account["credits"]:
                tree.insert(parent, "end", text="Keine gespeicherten Resets verfügbar", tags=("expired",))
            count = account.get("reported_count")
            if count is not None and count != len(account["credits"]):
                tree.insert(parent, "end", text=f"Server meldet {count} Resets; Detaildaten weichen ab.", tags=("warning",))
        text = f"{total} verfügbare Resets  ·  {len(accounts)} Accounts"
        if soon:
            text += f"  ·  {len(soon)} laufen bald ab – frühester: {lifetime(min(soon))}"
        if errors:
            text += f"  ·  {errors} Account(s) konnten nicht abgefragt werden"
        summary.set(text)
        checked = timestamp(snapshot["checked_at"]).astimezone().strftime("%H:%M:%S")
        status.set(f"Stand {checked} · Automatisch alle {args.interval // 60} Min. · Erinnerungen ab {args.warning_days} Tagen Restlaufzeit")
        if model["error"]:
            summary.set("Aktualisierung fehlgeschlagen – angezeigte Daten sind möglicherweise veraltet.")
            status.set(model["error"] + " Letzter erfolgreicher Stand bleibt sichtbar.")

    def worker():
        try:
            snapshot = load_snapshot(args.codex_home)
            messages.put((snapshot, None))
            notify(snapshot, args.warning_days)
        except (OSError, ValueError, KeyError, TypeError):
            messages.put((None, "Codex-Auth-Daten konnten nicht gelesen werden. CODEX_HOME und registry.json prüfen."))

    def refresh():
        if model["busy"]:
            return
        model["busy"] = True
        button.configure(state="disabled")
        status.set("Alle Accounts werden abgefragt …")
        import threading
        threading.Thread(target=worker, daemon=True).start()

    def poll():
        try:
            snapshot, error = messages.get_nowait()
            model["busy"] = False
            model["error"] = error
            button.configure(state="normal")
            if error:
                status.set(error + " Letzter erfolgreicher Stand bleibt sichtbar.")
                summary.set("Aktualisierung fehlgeschlagen – angezeigte Daten sind möglicherweise veraltet.")
            else:
                model["snapshot"] = snapshot
                render()
        except queue.Empty:
            pass
        root.after(150, poll)

    def scheduled():
        refresh()
        root.after(args.interval * 1000, scheduled)

    def clock_tick():
        if not model["busy"]:
            render()
        root.after(60000, clock_tick)

    button = ttk.Button(footer, text="Jetzt aktualisieren", command=refresh)
    button.pack(side="right")
    menu.bind("<<ComboboxSelected>>", render)
    root.after(50, scheduled)
    root.after(150, poll)
    root.after(60000, clock_tick)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--terminal", action="store_true", help="Print a colored English terminal overview and exit")
    parser.add_argument("--switch", action="store_true", help="Show resets and choose an account without clearing the terminal")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="Terminal colors (default: auto)")
    parser.add_argument("--urgent", action="store_true", help="Show soonest expiry first in terminal mode")
    parser.add_argument("--json", action="store_true", help="Fetch once and print JSON")
    parser.add_argument("--notify", action="store_true", help="Send desktop reminders once")
    parser.add_argument("--watch", action="store_true", help="Check for desktop reminders hourly in the background")
    parser.add_argument("--warning-days", type=int, default=7)
    parser.add_argument("--interval", type=int, default=300, help="GUI refresh interval in seconds (minimum: 60)")
    args = parser.parse_args()
    if args.warning_days < 1 or args.interval < 60:
        parser.error("warning-days must be positive; interval must be at least 60")
    if (args.terminal or args.switch) and (args.json or args.notify or args.watch):
        parser.error("--terminal/--switch cannot be combined with --json, --notify or --watch")
    if args.switch and not sys.stdin.isatty():
        parser.error("--switch requires interactive terminal input")
    if args.terminal or args.switch:
        try:
            snapshot = load_snapshot(args.codex_home)
        except (OSError, ValueError, KeyError, TypeError):
            parser.exit(1, "Could not read the Codex-Auth registry. Check --codex-home or CODEX_HOME.\n")
        accounts = terminal(snapshot, args.warning_days, args.color, args.urgent, numbered=args.switch)
        if args.switch:
            sys.exit(choose_account(accounts, args.codex_home))
        if any(a.get("error") for a in snapshot["accounts"]):
            sys.exit(1)
    elif args.watch:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (STATE / "watch.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            try:
                notify(load_snapshot(args.codex_home), args.warning_days)
            except (OSError, ValueError, KeyError, TypeError):
                print("Codex-Resets: Aktualisierung fehlgeschlagen; erneuter Versuch in einer Stunde.", file=sys.stderr)
            time.sleep(3600)
    elif args.json or args.notify:
        try:
            snapshot = load_snapshot(args.codex_home)
        except (OSError, ValueError, KeyError, TypeError):
            parser.exit(1, "Codex-Auth-Registry konnte nicht gelesen werden.\n")
        if args.json:
            print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        if args.notify:
            notify(snapshot, args.warning_days)
        if any(a.get("error") for a in snapshot["accounts"]):
            sys.exit(1)
    else:
        gui(args)


if __name__ == "__main__":
    main()
