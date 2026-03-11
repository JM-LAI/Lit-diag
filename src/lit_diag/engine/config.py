"""User config management -- role prompt, persistence, overrides."""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import Optional


class UserRole(str, Enum):
    CLIENT = "client"
    STAFF = "staff"


CONFIG_DIR = Path.home() / ".lit-diag"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _ensure_config_dir() -> None:
    """Create config directory if it doesn't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load config from disk."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(config: dict) -> None:
    """Save config to disk."""
    _ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")


def get_saved_role() -> Optional[UserRole]:
    """Get the saved user role, if any."""
    config = load_config()
    role_str = config.get("role")
    if role_str:
        try:
            return UserRole(role_str)
        except ValueError:
            return None
    return None


def save_role(role: UserRole) -> None:
    """Save the user role to config."""
    config = load_config()
    config["role"] = role.value
    save_config(config)


def reset_config() -> None:
    """Delete config file to re-trigger the role prompt."""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()


def prompt_for_role() -> UserRole:
    """Interactive prompt asking who's running this."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    console.print()
    console.print(
        Panel(
            "[bold magenta]Welcome to Lit-Diag[/bold magenta] [dim]by[/dim] "
            "[bold purple4]Lightning AI[/bold purple4]\n\n"
            "Who's running this?\n"
            "  [bold magenta]1)[/bold magenta] Client / Customer\n"
            "  [bold magenta]2)[/bold magenta] Support Staff / Engineer",
            title="[purple4]Setup[/purple4]",
            border_style="purple4",
        )
    )

    while True:
        choice = console.input("\n  Choice [1]: ").strip()
        if choice in ("", "1"):
            role = UserRole.CLIENT
            break
        elif choice == "2":
            role = UserRole.STAFF
            break
        else:
            console.print("  [yellow]Please enter 1 or 2.[/yellow]")

    save_role(role)
    console.print(f"\n  [green]Saved.[/green] Running as: [bold]{role.value.title()}[/bold]")
    console.print(
        f"  [dim]Change anytime with --{'staff' if role == UserRole.CLIENT else 'client'}"
        f", or --reset-config to re-choose.[/dim]\n"
    )
    return role


def resolve_role(
    client_flag: bool = False,
    staff_flag: bool = False,
) -> UserRole:
    """Determine which role to use, checking flags > config > prompt."""
    if client_flag:
        return UserRole.CLIENT
    if staff_flag:
        return UserRole.STAFF

    saved = get_saved_role()
    if saved is not None:
        return saved

    return prompt_for_role()


def print_role_reminder(role: UserRole) -> None:
    """Print the one-liner role reminder on startup."""
    from rich.console import Console

    console = Console()
    if role == UserRole.CLIENT:
        console.print(
            "  [dim]Running as: Client  "
            "(change with --staff, or --reset-config to re-choose)[/dim]"
        )
    else:
        console.print(
            "  [dim]Running as: Support Staff  "
            "(change with --client, or --reset-config to re-choose)[/dim]"
        )
