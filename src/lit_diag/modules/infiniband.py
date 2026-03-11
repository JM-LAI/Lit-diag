"""InfiniBand diagnostics -- port state, error counters, link health."""

from __future__ import annotations

import os
import re
from typing import Any

from lit_diag.modules.base import BaseDiagnosticModule, Finding, ModuleResult, Severity
from lit_diag.utils.commands import run_command
from lit_diag.engine.module_loader import register_module

# error counters that indicate real problems (not just informational)
CRITICAL_ERROR_COUNTERS = {
    "symbol_error",
    "link_error_recovery",
    "VL15_dropped",
    "port_rcv_errors",
    "port_rcv_remote_physical_errors",
    "port_rcv_constraint_errors",
    "port_xmit_constraint_errors",
    "local_link_integrity_errors",
    "excessive_buffer_overrun_errors",
}

# thresholds for error counter severity
ERROR_WARN_THRESHOLD = 1
ERROR_CRIT_THRESHOLD = 100


def _safe_int(value: str, default: int = 0) -> int:
    cleaned = value.strip()
    try:
        return int(cleaned)
    except (ValueError, AttributeError):
        return default


async def _read_sysfs(path: str) -> str:
    """Read a sysfs file via cat. Returns empty string on failure."""
    result = await run_command(f"cat {path}", timeout=5.0)
    if result.success:
        return result.stdout.strip()
    return ""


@register_module
class InfiniBandModule(BaseDiagnosticModule):
    name = "infiniband"
    display_name = "InfiniBand"
    requires_root = False
    optional_tools = ["ibstat", "perfquery"]

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        ports: list[dict[str, Any]] = []
        data: dict[str, Any] = {"ports": ports}

        ib_base = "/sys/class/infiniband"
        if not os.path.isdir(ib_base):
            findings.append(Finding(
                code="ib_not_present",
                severity=Severity.DEGRADED,
                summary="No InfiniBand devices found",
                explanation=(
                    "The /sys/class/infiniband/ directory does not exist, "
                    "meaning no InfiniBand HCAs are detected by the kernel. "
                    "This may be expected if the node doesn't have IB hardware."
                ),
                client_action="If this node should have InfiniBand, contact support.",
                engineer_action="Check lspci for Mellanox/NVIDIA HCAs. Verify drivers loaded (mlx5_core).",
            ))
            return ModuleResult(
                module_name=self.name,
                findings=findings,
                data=data,
            )

        # enumerate HCAs
        try:
            cas = sorted(os.listdir(ib_base))
        except OSError:
            cas = []

        if not cas:
            findings.append(Finding(
                code="ib_not_present",
                severity=Severity.DEGRADED,
                summary="No InfiniBand devices found",
                explanation=(
                    "The /sys/class/infiniband/ directory exists but is empty. "
                    "IB drivers may be loaded but no devices are detected."
                ),
                client_action="If this node should have InfiniBand, contact support.",
                engineer_action="Check dmesg for mlx5 errors. Try modprobe -r mlx5_core && modprobe mlx5_core.",
            ))
            return ModuleResult(
                module_name=self.name,
                findings=findings,
                data=data,
            )

        for ca in cas:
            ca_path = os.path.join(ib_base, ca)
            fw_ver = await _read_sysfs(os.path.join(ca_path, "fw_ver"))

            ports_dir = os.path.join(ca_path, "ports")
            if not os.path.isdir(ports_dir):
                continue

            try:
                port_nums = sorted(os.listdir(ports_dir))
            except OSError:
                continue

            for port_num in port_nums:
                port_path = os.path.join(ports_dir, port_num)
                if not os.path.isdir(port_path):
                    continue

                state = await _read_sysfs(os.path.join(port_path, "state"))
                phys_state = await _read_sysfs(os.path.join(port_path, "phys_state"))
                link_layer = await _read_sysfs(os.path.join(port_path, "link_layer"))
                rate = await _read_sysfs(os.path.join(port_path, "rate"))

                # error counters
                counters_path = os.path.join(port_path, "counters")
                error_counters: dict[str, int] = {}
                link_downed = 0
                total_errors = 0

                if os.path.isdir(counters_path):
                    try:
                        counter_files = os.listdir(counters_path)
                    except OSError:
                        counter_files = []

                    for cf in counter_files:
                        val_str = await _read_sysfs(
                            os.path.join(counters_path, cf)
                        )
                        val = _safe_int(val_str)
                        if cf == "link_downed":
                            link_downed = val
                        if val > 0 and cf in CRITICAL_ERROR_COUNTERS:
                            error_counters[cf] = val
                            total_errors += val

                port_label = f"{ca}/{port_num}"
                port_entry = {
                    "device": ca,
                    "port": port_num,
                    "state": state,
                    "phys_state": phys_state,
                    "link_layer": link_layer,
                    "rate": rate,
                    "firmware": fw_ver,
                    "errors": error_counters,
                    "link_downed": link_downed,
                }
                ports.append(port_entry)

                # findings: link state
                # only flag IB ports as critical -- Ethernet ports being
                # down is expected if they're not cabled for RoCE
                is_ib = "infiniband" in link_layer.lower()
                if "ACTIVE" not in state.upper() and is_ib:
                    findings.append(Finding(
                        code="ib_link_down",
                        severity=Severity.CRITICAL,
                        summary=f"InfiniBand port {port_label} is down",
                        explanation=(
                            f"Port {port_label} state is '{state}' (expected '4: ACTIVE'). "
                            f"Physical state: '{phys_state}'. Network connectivity "
                            "on this port is lost."
                        ),
                        client_action="Contact support about network connectivity on this node.",
                        engineer_action=(
                            f"Check cable on {port_label}. Verify switch port is up. "
                            "Run ibdiagnet if available. Check dmesg for mlx5 errors."
                        ),
                        detail={
                            "device": ca,
                            "port": port_num,
                            "state": state,
                            "phys_state": phys_state,
                        },
                    ))

                # findings: link flapping (IB ports only)
                if link_downed > 0 and is_ib:
                    findings.append(Finding(
                        code="ib_link_flapping",
                        severity=Severity.WARNING,
                        summary=f"InfiniBand port {port_label} has flapped {link_downed} time(s)",
                        explanation=(
                            f"Port {port_label} link_downed counter is {link_downed}, "
                            "meaning the link has gone down and come back up. This "
                            "indicates intermittent connectivity issues."
                        ),
                        client_action="Contact support about intermittent network issues.",
                        engineer_action=(
                            f"Check cable quality on {port_label}. Inspect switch port. "
                            "Look for pattern in timing via SM logs."
                        ),
                        detail={
                            "device": ca,
                            "port": port_num,
                            "link_downed": link_downed,
                        },
                    ))

                # findings: error counters
                if total_errors > 0:
                    if total_errors >= ERROR_CRIT_THRESHOLD:
                        sev = Severity.CRITICAL
                        level = "critically elevated"
                    else:
                        sev = Severity.WARNING
                        level = "elevated"

                    top_errors = sorted(
                        error_counters.items(), key=lambda x: x[1], reverse=True
                    )[:5]
                    err_detail = ", ".join(
                        f"{k}={v}" for k, v in top_errors
                    )

                    findings.append(Finding(
                        code="ib_errors",
                        severity=sev,
                        summary=(
                            f"InfiniBand error counters {level} on {port_label} "
                            f"({total_errors} total)"
                        ),
                        explanation=(
                            f"Port {port_label} has {total_errors} errors across "
                            f"monitored counters: {err_detail}. These indicate "
                            "packet errors on the network fabric."
                        ),
                        client_action="Contact support about network quality on this node.",
                        engineer_action=(
                            f"Check cable integrity on {port_label}. Run perfquery "
                            "for live counter rates. Consider clearing counters and "
                            "re-monitoring: perfquery -x -r"
                        ),
                        detail={
                            "device": ca,
                            "port": port_num,
                            "total_errors": total_errors,
                            "counters": error_counters,
                        },
                    ))

        # -- ibstat supplemental info (if available) --
        ibstat_result = await run_command("ibstat", timeout=10.0)
        if ibstat_result.success and ibstat_result.stdout:
            data["ibstat_raw"] = ibstat_result.stdout

        # -- perfquery supplemental info (if available) --
        perfquery_result = await run_command("perfquery", timeout=10.0)
        if perfquery_result.success and perfquery_result.stdout:
            data["perfquery_raw"] = perfquery_result.stdout

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data=data,
        )
