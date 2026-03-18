"""CLI entry point -- click group with subcommands."""

from __future__ import annotations

import asyncio
import sys

import click
from rich.console import Console

from lit_diag import __version__
from lit_diag.engine.config import (
    UserRole,
    get_saved_role,
    print_role_reminder,
    prompt_for_role,
    reset_config,
    resolve_role,
)


console = Console()


@click.group(invoke_without_command=True)
@click.option("--version", is_flag=True, help="Show version and exit.")
@click.option("--reset-config", "do_reset", is_flag=True, help="Re-prompt the role choice.")
@click.option("--client", "client_flag", is_flag=True, help="Force client-friendly output.")
@click.option("--staff", "staff_flag", is_flag=True, help="Force full engineer output.")
@click.pass_context
def cli(ctx, version, do_reset, client_flag, staff_flag):
    """Lit-Diag by Lightning AI -- GPU cluster diagnostics made simple."""
    ctx.ensure_object(dict)
    ctx.obj["client_flag"] = client_flag
    ctx.obj["staff_flag"] = staff_flag

    if version:
        console.print(f"Lit-Diag by Lightning AI  v{__version__}")
        raise SystemExit(0)

    if do_reset:
        reset_config()
        console.print("[green]Config reset.[/green] You'll be prompted on next run.")
        raise SystemExit(0)

    # check for updates on every interactive launch (cached, max once/hour)
    from lit_diag.updater import check_for_update
    check_for_update(console)

    if ctx.invoked_subcommand is None:
        # if --staff or --client passed at top level, set the role before shell
        if staff_flag:
            from lit_diag.engine.config import save_role
            save_role(UserRole.STAFF)
        elif client_flag:
            from lit_diag.engine.config import save_role
            save_role(UserRole.CLIENT)
        from lit_diag.shell import interactive_shell
        interactive_shell()
        raise SystemExit(0)


@cli.command()
@click.argument("modules", nargs=-1)
@click.option("--all", "run_all", is_flag=True, help="Run all diagnostic modules.")
@click.option("--client", "client_flag", is_flag=True, help="Force client-friendly output.")
@click.option("--staff", "staff_flag", is_flag=True, help="Force full engineer output.")
@click.option("--non-interactive", "non_interactive", is_flag=True, help="No prompts; safe for SSH/automation.")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
@click.option("-o", "--output", "output_file", type=str, default=None, help="Save JSON report to file.")
def run(modules, run_all, client_flag, staff_flag, non_interactive, json_output, output_file):
    """Run diagnostic checks.

    Specify module names or use --all for everything.

    Examples:

        lit-diag run --all

        lit-diag run gpu nvlink pcie

        lit-diag run --all --json -o report.json
    """
    # JSON output is role-agnostic -- skip the prompt
    if json_output or output_file:
        role = UserRole.CLIENT
    else:
        role = resolve_role(client_flag, staff_flag)
        if get_saved_role() is not None and not client_flag and not staff_flag:
            print_role_reminder(role)

    module_names = list(modules) if modules else None
    if run_all:
        module_names = None

    if not run_all and not modules:
        console.print(
            "[yellow]Specify modules to run, or use --all for everything.[/yellow]\n"
            "[dim]Example: lit-diag run --all[/dim]\n"
            "[dim]Example: lit-diag run gpu nvlink pcie[/dim]"
        )
        raise SystemExit(1)

    # pre-flight root check for --all (non-JSON mode); skip when non-interactive
    if run_all and not json_output and not output_file and not non_interactive:
        from lit_diag.engine.privilege import is_root, pre_flight_root_check, sudo_relaunch
        if not is_root():
            should_proceed = pre_flight_root_check(console)
            if not should_proceed:
                role_flag = "--staff" if staff_flag else "--client"
                sudo_relaunch(["run", "--all", role_flag])
                return

    from lit_diag.engine.runner import run_modules
    from lit_diag.output.formatters import print_report, report_to_json, save_report_json

    report = asyncio.run(run_modules(module_names, console))

    if json_output or output_file:
        if output_file:
            save_report_json(report, output_file)
            console.print(f"\n  [green]Report saved to {output_file}[/green]\n")
        else:
            click.echo(report_to_json(report))
    else:
        print_report(report, console, role)

        # offer quick fixes if any are available (skip prompts when non-interactive)
        if not non_interactive:
            from lit_diag.shell import _offer_fixes
            _offer_fixes(console, report)


@cli.command()
def shell():
    """Interactive diagnostic shell (recommended for first-time users)."""
    from lit_diag.shell import interactive_shell
    interactive_shell()


@cli.command(name="reset-gpu")
def reset_gpu():
    """Full GPU reset workflow (requires root)."""
    import asyncio
    from lit_diag.remediation.gpu_reset import gpu_reset_workflow
    asyncio.run(gpu_reset_workflow(console))


@cli.command()
def deps():
    """Show available and missing diagnostic tools."""
    from lit_diag.utils.deps import get_all_tool_status
    from rich.table import Table

    status = get_all_tool_status()

    table = Table(title="Diagnostic Tool Status", show_lines=False)
    table.add_column("Tool", style="bold")
    table.add_column("Status")
    table.add_column("Description")
    table.add_column("Install")

    for tool, info in sorted(status.items()):
        installed = info["installed"]
        status_str = "[green]installed[/green]" if installed else "[red]missing[/red]"
        hint = info.get("install_hint", "") if not installed else ""
        table.add_row(tool, status_str, info["description"], hint)

    console.print()
    console.print(table)
    console.print()


def _register_module_commands():
    """Register shortcut commands for each module (e.g., `lit-diag gpu`)."""
    from lit_diag.engine.module_loader import load_all_modules, get_all_modules

    load_all_modules()
    modules = get_all_modules()

    for name, cls in modules.items():
        display = cls.display_name

        def make_cmd(mod_name, mod_display):
            @cli.command(name=mod_name, help=f"Run {mod_display} diagnostics.")
            @click.option("--client", "client_flag", is_flag=True, hidden=True)
            @click.option("--staff", "staff_flag", is_flag=True, hidden=True)
            @click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
            @click.option("-o", "--output", "output_file", type=str, default=None)
            def cmd(client_flag, staff_flag, json_output, output_file):
                role = resolve_role(client_flag, staff_flag)
                if get_saved_role() is not None and not client_flag and not staff_flag:
                    print_role_reminder(role)

                from lit_diag.engine.runner import run_modules
                from lit_diag.output.formatters import (
                    print_report, report_to_json, save_report_json,
                )

                report = asyncio.run(run_modules([mod_name], console))

                if json_output or output_file:
                    if output_file:
                        save_report_json(report, output_file)
                        console.print(f"\n  [green]Report saved to {output_file}[/green]\n")
                    else:
                        click.echo(report_to_json(report))
                else:
                    print_report(report, console, role)

            return cmd

        make_cmd(name, display)


try:
    _register_module_commands()
except Exception:
    pass


def main():
    """Entry point."""
    cli()


if __name__ == "__main__":
    main()
