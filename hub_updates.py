"""Check public releases and delegate explicit upgrades to the existing installer."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from http.client import HTTPException
import json
import math
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from codex_resets import NoRedirect


REPOSITORY = "https://github.com/nerdworldDE/codex-accountdeck"
RELEASE_API = "https://api.github.com/repos/nerdworldDE/codex-accountdeck/releases/latest"
FORMULA = "nerdworldDE/tap/codex-accountdeck"
PACKAGE = "codex-accountdeck"
CHECK_INTERVAL = 24 * 60 * 60
RETRY_INTERVAL = 60 * 60


def stable_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,5})(\.(0|[1-9][0-9]{0,5})){2}", value):
        return None
    return tuple(map(int, value.split(".")))


def release_page(version=None):
    return REPOSITORY + (f"/releases/tag/v{version}" if version else "/releases/latest")


@dataclass(frozen=True)
class UpdateStatus:
    latest: str | None = None
    attempted_at: float = 0
    failed: bool = False

    def available(self, current):
        latest, installed = stable_version(self.latest), stable_version(current)
        return latest is not None and installed is not None and latest > installed


class ReleaseCheck:
    def __init__(self, home):
        self.path = Path(home) / "accountdeck" / "update-check.json"

    def cached(self):
        try:
            with self.path.open("rb") as stream:
                data = json.loads(stream.read(4096))
            latest = data.get("latest")
            attempted_at, failed = float(data["attempted_at"]), data["failed"]
            if (latest is None or stable_version(latest)) and math.isfinite(attempted_at) and isinstance(failed, bool):
                return UpdateStatus(latest, attempted_at, failed)
        except (OSError, ValueError, AttributeError, KeyError, TypeError, OverflowError, RecursionError):
            pass
        return UpdateStatus()

    def save(self, result):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(asdict(result), stream)
            temporary.replace(self.path)
        except OSError:
            pass  # The live result is still useful with a read-only cache.
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def fetch():
        request = urllib.request.Request(RELEASE_API, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Codex-AccountDeck",
        })
        with urllib.request.build_opener(NoRedirect).open(request, timeout=4) as response:
            data = json.loads(response.read(256 * 1024))
        tag = data.get("tag_name") if isinstance(data, dict) else None
        if (not isinstance(tag, str) or not tag.startswith("v") or not stable_version(tag[1:])
                or data.get("draft") is not False or data.get("prerelease") is not False):
            raise ValueError("Expected a published stable release")
        return tag[1:]

    def check(self, force=False):
        cached = self.cached()
        interval = RETRY_INTERVAL if cached.failed else CHECK_INTERVAL
        if not force and 0 <= time.time() - cached.attempted_at < interval:
            return cached
        try:
            result = UpdateStatus(self.fetch(), time.time())
        except urllib.error.HTTPError as error:
            error.close()
            result = UpdateStatus(cached.latest, time.time(), failed=True)
        except (OSError, ValueError, HTTPException, RecursionError):
            result = UpdateStatus(cached.latest, time.time(), failed=True)
        self.save(result)
        return result


class UpdateError(Exception):
    pass


def run_installer(command, *, cwd=None, capture=False):
    try:
        return subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=capture,
                              timeout=10 if capture else None).stdout
    except FileNotFoundError as error:
        raise UpdateError(f"Command not found: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        raise UpdateError(f"{Path(command[0]).name} exited with status {error.returncode}") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"Could not run {Path(command[0]).name}") from error


def checkout_commands(root, tag):
    if run_installer(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root, capture=True).strip():
        raise UpdateError("Save your checkout changes before updating.")
    if run_installer(["git", "branch", "--show-current"], cwd=root, capture=True).strip() != "main":
        raise UpdateError("Open the main branch to update this checkout.")
    # get-url reflects the first fetch URL after Git's insteadOf rules.
    origin = run_installer(["git", "remote", "get-url", "origin"], cwd=root, capture=True).strip()
    official = (REPOSITORY, "git@github.com:nerdworldDE/codex-accountdeck", "ssh://git@github.com/nerdworldDE/codex-accountdeck")
    if origin.rstrip("/").removesuffix(".git").lower() not in {url.lower() for url in official}:
        raise UpdateError("Update this checkout through its configured Git repository.")
    ref = f"refs/tags/{tag}"
    return [["git", "fetch", "--no-tags", "origin", f"{ref}:{ref}"],
            ["git", "merge", "--ff-only", "--no-overwrite-ignore", ref]]


def apply_update(root, version):
    """Upgrade the current installation, using only validated release versions."""
    if not stable_version(version):
        raise UpdateError("Invalid release version")
    root = Path(root).resolve()
    tag = f"v{version}"
    download = f"{REPOSITORY}/releases/download/{tag}"
    wheel = f"{download}/codex_accountdeck-{version}-py3-none-any.whl"
    prefix = Path(sys.prefix)
    cwd = None
    if root.name == "libexec" and root.parent.parent.name == PACKAGE and root.parent.parent.parent.name == "Cellar":
        brew_prefix = root.parents[3]
        brew = str(brew_prefix / "bin/brew")
        manager = "Homebrew"
        commands = [[brew, "update"], [brew, "upgrade", FORMULA]]
        launcher = [str(brew_prefix / "opt" / PACKAGE / "bin" / PACKAGE)]
    elif root.name == PACKAGE and root.parent.name == "node_modules" and root.parent.parent.name == "lib":
        npm_prefix = root.parents[2]
        manager = "npm"
        commands = [["npm", "install", "--global", "--prefix", str(npm_prefix), "--allow-remote=root",
                     "--ignore-scripts", "--no-audit", "--no-fund", f"{download}/{PACKAGE}-{version}.tgz"]]
        launcher = [str(npm_prefix / "bin" / PACKAGE)]
    elif (root / ".git").exists():
        manager, cwd = "Git", root
        commands = checkout_commands(root, tag)
        launcher = [sys.executable, str(root / "codex_hub.py")]
    elif (prefix / "uv-receipt.toml").is_file():
        raise UpdateError("Update this uv tool with " + shlex.join(["uv", "tool", "install", "--force", wheel]))
    elif (prefix / "pipx_metadata.json").is_file():
        raise UpdateError("Update this pipx tool with " + shlex.join(["pipx", "install", "--force", wheel]))
    elif sys.prefix != sys.base_prefix and root.name == "site-packages" and root.is_relative_to(prefix.resolve()):
        manager = "pip"
        commands = [[sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps", wheel]]
        launcher = [str(prefix / "bin" / PACKAGE)]
    else:
        raise UpdateError(f"Install v{version} using the release instructions at {release_page(version)}")

    print(f"Updating Codex AccountDeck with {manager}…", flush=True)
    for command in commands:
        run_installer(command, cwd=cwd)
    output = run_installer([*launcher, "--version"], cwd=cwd, capture=True).strip()
    installed = stable_version(output.removeprefix(f"{PACKAGE} ")) if output.startswith(f"{PACKAGE} ") else None
    if installed is None or installed < stable_version(version):
        raise UpdateError(f"{manager} has not installed v{version} yet. Check {release_page(version)}.")
    print(f"Installed {output}.")
