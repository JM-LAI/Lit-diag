"""Auto-update check -- compares local version to latest on GitHub."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from lit_diag import __version__

REPO_VERSION_URL = (
    "https://raw.githubusercontent.com/JM-LAI/Lit-diag/main/src/lit_diag/__init__.py"
)
REPO_URL = "https://github.com/JM-LAI/Lit-diag.git"

CACHE_DIR = Path.home() / ".lit-diag"
CACHE_FILE = CACHE_DIR / ".update_cache.json"
CHECK_INTERVAL = 3600  # once per hour


def _version_tuple(v: str) -> tuple[int, ...]:
    """Turn '0.2.1' into (0, 2, 1) for comparison."""
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _fetch_remote_version() -> str | None:
    """Grab __version__ from the repo's __init__.py. Quick and quiet."""
    try:
        from urllib.request import urlopen, Request

        req = Request(REPO_VERSION_URL, headers={"User-Agent": "lit-diag-updater"})
        with urlopen(req, timeout=3) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        return match.group(1) if match else None
    except Exception:
        return None


def _read_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def _find_venv_pip() -> str | None:
    """Find the pip that belongs to our venv (if we're running from one)."""
    python = sys.executable
    venv_dir = os.path.dirname(os.path.dirname(python))
    pip_path = os.path.join(venv_dir, "bin", "pip")
    if os.path.isfile(pip_path):
        return pip_path
    return None


def _needs_sudo() -> bool:
    """Check if the venv is owned by root and we're not root."""
    if os.geteuid() == 0:
        return False
    venv_dir = os.path.dirname(os.path.dirname(sys.executable))
    try:
        return os.stat(venv_dir).st_uid == 0
    except OSError:
        return False


def _do_update() -> bool:
    """Run the actual update. Returns True if it worked."""
    import subprocess

    pip_path = _find_venv_pip()
    if pip_path:
        cmd = [pip_path, "install", "--upgrade", "--quiet", f"git+{REPO_URL}"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", f"git+{REPO_URL}"]

    # if the venv is root-owned, we need sudo
    if _needs_sudo():
        cmd = ["sudo"] + cmd

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return result.returncode == 0
    except Exception:
        return False


def check_for_update(console) -> None:
    """Check if a newer version exists and offer to update.

    Runs at most once per hour (cached). Skips silently on any error.
    """
    if os.environ.get("LIT_DIAG_NO_UPDATE"):
        return

    cache = _read_cache()
    now = time.time()
    last_check = cache.get("last_check", 0)
    cached_remote = cache.get("remote_version")

    # only use cache if it found a NEWER version last time;
    # if the cached result was "no update", re-check every launch
    # so we don't miss an update for an entire hour
    cache_is_useful = (
        cached_remote
        and now - last_check < CHECK_INTERVAL
        and _version_tuple(cached_remote) > _version_tuple(__version__)
    )

    if cache_is_useful:
        remote = cached_remote
    else:
        remote = _fetch_remote_version()
        cache["last_check"] = now
        cache["remote_version"] = remote
        _write_cache(cache)

    if not remote:
        return

    try:
        if _version_tuple(remote) <= _version_tuple(__version__):
            return
    except Exception:
        return

    # always tell the user about the update
    console.print(
        f"\n  [bold yellow]Update available:[/bold yellow] "
        f"[dim]{__version__}[/dim] → [bold green]{remote}[/bold green]"
    )

    # only offer interactive update if we can prompt
    interactive = sys.stdin.isatty() or sys.stdout.isatty()
    if interactive:
        try:
            choice = console.input("  Update now? (Y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print(
                f"  [dim]Run: curl -fsSL https://raw.githubusercontent.com/"
                f"JM-LAI/Lit-diag/main/get.sh | sudo bash[/dim]\n"
            )
            return

        if choice in ("", "y", "yes"):
            console.print("  [dim]Updating...[/dim]")
            if _do_update():
                console.print(
                    f"  [green]Updated to v{remote}.[/green] "
                    f"Restart lit-diag to use the new version.\n"
                )
            else:
                console.print(
                    "  [red]Update failed.[/red] Try manually:\n"
                    f"  [dim]curl -fsSL https://raw.githubusercontent.com/"
                    f"JM-LAI/Lit-diag/main/get.sh | sudo bash[/dim]\n"
                )
    else:
        console.print(
            f"  [dim]Run: curl -fsSL https://raw.githubusercontent.com/"
            f"JM-LAI/Lit-diag/main/get.sh | sudo bash[/dim]\n"
        )
