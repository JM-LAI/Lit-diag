"""Interactive diagnostic shell -- client-friendly menu interface."""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from lit_diag import __version__
from lit_diag.engine.config import (
    UserRole,
    get_saved_role,
    print_role_reminder,
    resolve_role,
)
from lit_diag.engine.module_loader import get_all_modules, get_module_names, load_all_modules
from lit_diag.engine.privilege import (
    is_root,
    offer_sudo_for_fix,
    offer_sudo_for_reset,
    pre_flight_root_check,
    sudo_relaunch,
    sudo_run_command,
)
from lit_diag.engine.runner import run_modules
from lit_diag.modules.base import Finding
from lit_diag.output.formatters import print_report, save_report_json
from lit_diag.output.report import generate_filename


MODULE_MENU_ORDER = [
    ("gpu", "Check GPU Health"),
    ("nvlink", "Check GPU Interconnects (NVLink)"),
    ("pcie", "Check PCIe Bus"),
    ("kernel_logs", "Check System Logs for Errors"),
    ("storage", "Check Storage Health"),
    ("thermal", "Check Temperatures and Power"),
    ("infiniband", "Check Network (InfiniBand)"),
    ("cuda_tests", "Run GPU Validation Tests"),
    ("driver", "Check NVIDIA Driver"),
    ("system", "Show System Information"),
]

LIGHTNING_BANNER = (
    "[bold purple4]"
    " ██╗     ██╗████████╗      ██████╗ ██╗ █████╗  ██████╗\n"
    "[/bold purple4]"
    "[bold medium_purple]"
    " ██║     ██║╚══██╔══╝      ██╔══██╗██║██╔══██╗██╔════╝\n"
    "[/bold medium_purple]"
    "[bold magenta]"
    " ██║     ██║   ██║   █████╗██║  ██║██║███████║██║  ███╗\n"
    "[/bold magenta]"
    "[bold medium_purple]"
    " ██║     ██║   ██║   ╚════╝██║  ██║██║██╔══██║██║   ██║\n"
    "[/bold medium_purple]"
    "[bold purple4]"
    " ███████╗██║   ██║         ██████╔╝██║██║  ██║╚██████╔╝\n"
    " ╚══════╝╚═╝   ╚═╝         ╚═════╝ ╚═╝╚═╝  ╚═╝ ╚═════╝"
    "[/bold purple4]"
)


def _print_banner(console: Console) -> None:
    """Print the Lightning AI branded banner."""
    console.print()
    console.print(LIGHTNING_BANNER)
    console.print(
        f"  [bold magenta]Lit-Diag[/bold magenta] [dim]by[/dim] "
        f"[bold purple4]Lightning AI[/bold purple4]  "
        f"[dim]v{__version__}[/dim]"
    )
    console.print("  [dim]GPU Cluster Diagnostics[/dim]")
    console.print(
        "  [purple4]═══════════════════════════════════════════════════[/purple4]"
    )
    console.print()


def _print_menu(console: Console) -> None:
    """Print the main menu."""
    console.print()
    console.print("  [bold]What would you like to do?[/bold]\n")
    console.print("  [bold magenta]1)[/bold magenta]  Run All Checks  [dim](recommended)[/dim]")
    console.print()

    for i, (name, label) in enumerate(MODULE_MENU_ORDER, start=2):
        console.print(f"  [bold medium_purple]{i:>2})[/bold medium_purple]  {label}")

    console.print()
    console.print("  [bold medium_purple] d)[/bold medium_purple]  Show Available Tools")
    console.print("  [bold medium_purple] r)[/bold medium_purple]  GPU Reset  [dim](requires root)[/dim]")
    console.print("  [bold medium_purple] s)[/bold medium_purple]  Save Last Report")
    console.print("  [bold medium_purple] q)[/bold medium_purple]  Quit")
    console.print()


def _collect_fixable_findings(report) -> list[Finding]:
    """Gather all findings that have a fix_command."""
    fixable = []
    for result in report.modules.values():
        for finding in result.findings:
            if finding.fix_command:
                fixable.append(finding)
    return fixable


def _offer_fixes(console: Console, report) -> None:
    """After showing results, offer to apply any available quick fixes."""
    fixable = _collect_fixable_findings(report)
    if not fixable:
        return

    console.print(
        "  [purple4]┌─ Quick Fixes Available "
        "──────────────────────────────────────[/purple4]"
    )
    console.print("  [purple4]│[/purple4]")

    for i, f in enumerate(fixable, 1):
        console.print(
            f"  [purple4]│[/purple4]  [bold magenta]{i})[/bold magenta] {f.fix_description}"
        )
        console.print(
            f"  [purple4]│[/purple4]     [dim]Run:[/dim]  sudo {f.fix_command}"
        )
        console.print(
            f"  [purple4]│[/purple4]     [dim]Impact:[/dim] {f.fix_impact}"
        )
        console.print("  [purple4]│[/purple4]")

    console.print(
        "  [purple4]└──────────────────────────────────────────────────────────[/purple4]"
    )
    console.print()

    try:
        choice = console.input(
            "  Apply fixes? (enter number, 'all', or 'skip'): "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if choice == "skip" or not choice:
        return

    if choice == "all":
        fixes_to_run = fixable
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(fixable):
                fixes_to_run = [fixable[idx]]
            else:
                console.print("  [yellow]Invalid choice.[/yellow]\n")
                return
        except ValueError:
            console.print("  [yellow]Invalid choice.[/yellow]\n")
            return

    for fix in fixes_to_run:
        if fix.fix_requires_root and not is_root():
            if offer_sudo_for_fix(console, fix.fix_command, fix.fix_description, fix.fix_impact):
                success, output = sudo_run_command(fix.fix_command)
                if success:
                    console.print(f"    [green]✓ Done:[/green] {fix.fix_description}")
                else:
                    console.print(f"    [red]✗ Failed:[/red] {output}")
            else:
                console.print(f"    [dim]Skipped: {fix.fix_description}[/dim]")
        else:
            from lit_diag.utils.commands import run_command_sync
            result = run_command_sync(fix.fix_command, timeout=30)
            if result.success:
                console.print(f"    [green]✓ Done:[/green] {fix.fix_description}")
            else:
                console.print(f"    [red]✗ Failed:[/red] {result.stderr}")

    console.print()


def interactive_shell() -> None:
    """Run the interactive diagnostic shell."""
    console = Console()

    role = resolve_role()
    if get_saved_role() is not None:
        print_role_reminder(role)

    _print_banner(console)

    load_all_modules()
    last_report = None

    while True:
        _print_menu(console)

        try:
            choice = console.input("  Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]Goodbye.[/dim]\n")
            break

        if choice == "q":
            console.print("\n  [dim]Goodbye.[/dim]\n")
            break

        elif choice == "1":
            # pre-flight root check -- ask BEFORE running
            if not is_root():
                should_proceed = pre_flight_root_check(console)
                if not should_proceed:
                    role_flag = "--staff" if role == UserRole.STAFF else "--client"
                    sudo_relaunch(["run", "--all", role_flag])
                    return

            console.print("\n  [bold]Running all checks...[/bold]\n")
            last_report = asyncio.run(run_modules(console=console))
            print_report(last_report, console, role)
            _offer_fixes(console, last_report)
            _offer_save(console, last_report)

        elif choice in [str(i) for i in range(2, 2 + len(MODULE_MENU_ORDER))]:
            idx = int(choice) - 2
            mod_name, mod_label = MODULE_MENU_ORDER[idx]
            console.print(f"\n  [bold]Running: {mod_label}...[/bold]\n")
            last_report = asyncio.run(run_modules([mod_name], console))
            print_report(last_report, console, role)
            _offer_fixes(console, last_report)
            _offer_save(console, last_report)

        elif choice == "d":
            _show_deps(console)

        elif choice == "r":
            _run_reset(console)

        elif choice == "s":
            if last_report:
                _save_report(console, last_report)
            else:
                console.print("\n  [yellow]No report to save yet. Run some checks first.[/yellow]\n")

        else:
            console.print(f"\n  [yellow]'{choice}' isn't a valid option. Try a number or letter from the menu.[/yellow]\n")


def _offer_save(console: Console, report) -> None:
    """After running all checks, offer to save the report."""
    console.print()
    try:
        save = console.input("  Save this report? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if save in ("y", "yes"):
        _save_report(console, report)


def _save_report(console: Console, report) -> None:
    """Save report to JSON file."""
    filename = generate_filename(report)
    try:
        save_report_json(report, filename)
        console.print(f"\n  [green]Report saved to: {filename}[/green]")
        console.print(f"  [dim]Send this file to support if you need help.[/dim]\n")
    except Exception as e:
        console.print(f"\n  [red]Couldn't save report: {e}[/red]\n")


def _show_deps(console: Console) -> None:
    """Show tool availability."""
    from lit_diag.utils.deps import get_all_tool_status

    status = get_all_tool_status()
    console.print()

    table = Table(title="Diagnostic Tools", show_lines=False)
    table.add_column("Tool", style="bold")
    table.add_column("Status")
    table.add_column("What it's for")

    for tool, info in sorted(status.items()):
        installed = info["installed"]
        status_str = "[green]available[/green]" if installed else "[red]not found[/red]"
        table.add_row(tool, status_str, info["description"])

    console.print(table)
    console.print()


def _run_reset(console: Console) -> None:
    """Run the GPU reset workflow, offering sudo if needed."""
    if not is_root():
        if offer_sudo_for_reset(console):
            sudo_relaunch(["reset-gpu"])
            return
        else:
            console.print("  [dim]GPU reset cancelled.[/dim]\n")
            return

    from lit_diag.remediation.gpu_reset import gpu_reset_workflow
    asyncio.run(gpu_reset_workflow(console))
