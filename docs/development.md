# Development

The interactive app uses only Python's standard library. Both the thread-renamer backend and its color palette are included in this repository; no sibling checkout is required.

## Run and test

```bash
./codex-accountdeck
python3 -m unittest discover -v
```

The normal suite never signs into real accounts, restarts a real daemon, or calls a model. It uses temporary data and fake command-line programs. Terminal tests require a POSIX pseudoterminal and `curses`.

The bundled renamer also has an opt-in test against the installed Codex CLI. It uses a separate temporary `CODEX_HOME` and does not make inference requests:

```bash
CODEX_RENAMER_INTEGRATION=1 python3 -m unittest tests.test_real_codex -v
```

## Build distributions

Using `uv`:

```bash
uv build
```

Or with the standard Python build frontend:

```bash
python3 -m pip install build
python3 -m build
```

These commands produce a wheel and source distribution under `dist/`. The distribution name and console command are `codex-accountdeck`; the version comes from `codex_hub.VERSION`.

Building does not publish anything. The GitHub workflow tests Python 3.10, 3.12 and 3.14 on Linux, then builds and smoke-tests a wheel. It contains no upload, release, or publishing step.

The workflow also checks macOS, and packs/installs the npm distribution without lifecycle scripts. Python and npm versions must match. The npm entry point uses Python directly, so curses and interactive login retain their normal terminal and signal behavior.

```bash
npm pack
npm install --global ./codex-accountdeck-1.2.0.tgz
```

The npm package uses an explicit file allowlist: the launcher, the five Python modules, the demo SVG, and npm's standard metadata/readme/license files. It does not include tests, local settings, credentials, or caches. Installing from GitHub does not require npm publisher credentials. Publishing to the npm registry is a separate maintainer action (`npm login`, then `npm publish`).

Homebrew is distributed through [nerdworldDE/homebrew-tap](https://github.com/nerdworldDE/homebrew-tap). Its formula pins a release archive and SHA-256 checksum and declares the Python dependency. Future releases need an updated formula URL, version, and checksum.

## Terminal preview

```bash
python3 scripts/render_preview.py
```

This renders the actual account-card layout into `docs/preview.svg`, using a fixed clock and fictional accounts. It reads no real credentials, thread names, or account data.

## Source map

| File | Responsibility |
| --- | --- |
| `codex_hub.py` | AccountDeck interface and keyboard handling |
| `hub_accounts.py` | Account snapshots, read-only usage requests, login/switch/daemon commands |
| `codex_renamer.py`, `codex_colors.py` | Bundled thread management and colors |
| `codex_resets.py` | Shared reset parsing and original optional reset utilities |
| `install_hub.py` | Source-checkout launcher; optional shell shortcuts |

The old `codex-hub` source launcher is retained for existing local installations. `--renamer-dir` / `CODEX_RENAMER_DIR` can explicitly override the bundled renamer.
