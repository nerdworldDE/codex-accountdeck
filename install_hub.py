#!/usr/bin/env python3
"""Install a source-checkout launcher; optionally add the five shell shortcuts."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import shlex
import sys

COMMAND = "codex-accountdeck"


def install(home=None, project=None, shortcuts=False):
    home = Path(home or Path.home())
    project = Path(project or Path(__file__).resolve().parent).resolve()
    directory = home / ".local/bin"
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / COMMAND
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                        shlex.quote(str(project / "codex_hub.py")) + ' "$@"\n')
    launcher.chmod(0o755)
    if not shortcuts:
        return launcher
    profile = home / ".profile"
    text = profile.read_text() if profile.exists() else ""
    replacements = {
        "ws": COMMAND, "sw": COMMAND, "sww": COMMAND,
        "cr": COMMAND + " --renamer", "cn": COMMAND + " --renamer",
    }
    for name, command in replacements.items():
        line = f"alias {name}='{command}'"
        pattern = rf"(?m)^\s*alias {name}=.*$"
        if re.search(pattern, text):
            text = re.sub(pattern, lambda match, line=line: line, text)
        else:
            text = text.rstrip() + "\n" + line + "\n"
    if profile.exists():
        backup = profile.with_name(".profile.before-codex-accountdeck-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        backup.write_bytes(profile.read_bytes())
        backup.chmod(0o600)
    mode = profile.stat().st_mode & 0o777 if profile.exists() else 0o644
    temporary = profile.with_name(".profile.codex-accountdeck-" + str(os.getpid()))
    try:
        temporary.write_text(text)
        temporary.chmod(mode)
        temporary.replace(profile)
    finally:
        temporary.unlink(missing_ok=True)
    return launcher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shortcuts", action="store_true",
                        help="Also set ws/sw/sww/cr/cn in ~/.profile (backed up first)")
    args = parser.parse_args()
    try:
        launcher = install(shortcuts=args.shortcuts)
    except OSError as error:
        parser.exit(1, f"Installation failed: {error}\n")
    print(f"Installed: {launcher}")
    if args.shortcuts:
        print("Shortcuts: ws/sw/sww → Accounts; cr/cn → Threads")
        print("Reload shortcuts in the current shell: . ~/.profile")
    else:
        print("Shell configuration unchanged. Use --shortcuts to add aliases.")


if __name__ == "__main__":
    main()
