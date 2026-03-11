"""Output formatters -- console (client/staff) and JSON."""

from __future__ import annotations

import json
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from lit_diag.engine.config import UserRole
from lit_diag.modules.base import DiagnosticReport, Finding, ModuleResult, Severity
from lit_diag.utils.colors import (
    BANNER_STYLES,
    FINDING_LABELS,
    STATUS_ICONS,
    STATUS_STYLES,
    color_errors,
    color_gpu_temp,
    color_health,
    color_pct,
    color_power,
    color_rpm,
    color_state,
    color_temp,
    color_wear,
)


def _is_data_empty(data: dict) -> bool:
    """Check if a data dict is effectively empty (all lists are empty, etc)."""
    if not data:
        return True
    for val in data.values():
        if isinstance(val, list) and len(val) > 0:
            return False
        if isinstance(val, dict) and len(val) > 0:
            return False
        if isinstance(val, str) and val:
            return False
        if isinstance(val, (int, float)) and val:
            return False
        if isinstance(val, bool) and val:
            return False
    return True


MODULE_DISPLAY_ORDER = [
    "gpu", "nvlink", "pcie", "kernel_logs", "storage",
    "thermal", "infiniband", "cuda_tests", "driver", "system",
]

MODULE_FRIENDLY_NAMES = {
    "gpu": "GPU Health",
    "nvlink": "NVLink",
    "pcie": "PCIe Bus",
    "kernel_logs": "Kernel Logs",
    "storage": "Storage",
    "thermal": "Thermal / Power",
    "infiniband": "InfiniBand",
    "cuda_tests": "CUDA Tests",
    "driver": "NVIDIA Driver",
    "system": "System Info",
}


def print_banner(report: DiagnosticReport, console: Console) -> None:
    """Print the overall health banner -- first thing everyone sees."""
    status = report.overall_status
    style = BANNER_STYLES.get(status, "white")
    status_text = status.value.upper()
    icon = STATUS_ICONS.get(status, "")

    issue_count = len(report.degraded_codes)
    if issue_count > 0:
        subtitle = f"{issue_count} {'issue' if issue_count == 1 else 'issues'} found"
    else:
        subtitle = "All checks passed"

    console.print()
    console.print(
        Panel(
            f"{icon} [bold]NODE HEALTH: {status_text}[/bold] -- {subtitle}\n"
            f"  {report.hostname} | {report.timestamp[:19]} UTC\n"
            f"  [dim]Lit-Diag by Lightning AI[/dim]",
            border_style=style,
            padding=(0, 2),
        )
    )


def print_traffic_light(report: DiagnosticReport, console: Console) -> None:
    """Print the traffic light summary -- everyone sees this."""
    console.print()
    for name in MODULE_DISPLAY_ORDER:
        if name not in report.modules:
            continue
        result = report.modules[name]
        friendly = MODULE_FRIENDLY_NAMES.get(name, name)
        icon = STATUS_ICONS.get(result.status, " ")
        status_str = STATUS_STYLES.get(result.status, str(result.status))
        dots = "·" * (22 - len(friendly))
        console.print(f"  {icon} {friendly} [dim]{dots}[/dim] {status_str}")
    console.print()


def print_findings(
    report: DiagnosticReport,
    console: Console,
    role: UserRole = UserRole.CLIENT,
) -> None:
    """Print findings with plain English explanations.

    Clear visual separation between the problem (what's wrong)
    and the action (what to do about it) so clients don't panic.
    """
    has_findings = False

    for name in MODULE_DISPLAY_ORDER:
        if name not in report.modules:
            continue
        result = report.modules[name]
        if not result.findings:
            continue

        has_findings = True

        for finding in result.findings:
            if finding.severity == Severity.OK:
                continue

            label = FINDING_LABELS.get(finding.severity, "")

            # -- PROBLEM section --
            if finding.severity == Severity.CRITICAL:
                console.print(f"{label} [bold red]{finding.summary}[/bold red]")
            elif finding.severity == Severity.WARNING:
                console.print(f"{label} [bold yellow]{finding.summary}[/bold yellow]")
            elif finding.severity == Severity.ERROR:
                console.print(f"{label} [bold red]{finding.summary}[/bold red]")
            else:
                console.print(f"{label} [bold]{finding.summary}[/bold]")

            console.print(f"          [white]{finding.explanation}[/white]")
            console.print()

            # -- ACTION section (visually separated with box) --
            if finding.fix_command:
                console.print("          [purple4]┌─ What to do ──────────────────────────────────────[/purple4]")
                console.print(f"          [purple4]│[/purple4]  [green]Quick fix available[/green]")
                console.print(f"          [purple4]│[/purple4]  [bold]Run:[/bold]  sudo {finding.fix_command}")
                console.print(f"          [purple4]│[/purple4]  [bold]Impact:[/bold] {finding.fix_impact}")
                console.print("          [purple4]└──────────────────────────────────────────────────[/purple4]")
            else:
                console.print("          [purple4]┌─ What to do ──────────────────────────────────────[/purple4]")
                console.print(f"          [purple4]│[/purple4]  {finding.client_action}")
                console.print("          [purple4]└──────────────────────────────────────────────────[/purple4]")

            if role == UserRole.STAFF:
                if finding.engineer_action:
                    console.print(
                        f"          [medium_purple]→ Engineer:[/medium_purple] "
                        f"{finding.engineer_action}"
                    )
                if finding.detail:
                    console.print("          [dim]─── Detail ───[/dim]")
                    for key, val in finding.detail.items():
                        label_str = key.replace("_", " ").title()
                        console.print(f"            [dim]{label_str}:[/dim] {val}")

            console.print()

    if not has_findings:
        console.print(
            "  [bold green]✓ No issues detected. Everything looks healthy.[/bold green]"
        )
    console.print()


NEEDS_ROOT_HINT = {
    "kernel_logs": "[yellow]⚠[/yellow] Run with [bold]sudo[/bold] to see kernel log analysis (XID errors, OOM events, etc.)",
    "storage": "[yellow]⚠[/yellow] Run with [bold]sudo[/bold] to see NVMe health, RAID status, and disk details",
    "thermal": "[yellow]⚠[/yellow] Run with [bold]sudo[/bold] to see CPU temps, fan speeds, and power supply status",
}


def _safe_float(val, default=0.0) -> float:
    """Safely convert to float."""
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def print_staff_data(report: DiagnosticReport, console: Console) -> None:
    """Print full device inventories and data dumps for staff mode."""
    for name in MODULE_DISPLAY_ORDER:
        if name not in report.modules:
            continue
        result = report.modules[name]

        friendly = MODULE_FRIENDLY_NAMES.get(name, name)
        icon = STATUS_ICONS.get(result.status, " ")
        console.print(f"  [bold purple4]── {icon} {friendly} ──[/bold purple4]")

        if not result.data or _is_data_empty(result.data):
            if name in NEEDS_ROOT_HINT:
                console.print(f"    {NEEDS_ROOT_HINT[name]}")
            elif result.error_message:
                console.print(f"    [dim]{result.error_message}[/dim]")
            else:
                console.print("    [dim]No data collected[/dim]")
            console.print()
            continue

        data = result.data

        if name == "gpu" and "devices" in data:
            for dev in data["devices"]:
                idx = dev.get('index', '?')
                gpu_name = dev.get('name', 'Unknown')
                temp = _safe_float(dev.get('temp'))
                power_draw = _safe_float(dev.get('power_draw'))
                power_limit = _safe_float(dev.get('power_limit'))
                clocks = dev.get('clocks_sm', '?')
                pcie_gen = dev.get('pcie_gen', '?')
                pcie_w = dev.get('pcie_width', '?')

                console.print(
                    f"    [bold]GPU {idx}[/bold]: [white]{gpu_name}[/white]  "
                    f"{color_gpu_temp(temp)}  "
                    f"{color_power(power_draw, power_limit)}/{power_limit}W  "
                    f"[dim]{clocks} MHz[/dim]  "
                    f"[dim]PCIe Gen{pcie_gen} x{pcie_w}[/dim]"
                )

        elif name == "nvlink" and "links" in data:
            for link in data["links"]:
                gpu_idx = link.get("gpu", "?")
                ver = link.get("version", "?")
                lc = link.get("link_count", "?")
                inactive = link.get("inactive", 0)
                if inactive == 0:
                    state_str = f"[green]all active[/green]"
                else:
                    state_str = f"[red]{inactive} inactive[/red]"
                console.print(
                    f"    [bold]GPU {gpu_idx}[/bold]: "
                    f"[dim]NVLink v{ver},[/dim] {lc} links, {state_str}"
                )
            if "error_counters" in data:
                for gpu_idx, counts in data["error_counters"].items():
                    total = sum(counts.values())
                    if total > 0:
                        console.print(
                            f"    [bold]GPU {gpu_idx}[/bold] errors: "
                            f"replay={color_errors(counts.get('replay', 0))}, "
                            f"recovery={color_errors(counts.get('recovery', 0))}, "
                            f"CRC={color_errors(counts.get('crc_flit', 0) + counts.get('crc_data', 0))}"
                        )
            if "topology" in data and data["topology"]:
                console.print(f"    [dim]Topology matrix available (use --json for full data)[/dim]")

        elif name == "pcie" and "devices" in data:
            for dev in data["devices"]:
                bdf = dev.get('bdf', '?')
                dev_name = dev.get('name', '?')
                cur_gen = dev.get('current_gen', 0)
                cap_gen = dev.get('capable_gen', 0)
                cur_w = dev.get('current_width', 0)
                cap_w = dev.get('capable_width', 0)
                correctable = dev.get('correctable', 0)
                fatal = dev.get('fatal', 0)

                # color gen/width: green if at max, yellow if degraded
                if cur_gen > 0 and cap_gen > 0 and cur_gen < cap_gen:
                    gen_str = f"[yellow]Gen{cur_gen}[/yellow][dim]/Gen{cap_gen}[/dim]"
                elif cur_gen > 0:
                    gen_str = f"[green]Gen{cur_gen}[/green]"
                else:
                    gen_str = f"[dim]Gen?[/dim]"

                if cur_w > 0 and cap_w > 0 and cur_w < cap_w:
                    width_str = f"[yellow]x{cur_w}[/yellow][dim]/x{cap_w}[/dim]"
                elif cur_w > 0:
                    width_str = f"[green]x{cur_w}[/green]"
                else:
                    width_str = f"[dim]x?[/dim]"

                aer_str = (
                    f"AER: {color_errors(correctable)} corr, "
                    f"{color_errors(fatal, warn=1, crit=1)} fatal"
                )
                console.print(
                    f"    [dim]{bdf}[/dim] {gen_str} {width_str}  {aer_str}"
                )

        elif name == "kernel_logs" and "entries" in data:
            for entry in data["entries"][:20]:
                ts = entry.get('timestamp', '?')
                msg = entry.get('message', '?')
                cat = entry.get('category', '')
                # color by severity of the log entry
                if any(kw in cat.lower() for kw in ('xid', 'lockup', 'sxid', 'io_error')):
                    console.print(f"    [red][{ts}] {msg}[/red]")
                elif any(kw in cat.lower() for kw in ('oom', 'nvrm', 'hung')):
                    console.print(f"    [yellow][{ts}] {msg}[/yellow]")
                else:
                    console.print(f"    [dim][{ts}] {msg}[/dim]")
            total = len(data["entries"])
            if total > 20:
                console.print(f"    [dim]... and {total - 20} more entries[/dim]")

        elif name == "infiniband" and "ports" in data:
            for port in data["ports"]:
                device = port.get('device', '?')
                port_num = port.get('port', '?')
                state = port.get('state', '?')
                phys = port.get('phys_state', '?')
                layer = port.get('link_layer', '?')
                rate = port.get('rate', '?')
                errs = port.get('errors', {})
                ld = port.get('link_downed', 0)

                state_colored = color_state(state)
                ld_str = color_errors(ld) if ld > 0 else f"[green]{ld}[/green]"
                err_count = sum(errs.values()) if isinstance(errs, dict) else 0

                console.print(
                    f"    [bold]{device}[/bold]/port {port_num}: "
                    f"{state_colored}  "
                    f"[dim]{layer}[/dim] {rate}  "
                    f"errors: {color_errors(err_count)}  "
                    f"link_downed: {ld_str}"
                )

        elif name == "thermal" and "sensors" in data:
            for sensor in data["sensors"]:
                sname = sensor.get('name', '?')
                val = _safe_float(sensor.get('value'))
                unit = sensor.get('unit', '')

                if unit == "C":
                    name_lower = sname.lower()
                    if any(kw in name_lower for kw in ('inlet', 'ambient')):
                        val_str = color_temp(val, warn=35, crit=40)
                    elif 'gpu' in name_lower:
                        val_str = color_gpu_temp(val)
                    else:
                        val_str = color_temp(val, warn=85, crit=95)
                elif unit == "RPM":
                    val_str = color_rpm(val)
                else:
                    val_str = f"{val}{unit}"

                console.print(f"    [dim]{sname}:[/dim] {val_str}")

        elif name == "storage":
            if "devices" in data and data["devices"]:
                console.print("    [dim]NVMe Drives:[/dim]")
                for dev in data["devices"]:
                    device = dev.get('device', '?')
                    size = dev.get('size', '?')
                    health = dev.get('health', '?')
                    wear = _safe_float(dev.get('wear_pct'))
                    spare = _safe_float(dev.get('spare_pct'))
                    temp = _safe_float(dev.get('temp'))

                    console.print(
                        f"      [bold]{device}[/bold]: {size}  "
                        f"{color_health(health)}  "
                        f"wear: {color_wear(wear)}  "
                        f"spare: {color_pct(spare, invert=True)}  "
                        f"temp: {color_temp(temp, warn=60, crit=70)}"
                    )

            if "filesystems" in data and data["filesystems"]:
                console.print("    [dim]Disk Space:[/dim]")
                for fs in data["filesystems"]:
                    mp = fs.get("mountpoint", "?")
                    size = fs.get("size", "?")
                    avail = fs.get("available", "?")
                    pct = fs.get("use_pct", 0)
                    pct_str = color_pct(pct, warn=85, crit=95)
                    console.print(
                        f"      {mp}: {pct_str} used  "
                        f"[dim]({avail} free of {size})[/dim]"
                    )

            if "unused_drives" in data and data["unused_drives"]:
                console.print("    [yellow]Unused Drives:[/yellow]")
                for drv in data["unused_drives"]:
                    console.print(
                        f"      [yellow]{drv['device']}[/yellow]: "
                        f"{drv['size']} [dim](not mounted, not in LVM or RAID)[/dim]"
                    )

        elif name == "driver":
            dv = data.get('driver_version', '?')
            cv = data.get('cuda_version', '?')
            kv = data.get('kernel_version', '?')
            console.print(
                f"    Driver: [bold white]{dv}[/bold white]  "
                f"CUDA: [bold white]{cv}[/bold white]  "
                f"Kernel: [bold white]{kv}[/bold white]"
            )
            if "vbios" in data:
                console.print(f"    VBIOS: [white]{data['vbios']}[/white]")
            if "modules" in data:
                mods = data['modules']
                mod_strs = [f"[green]{m}[/green]" for m in mods]
                console.print(f"    Modules: {', '.join(mod_strs)}")
            if "persistence_mode" in data:
                pm = data['persistence_mode']
                if 'enabled' in str(pm).lower():
                    console.print(f"    Persistence: [green]{pm}[/green]")
                else:
                    console.print(f"    Persistence: [yellow]{pm}[/yellow]")

        elif name == "system":
            for key in ["cpu", "architecture", "kernel", "os",
                        "numa_nodes", "memory_total", "uptime",
                        "load_average", "hostname"]:
                if key in data:
                    label = key.replace("_", " ").title()
                    console.print(f"    [dim]{label}:[/dim] [white]{data[key]}[/white]")

        elif name == "cuda_tests":
            dcgm = data.get('dcgm_available', False)
            console.print(
                f"    DCGM: {'[green]available[/green]' if dcgm else '[yellow]not installed[/yellow]'}"
            )
            if "dcgm_tests" in data:
                for test in data["dcgm_tests"]:
                    tname = test.get("test_name", "?")
                    tres = test.get("result", "?")
                    if tres.lower() == "pass":
                        console.print(f"      [green]✓[/green] {tname}")
                    elif tres.lower() == "fail":
                        console.print(f"      [red]✗[/red] {tname}")
                    else:
                        console.print(f"      [dim]- {tname}: {tres}[/dim]")
            cdv = data.get('cuda_driver_version', '')
            crv = data.get('cuda_runtime_version', '')
            if cdv:
                console.print(f"    CUDA Driver: [white]{cdv}[/white]")
            if crv:
                console.print(f"    CUDA Runtime: [white]{crv}[/white]")

        else:
            for key, val in data.items():
                if isinstance(val, (list, dict)):
                    continue
                label = key.replace("_", " ").title()
                console.print(f"    [dim]{label}:[/dim] [white]{val}[/white]")

        console.print()


def print_report(
    report: DiagnosticReport,
    console: Console,
    role: UserRole = UserRole.CLIENT,
) -> None:
    """Print the full console report."""
    print_banner(report, console)
    print_traffic_light(report, console)
    print_findings(report, console, role)

    if role == UserRole.STAFF:
        print_staff_data(report, console)

    elapsed = report.duration_ms / 1000
    console.print(f"  [dim]Completed in {elapsed:.1f}s[/dim]\n")


def report_to_json(report: DiagnosticReport) -> str:
    """Serialize report to JSON string."""
    return report.model_dump_json(indent=2)


def save_report_json(report: DiagnosticReport, path: str) -> None:
    """Save report to a JSON file."""
    with open(path, "w") as f:
        f.write(report_to_json(report))
