"""GPU Reset Workflow -- full GPU recovery sequence.

Steps:
  1. Kill GPU processes
  2. Stop NVIDIA systemd services
  3. Unload NVIDIA kernel modules
  4. Perform PCIe bus reset
  5. Reload kernel modules
  6. Restart NVIDIA services

Requires root.
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.panel import Panel

from lit_diag.engine.privilege import is_root
from lit_diag.utils.commands import run_command


NVIDIA_SERVICES = [
    "nvidia-persistenced",
    "nvidia-fabricmanager",
    "dcgm",
    "nvsm",
]

NVIDIA_MODULES = [
    "nvidia_uvm",
    "nvidia_drm",
    "nvidia_modeset",
    "nvidia_peermem",
    "nvidia",
]


async def _step(console: Console, msg: str, cmd: str, timeout: float = 30.0) -> bool:
    """Run a step, print result, return success."""
    console.print(f"  [dim]→ {msg}...[/dim]", end="")
    result = await run_command(cmd, timeout=timeout)
    if result.success:
        console.print(" [green]done[/green]")
    else:
        console.print(f" [yellow]skipped[/yellow] [dim]({result.stderr or 'not applicable'})[/dim]")
    return result.success


async def gpu_reset_workflow(console: Console | None = None) -> bool:
    """Run the full GPU reset workflow. Returns True on success."""
    if console is None:
        console = Console()

    if not is_root():
        console.print(
            Panel(
                "[bold red]GPU reset requires root access.[/bold red]\n\n"
                "Run with: [bold]sudo lit-diag reset-gpu[/bold]",
                title="Permission Required",
                border_style="red",
            )
        )
        return False

    console.print()
    console.print(
        Panel(
            "[bold yellow]GPU Reset Workflow[/bold yellow]\n\n"
            "This will:\n"
            "  1. Kill all GPU processes\n"
            "  2. Stop NVIDIA services\n"
            "  3. Unload GPU kernel modules\n"
            "  4. Reset PCIe bus for GPU devices\n"
            "  5. Reload kernel modules\n"
            "  6. Restart NVIDIA services\n\n"
            "[bold]This WILL interrupt any running GPU workloads.[/bold]",
            title="Warning",
            border_style="yellow",
        )
    )

    confirm = console.input("\n  Proceed with GPU reset? (y/N): ").strip().lower()
    if confirm not in ("y", "yes"):
        console.print("  [dim]Cancelled.[/dim]\n")
        return False

    console.print()

    # step 1: kill GPU processes
    console.print("  [bold]Step 1: Killing GPU processes[/bold]")
    result = await run_command("nvidia-smi --query-compute-apps=pid --format=csv,noheader", timeout=10)
    if result.success and result.stdout.strip():
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        for pid in pids:
            await run_command(f"kill -9 {pid}", timeout=5)
        console.print(f"  [green]Killed {len(pids)} GPU process(es)[/green]")
    else:
        console.print("  [dim]No GPU processes running[/dim]")

    # step 2: stop NVIDIA services
    console.print("\n  [bold]Step 2: Stopping NVIDIA services[/bold]")
    for svc in NVIDIA_SERVICES:
        await _step(console, f"Stopping {svc}", f"systemctl stop {svc} 2>/dev/null")

    # step 3: unload kernel modules
    console.print("\n  [bold]Step 3: Unloading kernel modules[/bold]")
    for mod in NVIDIA_MODULES:
        await _step(console, f"Removing {mod}", f"modprobe -r {mod} 2>/dev/null")

    # step 4: PCIe bus reset
    console.print("\n  [bold]Step 4: PCIe bus reset[/bold]")
    gpu_bdfs = await run_command("lspci -d 10de: -D | awk '{print $1}'", timeout=10)
    if gpu_bdfs.success and gpu_bdfs.stdout.strip():
        for bdf in gpu_bdfs.stdout.strip().split("\n"):
            bdf = bdf.strip()
            if not bdf:
                continue
            remove_path = f"/sys/bus/pci/devices/{bdf}/remove"
            await _step(console, f"Removing {bdf}", f"echo 1 > {remove_path}")

        await asyncio.sleep(2)
        await _step(console, "Rescanning PCIe bus", "echo 1 > /sys/bus/pci/rescan")
        await asyncio.sleep(3)
    else:
        console.print("  [yellow]No NVIDIA PCIe devices found for reset[/yellow]")

    # step 5: reload modules
    console.print("\n  [bold]Step 5: Reloading kernel modules[/bold]")
    await _step(console, "Loading nvidia module", "modprobe nvidia", timeout=30)
    await asyncio.sleep(1)
    await _step(console, "Loading nvidia_uvm", "modprobe nvidia_uvm", timeout=10)
    await _step(console, "Loading nvidia_modeset", "modprobe nvidia_modeset", timeout=10)

    # step 6: restart services
    console.print("\n  [bold]Step 6: Restarting NVIDIA services[/bold]")
    for svc in NVIDIA_SERVICES:
        await _step(console, f"Starting {svc}", f"systemctl start {svc} 2>/dev/null")

    # verify
    console.print("\n  [bold]Verifying...[/bold]")
    verify = await run_command("nvidia-smi -L", timeout=30)
    if verify.success:
        gpu_count = len([l for l in verify.stdout.split("\n") if l.strip()])
        console.print(f"  [green]GPU reset complete. {gpu_count} GPU(s) detected.[/green]\n")
        return True
    else:
        console.print(
            "  [red]GPU reset completed but nvidia-smi is not responding.[/red]\n"
            "  [dim]The driver may need more time to initialize. "
            "Try running 'nvidia-smi' in a minute.[/dim]\n"
        )
        return False
