"""Root detection, privilege handling, and sudo elevation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def is_root() -> bool:
    """Check if running as root."""
    return os.geteuid() == 0


def get_lit_diag_path() -> str:
    """Get the full path to the lit-diag executable."""
    which = shutil.which("lit-diag")
    if which:
        return which
    return os.path.join(os.path.expanduser("~"), ".local", "bin", "lit-diag")


def _get_user_site_packages() -> str:
    """Find the user site-packages dir where lit-diag is installed."""
    import site
    user_site = site.getusersitepackages()
    if os.path.isdir(user_site):
        return user_site
    # fallback: guess based on home dir and python version
    home = os.path.expanduser("~")
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    return os.path.join(home, ".local", "lib", f"python{ver}", "site-packages")


def sudo_relaunch(extra_args: list[str] | None = None) -> None:
    """Re-launch lit-diag with sudo, preserving the Python package path."""
    lit_path = get_lit_diag_path()
    pypath = _get_user_site_packages()

    # sudo strips env vars, so we use 'sudo env PYTHONPATH=...' to pass it through
    args = [
        "sudo", "env",
        f"PYTHONPATH={pypath}",
        lit_path,
    ] + (extra_args or [])
    os.execvp("sudo", args)


def sudo_run_command(cmd: str) -> tuple[bool, str]:
    """Run a single command via sudo. Returns (success, output)."""
    try:
        result = subprocess.run(
            f"sudo {cmd}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip() or result.stderr.strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        return False, str(e)


ROOT_MODULES = {
    "kernel_logs": "Scans for GPU errors (XID), crashes, and system events",
    "thermal": "Reads CPU temps, fan speeds, and power supply health",
    "storage": "Checks NVMe drive health, wear levels, and RAID status",
}


def pre_flight_root_check(console) -> bool:
    """Check if root is needed and offer to switch. Returns True if we should proceed."""
    if is_root():
        return True

    from rich.panel import Panel

    console.print()
    lines = "  Some checks need root access for full results:\n\n"
    for mod, desc in ROOT_MODULES.items():
        friendly = mod.replace("_", " ").title()
        lines += f"    [bold]{friendly:15s}[/bold] [dim]{desc}[/dim]\n"
    lines += "\n  Without root, these will be skipped. Everything else runs normally."

    console.print(Panel(lines, title="[purple4]Root Access[/purple4]", border_style="purple4"))

    try:
        choice = console.input("\n  Switch to root for the complete picture? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return True

    if choice in ("y", "yes"):
        console.print("  [dim]Restarting with root access...[/dim]\n")
        return False  # caller should re-launch
    return True


def offer_sudo_for_reset(console) -> bool:
    """Offer to re-launch with sudo for GPU reset. Returns True if user wants sudo."""
    from rich.panel import Panel

    console.print()
    console.print(
        Panel(
            "[bold]GPU Reset requires root access.[/bold]\n\n"
            "This will restart lit-diag with elevated privileges.\n"
            "Your password may be required.",
            title="[purple4]Root Access Needed[/purple4]",
            border_style="purple4",
        )
    )

    try:
        choice = console.input("\n  Switch to root and run GPU reset? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False

    return choice in ("y", "yes")


def offer_sudo_for_fix(console, fix_command: str, fix_description: str, fix_impact: str) -> bool:
    """Offer to run a fix command with sudo. Returns True if user confirmed."""
    console.print()
    console.print(f"    [bold magenta]Command:[/bold magenta]  sudo {fix_command}")
    console.print(f"    [bold magenta]What it does:[/bold magenta]  {fix_description}")
    console.print(f"    [bold magenta]Impact:[/bold magenta]  {fix_impact}")
    console.print()

    try:
        choice = console.input("    Run this fix? Your password may be required. (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False

    return choice in ("y", "yes")
