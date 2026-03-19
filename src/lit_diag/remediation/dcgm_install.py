"""DCGM Installation -- installs NVIDIA datacenter-gpu-manager.

Handles:
  1. NVIDIA CUDA keyring setup (if missing)
  2. CUDA version detection from nvidia-smi
  3. Package installation with held-package workarounds
  4. Service enablement
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.panel import Panel

from lit_diag.engine.privilege import is_root
from lit_diag.utils.commands import run_command


async def install_dcgm(console: Console) -> bool:
    """Install DCGM interactively with step-by-step feedback. Returns True on success."""

    if not is_root():
        console.print("  [red]DCGM installation requires root.[/red]")
        console.print("  [dim]Re-run with: sudo lit-diag[/dim]\n")
        return False

    console.print()
    console.print(
        Panel(
            "  [bold]DCGM Installer[/bold]\n\n"
            "  This will install NVIDIA's Data Center GPU Manager,\n"
            "  which enables full GPU hardware validation.\n\n"
            "  Steps:\n"
            "    1. Set up NVIDIA package repository\n"
            "    2. Detect CUDA version\n"
            "    3. Install datacenter-gpu-manager\n"
            "    4. Start the DCGM service",
            border_style="purple4",
        )
    )

    try:
        choice = console.input("\n  Proceed with installation? (Y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False

    if choice not in ("", "y", "yes"):
        console.print("  [dim]Skipped.[/dim]\n")
        return False

    # step 1: CUDA keyring
    console.print("\n  [bold][1/4][/bold] Checking NVIDIA repository keyring...")

    keyring_check = await run_command("dpkg -l cuda-keyring 2>/dev/null", timeout=10)
    if keyring_check.success and "cuda-keyring" in keyring_check.stdout:
        console.print("    [green]✓[/green] Keyring already installed")
    else:
        console.print("    [dim]Installing NVIDIA CUDA keyring...[/dim]")
        distro = await run_command("lsb_release -rs 2>/dev/null | tr -d '.'", timeout=5)
        distro_ver = distro.stdout.strip() if distro.success else "2204"

        keyring_url = (
            f"https://developer.download.nvidia.com/compute/cuda/repos/"
            f"ubuntu{distro_ver}/x86_64/cuda-keyring_1.1-1_all.deb"
        )
        dl = await run_command(
            f"wget -q {keyring_url} -O /tmp/cuda-keyring.deb && "
            f"dpkg -i /tmp/cuda-keyring.deb && "
            f"rm -f /tmp/cuda-keyring.deb",
            timeout=30,
        )
        if dl.success:
            console.print("    [green]✓[/green] Keyring installed")
        else:
            console.print(f"    [yellow]⚠[/yellow] Keyring install failed: {dl.stderr[:200]}")
            console.print("    [dim]Continuing anyway -- repo might already be configured[/dim]")

        await run_command("apt-get update -qq", timeout=60)

    # step 2: detect CUDA version
    console.print("\n  [bold][2/4][/bold] Detecting CUDA version...")

    smi = await run_command("nvidia-smi", timeout=15)
    cuda_ver = ""
    if smi.success:
        import re
        m = re.search(r"CUDA Version:\s+(\d+)", smi.stdout)
        if m:
            cuda_ver = m.group(1)

    if cuda_ver:
        console.print(f"    [green]✓[/green] CUDA {cuda_ver} detected")
    else:
        console.print("    [yellow]⚠[/yellow] Could not detect CUDA version, trying default package")

    # step 3: install DCGM
    console.print("\n  [bold][3/4][/bold] Installing datacenter-gpu-manager...")

    if cuda_ver:
        pkg = f"datacenter-gpu-manager-4-cuda{cuda_ver}"
    else:
        pkg = "datacenter-gpu-manager"

    install = await run_command(
        f"apt-get install -y -qq {pkg} 2>&1",
        timeout=120,
    )

    if not install.success:
        # try without the cuda version suffix
        console.print(f"    [dim]Package {pkg} failed, trying generic...[/dim]")
        install = await run_command(
            "apt-get install -y -qq datacenter-gpu-manager 2>&1",
            timeout=120,
        )

    if install.success:
        console.print(f"    [green]✓[/green] DCGM installed")
    else:
        console.print(f"    [red]✗[/red] Installation failed")
        console.print(f"    [dim]{install.stderr[:300]}[/dim]")
        return False

    # step 4: start the service
    console.print("\n  [bold][4/4][/bold] Starting DCGM service...")

    await run_command("systemctl enable nvidia-dcgm 2>/dev/null", timeout=10)
    svc = await run_command("systemctl start nvidia-dcgm", timeout=15)

    if svc.success:
        console.print("    [green]✓[/green] nvidia-dcgm service running")
    else:
        # nv-hostengine might be the service name on some setups
        await run_command("systemctl enable dcgm 2>/dev/null", timeout=10)
        svc2 = await run_command("systemctl start dcgm 2>/dev/null", timeout=15)
        if svc2.success:
            console.print("    [green]✓[/green] dcgm service running")
        else:
            console.print("    [yellow]⚠[/yellow] Service didn't start, but package is installed")
            console.print("    [dim]Try: nv-hostengine[/dim]")

    # verify
    verify = await run_command("dcgmi discovery -l", timeout=15)
    if verify.success:
        console.print(f"\n  [green bold]DCGM installed successfully![/green bold]")
        gpu_count = verify.stdout.count("GPU ID:")
        if gpu_count:
            console.print(f"  [dim]Found {gpu_count} GPU(s)[/dim]")
        console.print(
            "  [dim]Run 'lit-diag run --all' to include DCGM diagnostics[/dim]\n"
        )
        return True
    else:
        console.print("\n  [yellow]DCGM installed but dcgmi not responding yet.[/yellow]")
        console.print("  [dim]Try: sudo nv-hostengine && dcgmi discovery -l[/dim]\n")
        return True
