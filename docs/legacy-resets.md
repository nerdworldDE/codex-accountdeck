# Original reset utilities

The main app is **Codex AccountDeck**. The original one-shot terminal view and Tkinter desktop view remain available in a source checkout.

## One-shot terminal output

```bash
python3 codex_resets.py --terminal
python3 codex_resets.py --terminal --urgent
python3 codex_resets.py --terminal --color always
python3 codex_resets.py --json
```

Terminal output is English. Color is automatic for terminals; `NO_COLOR` or `--color never` disables it. Available color modes are `auto`, `always`, and `never`. `--warning-days` adjusts the seven-day warning threshold.

The old line-based account picker is still available with `--switch`. It delegates switching to Codex-Auth, but does not restart the daemon. Use AccountDeck for the complete interactive workflow.

## Optional desktop view and reminders

```bash
python3 codex_resets.py
python3 codex_resets.py --notify
python3 codex_resets.py --watch
```

The desktop view needs Tkinter and a graphical session. Reminders need `notify-send` on Linux or Windows PowerShell under WSL. Reminder delivery also depends on the desktop's notification settings. Daily deduplication state is saved under `$XDG_STATE_HOME/codex-resets` (default: `~/.local/state/codex-resets`).

No autostart entry or desktop shortcut is installed by the public AccountDeck package. The old desktop interface and notification text are German.

All commands honor `CODEX_HOME` or `--codex-home`.
