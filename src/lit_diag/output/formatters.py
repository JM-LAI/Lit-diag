"""Output formatters -- console (client/staff) and JSON."""

from __future__ import annotations

import json
import os
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

    # degraded modules with no findings -- e.g. missing required tool
    for name in MODULE_DISPLAY_ORDER:
        if name not in report.modules:
            continue
        result = report.modules[name]
        if result.findings:
            continue
        if result.status == Severity.OK:
            continue
        friendly = MODULE_FRIENDLY_NAMES.get(name, name)
        err = result.error_message or f"Module {friendly} returned status: {result.status}"
        has_findings = True
        label = FINDING_LABELS.get(result.status, "")
        console.print(f"{label} [bold yellow]{friendly}: {err}[/bold yellow]")
        console.print(f"          [white]This check could not complete fully.[/white]")
        console.print()
        console.print("          [purple4]┌─ What to do ──────────────────────────────────────[/purple4]")
        console.print(f"          [purple4]│[/purple4]  Contact support and share this report.")
        console.print("          [purple4]└──────────────────────────────────────────────────[/purple4]")
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
    console.print("  [bold purple4]══ Staff / Engineer data (use --json -o file for full dump) ══[/bold purple4]")
    console.print()
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
                mem_used = _safe_float(dev.get('memory_used'))
                mem_total = _safe_float(dev.get('memory_total'))
                util = dev.get('utilization', 0)
                mem_pct = (mem_used / mem_total * 100) if mem_total else 0
                ecc_sram = dev.get('ecc_sram_uncorrectable', 0)
                ecc_dram_unc = dev.get('ecc_dram_uncorrectable', 0)
                ecc_dram_corr = dev.get('ecc_dram_correctable', 0)

                power_instant = _safe_float(dev.get("power_draw_instant"))
                instant_str = f"  Instant: {power_instant:.0f}W" if power_instant > 0 else ""
                line = (
                    f"    [bold]GPU {idx}[/bold]: [white]{gpu_name}[/white]  "
                    f"{color_gpu_temp(temp)}  "
                    f"{color_power(power_draw, power_limit)}/{power_limit}W{instant_str}  "
                    f"[dim]{clocks} MHz[/dim]  "
                    f"[dim]PCIe Gen{pcie_gen} x{pcie_w}[/dim]"
                )
                console.print(line)
                # second line: memory, utilization, ECC, UUID (staff troubleshooting)
                mem_str = f"{mem_used:.0f}/{mem_total:.0f} MiB ({mem_pct:.0f}%)" if mem_total else "—"
                util_str = f"{util}%" if util is not None else "—"
                ecc_parts = []
                if ecc_sram > 0:
                    ecc_parts.append(f"sram_unc={color_errors(ecc_sram)}")
                if ecc_dram_unc > 0:
                    ecc_parts.append(f"dram_unc={color_errors(ecc_dram_unc)}")
                if ecc_dram_corr > 0:
                    ecc_parts.append(f"dram_corr={color_errors(ecc_dram_corr)}")
                ecc_enabled = dev.get("ecc_enabled")
                if ecc_enabled is False:
                    ecc_tag = "[yellow]ECC OFF[/yellow]"
                elif ecc_parts:
                    ecc_tag = "  ".join(ecc_parts)
                else:
                    ecc_tag = "[green]ECC 0[/green]"
                ecc_str = ecc_tag
                uuid_val = dev.get("uuid", "")
                short_uuid = f"  UUID: …{uuid_val[-12:]}" if uuid_val and len(uuid_val) >= 12 else ""
                serial_val = dev.get("serial", "")
                serial_str = f"  Serial: {serial_val}" if serial_val else ""
                console.print(
                    f"      [dim]Memory: {mem_str}  Util: {util_str}  ECC: {ecc_str}{short_uuid}{serial_str}[/dim]"
                )
                # third line: HBM temp, throttle, retired pages, NUMA (staff deep-dive)
                extra_parts = []
                temp_mem = _safe_float(dev.get("temp_memory"))
                if temp_mem > 0:
                    extra_parts.append(f"HBM: {color_gpu_temp(temp_mem)}")
                throttle = dev.get("throttle_reasons", "")
                if throttle and "idle" not in throttle.lower() and "none" not in throttle.lower() and throttle != "0x0000000000000000":
                    extra_parts.append(f"Throttle: [yellow]{throttle}[/yellow]")
                ret_sbe = dev.get("retired_pages_sbe", 0)
                ret_dbe = dev.get("retired_pages_dbe", 0)
                remap_corr = dev.get("remap_correctable", 0)
                remap_unc = dev.get("remap_uncorrectable", 0)
                if ret_sbe or ret_dbe or remap_corr or remap_unc:
                    ret_str = f"Retired: sbe={ret_sbe} dbe={color_errors(ret_dbe)}"
                    if remap_corr or remap_unc:
                        ret_str += f"  Remap: corr={remap_corr} unc={color_errors(remap_unc)}"
                    extra_parts.append(ret_str)
                numa = dev.get("numa_node")
                if numa is not None:
                    extra_parts.append(f"NUMA: {numa}")
                if extra_parts:
                    console.print(f"      [dim]{' | '.join(extra_parts)}[/dim]")
            if data.get("processes"):
                procs = data["processes"]
                unique_pids = list(dict.fromkeys(p.get('pid', '?') for p in procs))
                if len(unique_pids) == 1:
                    pid_str = f"PID {unique_pids[0]} on all {len(procs)} GPUs"
                else:
                    shown = [str(pid) for pid in unique_pids[:8]]
                    suffix = f" ... +{len(unique_pids) - 8} more" if len(unique_pids) > 8 else ""
                    pid_str = f"{', '.join(shown)}{suffix}"
                console.print(
                    f"    [dim]Processes on GPU: {len(procs)} ({pid_str})[/dim]"
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
            iommu = data.get("iommu_enabled")
            if iommu is not None:
                console.print(
                    f"    [dim]IOMMU:[/dim] "
                    f"{'[green]enabled[/green]' if iommu else '[yellow]disabled[/yellow]'}"
                )
            for dev in data["devices"]:
                bdf = dev.get('bdf', '?')
                dev_name = dev.get('name', '?')
                # truncate long names so staff still see BDF and link info
                name_short = (dev_name[:44] + "…") if len(dev_name) > 45 else dev_name
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

                replay = dev.get("replay_count", 0)
                aer_str = (
                    f"AER: {color_errors(correctable)} corr, "
                    f"{color_errors(fatal, warn=1, crit=1)} fatal"
                )
                replay_str = f"  replay: {color_errors(replay)}" if replay > 0 else ""
                console.print(
                    f"    [dim]{bdf}[/dim] [white]{name_short}[/white]"
                )
                console.print(
                    f"      {gen_str} {width_str}  {aer_str}{replay_str}"
                )

        elif name == "kernel_logs" and "entries" in data:
            entries = data["entries"]
            total = len(entries)
            # last error age
            last_err = data.get("last_error_ts", "")
            if last_err:
                console.print(f"    [dim]Last error: [yellow]{last_err}[/yellow][/dim]")
            elif total > 0:
                console.print(f"    [dim]Last error: [green]none (all informational)[/green][/dim]")
            # one-line summaries for staff (XID counts, category breakdown)
            xid_summary = data.get("xid_summary") or {}
            if xid_summary and isinstance(xid_summary, dict):
                xid_parts = [f"{code}: {cnt}" for code, cnt in sorted(xid_summary.items(), key=lambda x: (-x[1], x[0]))]
                if xid_parts:
                    console.print(f"    [dim]XID counts: {', '.join(xid_parts)}[/dim]")
            categories = data.get("categories") or {}
            if categories and isinstance(categories, dict):
                cat_parts = [f"{cat}: {n}" for cat, n in sorted(categories.items(), key=lambda x: -x[1]) if n > 0]
                if cat_parts:
                    console.print(f"    [dim]By category: {', '.join(cat_parts)}[/dim]")
            for entry in entries[:25]:
                ts = entry.get('timestamp', '?')
                msg = entry.get('message', '?')
                cat = entry.get('category', '')
                tag = f"[{cat}] " if cat else ""
                if any(kw in cat.lower() for kw in ('xid', 'lockup', 'sxid', 'io_error')):
                    console.print(f"    [red]{tag}[{ts}] {msg}[/red]")
                elif any(kw in cat.lower() for kw in ('oom', 'nvrm', 'hung')):
                    console.print(f"    [yellow]{tag}[{ts}] {msg}[/yellow]")
                else:
                    console.print(f"    [dim]{tag}[{ts}] {msg}[/dim]")
            if total > 25:
                console.print(
                    f"    [dim]... and {total - 25} more (use --json -o report.json for full log)[/dim]"
                )
            if total == 0:
                console.print("    [dim]No parsed entries (run with sudo for dmesg/journalctl)[/dim]")

        elif name == "infiniband" and "ports" in data:
            # group by link layer so IB fabric vs Ethernet mgmt are visually separate
            ib_ports = [p for p in data["ports"] if "infiniband" in p.get("link_layer", "").lower()]
            eth_ports = [p for p in data["ports"] if "infiniband" not in p.get("link_layer", "").lower()]

            def _print_ib_port(port):
                device = port.get('device', '?')
                port_num = port.get('port', '?')
                state = port.get('state', '?')
                phys_state = port.get('phys_state', '')
                layer = port.get('link_layer', '?')
                rate = port.get('rate', '?')
                errs = port.get('errors', {}) or {}
                ld = port.get('link_downed', 0)
                fw = port.get('firmware', '')
                state_colored = color_state(state)
                phys_str = f" / {phys_state}" if phys_state else ""
                ld_str = color_errors(ld) if ld > 0 else f"[green]{ld}[/green]"
                err_count = sum(errs.values()) if isinstance(errs, dict) else 0
                console.print(
                    f"    [bold]{device}[/bold]/port {port_num}: "
                    f"{state_colored}{phys_str}  "
                    f"[dim]{layer}[/dim] {rate}  "
                    f"errors: {color_errors(err_count)}  "
                    f"link_downed: {ld_str}"
                )
                if fw:
                    console.print(f"      [dim]FW: {fw}[/dim]")
                if errs and isinstance(errs, dict):
                    err_detail = "  ".join(f"{k}={color_errors(v)}" for k, v in sorted(errs.items()))
                    console.print(f"      [dim]Counters: {err_detail}[/dim]")

            if ib_ports:
                console.print("    [dim]─ IB Fabric ─[/dim]")
                for port in ib_ports:
                    _print_ib_port(port)
            if eth_ports:
                console.print("    [dim]─ Ethernet / Management ─[/dim]")
                for port in eth_ports:
                    _print_ib_port(port)

        elif name == "thermal" and "sensors" in data:
            for sensor in data["sensors"]:
                sname = sensor.get('name', '?')
                val = _safe_float(sensor.get('value'))
                unit = sensor.get('unit', '')
                name_lower = sname.lower()
                # skip noisy/useless IPMI sensors
                if any(skip in name_lower for skip in (
                    'overt', 'redundancy', 'ps redundancy',
                )):
                    continue
                if name_lower == 'status' and val == 0.0:
                    continue

                if unit == "C":
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
            if "sel_entries" in data and data["sel_entries"]:
                sel = data["sel_entries"]
                n = len(sel)
                last_str = ""
                if sel:
                    last = sel[0]
                    if isinstance(last, dict):
                        last_str = f" (last: {last.get('timestamp', '?')} {last.get('message', '')[:40]}...)"
                    else:
                        last_str = f" (last: {str(sel[0])[:50]}...)"
                console.print(f"    [dim]IPMI SEL: {n} entries{last_str}[/dim]")

        elif name == "storage":
            if not data.get("devices") and not os.geteuid() == 0:
                console.print(f"    {NEEDS_ROOT_HINT['storage']}")
            if "devices" in data and data["devices"]:
                console.print("    [dim]NVMe Drives:[/dim]")
                for dev in data["devices"]:
                    device = dev.get('device', '?')
                    model = dev.get('model', '')
                    size = dev.get('size', '?')
                    health = dev.get('health', '?')
                    wear = _safe_float(dev.get('wear_pct'))
                    spare = _safe_float(dev.get('spare_pct'))
                    temp = _safe_float(dev.get('temp'))

                    model_str = f" [dim]({model})[/dim]" if model else ""
                    media_err = dev.get("media_errors", 0)
                    err_log = dev.get("error_log_entries", 0)
                    media_str = f"  Media: {color_errors(media_err)} ErrLog: {color_errors(err_log)}" if "media_errors" in dev or "error_log_entries" in dev else ""
                    console.print(
                        f"      [bold]{device}[/bold]{model_str}: {size}  "
                        f"{color_health(health)}  "
                        f"wear: {color_wear(wear)}  "
                        f"spare: {color_pct(spare, invert=True)}  "
                        f"temp: {color_temp(temp, warn=60, crit=70)}{media_str}"
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
                    console.print(
                        f"    Persistence: [yellow]{pm}[/yellow]  "
                        f"[dim]→ Fix: sudo nvidia-smi -pm 1[/dim]"
                    )
            if "dkms_status" in data and data["dkms_status"]:
                console.print(f"    DKMS: [dim]{data['dkms_status']}[/dim]")
            if "fabricmanager_active" in data:
                fm = data["fabricmanager_active"]
                if fm:
                    console.print("    Fabric Manager: [green]active[/green]")
                else:
                    console.print(
                        "    Fabric Manager: [yellow]not running[/yellow]  "
                        "[dim]→ Fix: sudo systemctl start nvidia-fabricmanager[/dim]"
                    )

        elif name == "system":
            for key in ["cpu", "cpu_cores", "cpu_sockets", "architecture", "kernel", "os",
                        "numa_nodes", "memory_total", "uptime",
                        "load_average", "hostname", "primary_ip"]:
                if key not in data:
                    continue
                val = data[key]
                if key == "cpu_cores" and data.get("cpu_sockets") not in (None, 0, ""):
                    # show "96 (2 sockets)" in cpu_sockets row
                    continue
                if key == "cpu_sockets":
                    cores = data.get("cpu_cores")
                    sockets = val
                    if cores is not None and sockets not in (None, ""):
                        console.print(f"    [dim]Cpu Cores:[/dim] [white]{cores} ({sockets} sockets)[/white]")
                    elif sockets not in (None, ""):
                        console.print(f"    [dim]Cpu Sockets:[/dim] [white]{sockets}[/white]")
                    continue
                label = key.replace("_", " ").title()
                console.print(f"    [dim]{label}:[/dim] [white]{val}[/white]")
            # memory available %
            mem_avail = data.get("memory_available_pct")
            if mem_avail is not None:
                avail_gb = ""
                # rough calc from total and pct
                mem_total_str = data.get("memory_total", "")
                if mem_total_str:
                    try:
                        total_gb = float(mem_total_str.replace(" GB", ""))
                        avail_gb = f" ({total_gb * mem_avail / 100:.0f} GB free)"
                    except (ValueError, TypeError):
                        pass
                if mem_avail < 10:
                    console.print(f"    [dim]Memory Available:[/dim] [red]{mem_avail}%{avail_gb}[/red]")
                elif mem_avail < 30:
                    console.print(f"    [dim]Memory Available:[/dim] [yellow]{mem_avail}%{avail_gb}[/yellow]")
                else:
                    console.print(f"    [dim]Memory Available:[/dim] [green]{mem_avail}%{avail_gb}[/green]")

            # kernel taint decoded
            taint = data.get("kernel_tainted")
            if taint is not None and taint != 0:
                decoded = data.get("kernel_tainted_decoded", str(taint))
                console.print(f"    [dim]Kernel Tainted:[/dim] [yellow]{taint}[/yellow] [dim]({decoded})[/dim]")

            # NTP / time sync
            tsync = data.get("time_sync")
            if tsync and isinstance(tsync, dict):
                synced = tsync.get("ntp_synced")
                enabled = tsync.get("ntp_enabled")
                offset = tsync.get("offset", "")
                stratum = tsync.get("stratum", "")
                ntp_parts = []
                if synced is True:
                    ntp_parts.append("[green]NTP synchronized[/green]")
                elif synced is False:
                    ntp_parts.append("[red]NTP NOT synchronized[/red]")
                elif enabled is True:
                    ntp_parts.append("[yellow]NTP enabled, sync status unknown[/yellow]")
                else:
                    ntp_parts.append("[yellow]unknown[/yellow]")
                if stratum:
                    ntp_parts.append(f"stratum {stratum}")
                if offset:
                    ntp_parts.append(f"offset {offset}")
                console.print(f"    [dim]Time Sync:[/dim] {' '.join(ntp_parts)}")

            # Reboot history
            reboot_hist = data.get("reboot_history")
            if reboot_hist and isinstance(reboot_hist, list):
                console.print("    [dim]Reboot history:[/dim]")
                for line in reboot_hist[:5]:
                    console.print(f"      [dim]{line}[/dim]")

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

    # ticket one-liner (staff only): copy-paste for Jira/support
    _print_ticket_oneliner(report, console)


def _print_ticket_oneliner(report: DiagnosticReport, console: Console) -> None:
    """Print a single line staff can copy into tickets: hostname | GPUs | driver | kernel | timestamp."""
    parts = [report.hostname or "?"]
    gpu_mod = report.modules.get("gpu")
    if gpu_mod and gpu_mod.data and gpu_mod.data.get("devices"):
        devs = gpu_mod.data["devices"]
        n = len(devs)
        model = devs[0].get("name", "GPU") if devs else "GPU"
        parts.append(f"{n}x {model}")
    drv_mod = report.modules.get("driver")
    if drv_mod and drv_mod.data:
        dv = drv_mod.data.get("driver_version", "")
        if dv:
            parts.append(f"driver {dv}")
    sys_mod = report.modules.get("system")
    if sys_mod and sys_mod.data:
        kv = sys_mod.data.get("kernel", "")
        if kv:
            parts.append(kv)
    if report.timestamp:
        # e.g. 2026-03-17T18:02:46 -> keep first 19 chars for readability
        ts = report.timestamp[:19].replace("T", " ")
        parts.append(ts)
    console.print("  [purple4]Copy for ticket:[/purple4] [dim]" + " | ".join(parts) + "[/dim]")
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
    if role == UserRole.CLIENT:
        console.print(
            "  [dim]If you need help, save this report: "
            "[bold]lit-diag run --all --json -o report.json[/bold] "
            "and send it to your support team.[/dim]\n"
        )


def report_to_json(report: DiagnosticReport) -> str:
    """Serialize report to JSON string."""
    return report.model_dump_json(indent=2)


def save_report_json(report: DiagnosticReport, path: str) -> None:
    """Save report to a JSON file."""
    with open(path, "w") as f:
        f.write(report_to_json(report))
