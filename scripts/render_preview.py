#!/usr/bin/env python3
"""Render the real account layout as an SVG, using only fictional fixture data."""
import argparse
import curses
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_hub import HubApp, load_renamer
from test_hub import make_fixture

CLOCK = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
PALETTE = {
    "accent": "#7dd3fc", "text": "#e2e8f0", "muted": "#94a3b8", "border": "#475569",
    "green": "#86efac", "amber": "#fdba74", "red": "#fc8181", "purple": "#c4b5fd",
}


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return CLOCK.astimezone(tz) if tz else CLOCK.replace(tzinfo=None)


class Canvas:
    def __init__(self, width=120, height=34):
        self.width, self.height = width, height
        self.cells = [[(" ", 0) for _ in range(width)] for _ in range(height)]

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y, x, text, style=0):
        for offset, char in enumerate(text):
            if 0 <= x + offset < self.width:
                self.cells[y][x + offset] = char, style

    def svg(self):
        colors = {i + 1: color for i, color in enumerate(PALETTE.values())}
        cell_width, cell_height = 8, 20
        width, height = self.width * cell_width + 40, self.height * cell_height + 78
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
            '<title id="title">Codex AccountDeck terminal preview</title>',
            '<desc id="desc">Four fictional accounts, stacked quota bars on the left, reset expiry on the right, and keyboard shortcuts below.</desc>',
            f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="14" fill="#10151e" stroke="#283448"/>',
            '<circle cx="23" cy="22" r="5" fill="#fc8181"/>',
            '<circle cx="41" cy="22" r="5" fill="#fdba74"/>',
            '<circle cx="59" cy="22" r="5" fill="#86efac"/>',
            f'<text x="{width // 2}" y="27" text-anchor="middle" fill="#94a3b8" font-family="monospace" font-size="12">codex-accountdeck · fictional demo accounts</text>',
            '<g font-family="DejaVu Sans Mono, monospace" font-size="13" xml:space="preserve">',
        ]
        for row, cells in enumerate(self.cells):
            start = 0
            while start < len(cells):
                style = cells[start][1]
                end = start + 1
                while end < len(cells) and cells[end][1] == style:
                    end += 1
                text = "".join(char for char, _ in cells[start:end])
                foreground = colors.get(style & 255, PALETTE["text"])
                x, y = 20 + start * cell_width, 62 + row * cell_height
                if style & curses.A_REVERSE:
                    parts.append(f'<rect x="{x}" y="{y - 15}" width="{(end - start) * cell_width}" height="{cell_height}" fill="{foreground}"/>')
                    foreground = "#10151e"
                if text.strip():
                    bold = ' font-weight="bold"' if style & curses.A_BOLD else ""
                    parts.append(f'<text x="{x}" y="{y}" fill="{foreground}"{bold} textLength="{len(text) * cell_width}" lengthAdjust="spacing">{escape(text)}</text>')
                start = end
        parts.extend(["</g>", "</svg>"])
        return "\n".join(parts) + "\n"


def main():
    with tempfile.TemporaryDirectory(prefix="accountdeck-preview-") as temporary:
        home = Path(temporary)
        binary = make_fixture(home)
        args = argparse.Namespace(codex_home=home, renamer=False, limit=200,
                                  codex_bin=str(binary), auth_bin=str(binary),
                                  offline=False, warning_days=7, interval=180)
        app = HubApp(args, load_renamer(ROOT))
        try:
            app.reload_local()
            app.styles = {name: i + 1 for i, name in enumerate(PALETTE)}
            app.screen = Canvas()
            app.daemon = "running"
            app.status = "All accounts up to date · limits show remaining capacity"
            emails = ("work@example.com", "personal@example.com", "client@example.com", "lab@example.com")
            for i, account in enumerate(app.accounts):
                account.update(email=emails[i], usage_live=True, resets_live=True, usage_at=CLOCK.timestamp())
                account["usage"]["primary"]["resets_at"] = (CLOCK + timedelta(hours=3, minutes=24)).timestamp()
                account["usage"]["secondary"]["resets_at"] = (CLOCK + timedelta(days=3, hours=5)).timestamp()
                account["credits"] = [
                    {"expires_at": (CLOCK + timedelta(days=days)).isoformat()}
                    for days in (i + .5, i + 12)
                ]
            with patch("codex_hub.datetime", FixedClock), patch("hub_accounts.datetime", FixedClock):
                app.draw_chrome()
                app.draw_accounts()
            destination = ROOT / "docs/preview.svg"
            destination.write_text(app.screen.svg(), encoding="utf-8")
            print(f"Rendered fictional preview: {destination}")
        finally:
            app.close()


if __name__ == "__main__":
    main()
