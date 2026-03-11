"""Async execution engine for running diagnostic modules."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from lit_diag.modules.base import (
    BaseDiagnosticModule,
    DiagnosticReport,
    Severity,
)
from lit_diag.engine.module_loader import get_all_modules, load_all_modules


async def run_module_with_progress(
    module: BaseDiagnosticModule,
    console: Console,
) -> tuple[str, any]:
    """Run a single module."""
    result = await module.run()
    return module.name, result


async def run_modules(
    module_names: Optional[list[str]] = None,
    console: Optional[Console] = None,
) -> DiagnosticReport:
    """Run specified modules (or all) and return a complete report."""
    if console is None:
        console = Console()

    load_all_modules()
    all_modules = get_all_modules()

    if module_names:
        modules_to_run = {
            name: cls for name, cls in all_modules.items()
            if name in module_names
        }
        unknown = set(module_names) - set(all_modules.keys())
        if unknown:
            console.print(
                f"[yellow]Unknown modules: {', '.join(unknown)}. "
                f"Available: {', '.join(sorted(all_modules.keys()))}[/yellow]"
            )
    else:
        modules_to_run = all_modules

    report = DiagnosticReport(
        hostname=socket.gethostname(),
    )

    start = time.monotonic()

    friendly_names = {
        name: cls.display_name for name, cls in modules_to_run.items()
    }

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        tasks = {}
        for name, cls in modules_to_run.items():
            instance = cls()
            task_id = progress.add_task(
                f"Checking {friendly_names.get(name, name)}...",
                total=None,
            )
            tasks[name] = (instance, task_id)

        results = await asyncio.gather(
            *(instance.run() for instance, _ in tasks.values()),
            return_exceptions=True,
        )

        for (name, (_, task_id)), result in zip(tasks.items(), results):
            if isinstance(result, Exception):
                from lit_diag.modules.base import ModuleResult
                result = ModuleResult(
                    module_name=name,
                    status=Severity.ERROR,
                    error_message=str(result),
                )
            report.modules[name] = result
            progress.update(task_id, completed=True)

    report.duration_ms = (time.monotonic() - start) * 1000
    report.roll_up()

    return report
