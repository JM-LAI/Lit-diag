"""Kernel log analysis -- the biggest value-add module.

Parses dmesg/journalctl output for GPU errors, OOM kills, lockups,
I/O failures, and other actionable patterns that raw log tools miss.
"""

from __future__ import annotations

import re
from typing import Any

from lit_diag.modules.base import (
    BaseDiagnosticModule,
    Finding,
    ModuleResult,
    Severity,
)
from lit_diag.utils.commands import run_command
from lit_diag.engine.module_loader import register_module

# -- XID code reference -----------------------------------------------------------
# mapping covers the most common codes we see on GPU cluster nodes
XID_DESCRIPTIONS: dict[int, str] = {
    13: "Graphics Engine Exception",
    31: "GPU memory page fault",
    43: "GPU stopped processing",
    45: "Preemptive cleanup",
    48: "Double Bit ECC Error",
    61: "Internal micro-controller halt",
    62: "Internal micro-controller halt (non-fatal)",
    63: "ECC page retirement or row remapping recording event",
    64: "ECC page retirement or row remapping failure",
    69: "Graphics Engine class error",
    74: "NVLink Error",
    79: "GPU has fallen off the bus",
    92: "High single-bit ECC error rate",
    94: "Contained ECC error",
    95: "Uncontained ECC error",
}

# XIDs that warrant CRITICAL vs WARNING
_CRITICAL_XIDS = {48, 79, 95}
_WARNING_XIDS = {63, 64, 92, 94}

# -- Log patterns ------------------------------------------------------------------

_PATTERNS: dict[str, re.Pattern[str]] = {
    "xid": re.compile(
        r"NVRM: Xid \(PCI:([^)]+)\): (\d+)",
    ),
    "sxid": re.compile(
        r"nvidia-nvswitch.*SXid.*: (\d+)",
    ),
    "nvrm_error": re.compile(
        r"NVRM:.*[Ee]rror|NVRM:.*[Ff]ailed",
    ),
    "oom": re.compile(
        r"Out of memory: Killed process (\d+) \(([^)]+)\)",
    ),
    "hung_task": re.compile(
        r"blocked for more than \d+ seconds|hung_task_timeout",
    ),
    "soft_lockup": re.compile(
        r"soft lockup - CPU#\d+",
    ),
    "hard_lockup": re.compile(
        r"hard LOCKUP",
    ),
    "iommu_fault": re.compile(
        r"DMAR:.*fault|AMD-Vi:.*fault",
    ),
    "nvme_io_error": re.compile(
        r"I/O error.*nvme|nvme.*I/O error",
    ),
    "ro_remount": re.compile(
        r"Remounting filesystem read-only",
    ),
}

# map pattern categories to human labels
_CATEGORY_LABELS: dict[str, str] = {
    "xid": "NVIDIA XID Error",
    "sxid": "NVSwitch SXid Error",
    "nvrm_error": "NVRM Error",
    "oom": "Out of Memory Kill",
    "hung_task": "Hung Task",
    "soft_lockup": "Soft Lockup",
    "hard_lockup": "Hard Lockup",
    "iommu_fault": "IOMMU Fault",
    "nvme_io_error": "NVMe I/O Error",
    "ro_remount": "Read-Only Remount",
}


def _extract_timestamp(line: str) -> str:
    """Best-effort timestamp extraction from a dmesg line.

    Handles ISO format (dmesg --time-format iso) and the classic
    [seconds.usecs] format. Returns the raw string on failure.
    """
    # ISO: 2024-03-11T14:22:01,000000+00:00
    iso = re.match(r"(\d{4}-\d{2}-\d{2}T\S+)", line)
    if iso:
        return iso.group(1)
    # classic: [12345.678901]
    classic = re.match(r"\[\s*([\d.]+)\]", line)
    if classic:
        return classic.group(1)
    return ""


def _xid_severity(code: int) -> Severity:
    if code in _CRITICAL_XIDS:
        return Severity.CRITICAL
    if code in _WARNING_XIDS:
        return Severity.WARNING
    return Severity.WARNING


def _xid_engineer_action(code: int) -> str:
    """Tailored engineer guidance per XID code family."""
    actions: dict[int, str] = {
        13: (
            "Check GPU utilization and thermals. Run cuda_memtest. "
            "If recurring, RMA the GPU."
        ),
        31: (
            "Usually application-level. Verify CUDA code for illegal memory access. "
            "If persistent across applications, run memory diagnostics."
        ),
        43: (
            "GPU hung. Check for thermal throttling, power capping, or driver bugs. "
            "Collect a GPU coredump if possible."
        ),
        45: "Informational during cleanup. Usually safe to ignore unless repeated.",
        48: (
            "Double-bit ECC -- uncorrectable memory error. Schedule GPU replacement. "
            "Run nvidia-smi -q to check retired pages."
        ),
        61: (
            "GPU microcontroller halted. Likely fatal hardware failure. "
            "Collect nvidia-bug-report.sh and schedule RMA."
        ),
        62: "Non-fatal MC halt. Monitor frequency. If recurring, treat like XID 61.",
        63: (
            "ECC row remap recorded successfully. Monitor retired page count "
            "with nvidia-smi -q -d PAGE_RETIREMENT."
        ),
        64: (
            "Row remapping failed -- GPU memory bank exhausted. "
            "Schedule GPU replacement."
        ),
        69: (
            "Graphics engine class error. Check workload for shader bugs. "
            "If app-independent, likely hardware. Run cuda_memtest."
        ),
        74: (
            "NVLink error. Check NVLink cable seating and switch health. "
            "Run nvidia-smi nvlink -s to view error counters."
        ),
        79: (
            "GPU fell off the bus -- PCIe link lost. Check riser cable, "
            "PCIe slot, power cables. Inspect for thermal shutdown. "
            "This usually requires physical intervention."
        ),
        92: (
            "High SBE rate approaching retirement threshold. Monitor "
            "retired pages. Plan preemptive GPU replacement."
        ),
        94: (
            "Contained ECC error -- workload was affected but system is stable. "
            "Run nvidia-smi -q -d ECC to check current counts."
        ),
        95: (
            "Uncontained ECC -- data corruption possible. Drain workloads "
            "and take the GPU offline. Schedule RMA."
        ),
    }
    return actions.get(
        code,
        "Review nvidia-bug-report.sh output. Cross-reference XID code in NVIDIA docs.",
    )


@register_module
class KernelLogsModule(BaseDiagnosticModule):
    name = "kernel_logs"
    display_name = "Kernel Logs"
    requires_root = True
    required_tools = ["dmesg"]
    optional_tools = ["journalctl"]

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        entries: list[dict[str, str]] = []
        xid_summary: dict[int, int] = {}
        categories: dict[str, int] = {}

        # grab kernel ring buffer -- prefer ISO timestamps
        dmesg = await run_command(
            "dmesg --time-format iso 2>/dev/null || dmesg",
            timeout=15.0,
        )
        log_lines: list[str] = []
        if dmesg.success:
            log_lines.extend(dmesg.stdout.splitlines())

        # supplementary source
        journal = await run_command(
            "journalctl -k --no-pager -n 5000 2>/dev/null",
            timeout=15.0,
        )
        if journal.success and journal.stdout:
            log_lines.extend(journal.stdout.splitlines())

        if not log_lines:
            return ModuleResult(
                module_name=self.name,
                data={"entries": [], "xid_summary": {}, "categories": {}},
            )

        # scan every line against every pattern
        for line in log_lines:
            for category, pattern in _PATTERNS.items():
                m = pattern.search(line)
                if not m:
                    continue

                timestamp = _extract_timestamp(line)
                entries.append({
                    "timestamp": timestamp,
                    "message": line.strip(),
                    "category": category,
                })
                categories[category] = categories.get(category, 0) + 1

                # track XID counts separately
                if category == "xid":
                    xid_code = int(m.group(2))
                    xid_summary[xid_code] = xid_summary.get(xid_code, 0) + 1

        # -- build findings from aggregated data -----------------------------------

        # XID findings (one per distinct code)
        for code, count in sorted(xid_summary.items()):
            desc = XID_DESCRIPTIONS.get(code, f"Unknown XID {code}")
            sev = _xid_severity(code)
            findings.append(Finding(
                code=f"xid_{code}",
                severity=sev,
                summary=f"XID {code}: {desc} ({count} occurrence{'s' if count != 1 else ''})",
                explanation=(
                    f"The NVIDIA driver reported XID {code} ({desc}). "
                    f"{'This is a critical hardware error that can cause data corruption or system instability.' if sev == Severity.CRITICAL else 'This indicates a GPU issue that should be investigated.'}"
                ),
                client_action=(
                    "Contact support and share this diagnostic report. "
                    f"XID {code} was detected {count} time{'s' if count != 1 else ''} "
                    "in the kernel log."
                ),
                engineer_action=_xid_engineer_action(code),
                detail={"xid_code": code, "count": count, "description": desc},
            ))

        # SXid findings
        sxid_count = categories.get("sxid", 0)
        if sxid_count:
            findings.append(Finding(
                code="sxid_errors",
                severity=Severity.WARNING,
                summary=f"NVSwitch SXid errors detected ({sxid_count} occurrences)",
                explanation=(
                    "NVSwitch reported SXid errors which indicate problems with "
                    "the inter-GPU NVLink fabric. This can affect multi-GPU "
                    "communication and training performance."
                ),
                client_action="Contact support. NVSwitch errors require physical inspection.",
                engineer_action=(
                    "Check NVSwitch firmware version. Inspect NVLink cables. "
                    "Run nvidia-smi nvlink -s for per-link error counters. "
                    "Collect nvidia-bug-report.sh."
                ),
                detail={"count": sxid_count},
            ))

        # NVRM errors (only if no XID already covers them)
        nvrm_count = categories.get("nvrm_error", 0)
        if nvrm_count and not xid_summary:
            findings.append(Finding(
                code="nvrm_errors",
                severity=Severity.WARNING,
                summary=f"NVRM driver errors detected ({nvrm_count} occurrences)",
                explanation=(
                    "The NVIDIA kernel driver logged error or failure messages "
                    "that don't map to a specific XID code. These can indicate "
                    "driver issues, resource exhaustion, or hardware problems."
                ),
                client_action="Contact support with this diagnostic report.",
                engineer_action=(
                    "Review the full dmesg output for context around NVRM messages. "
                    "Collect nvidia-bug-report.sh."
                ),
                detail={"count": nvrm_count},
            ))

        # OOM kills
        oom_count = categories.get("oom", 0)
        if oom_count:
            findings.append(Finding(
                code="oom_detected",
                severity=Severity.WARNING,
                summary=f"Out of memory events detected ({oom_count} occurrences)",
                explanation=(
                    "The kernel's OOM killer terminated one or more processes because "
                    "the system ran out of available RAM. This usually means workloads "
                    "are consuming more memory than the node has available."
                ),
                client_action=(
                    "Review memory allocation in your workloads. Consider requesting "
                    "nodes with more RAM or reducing batch sizes."
                ),
                engineer_action=(
                    "Check which processes were killed (look for 'Killed process' in "
                    "dmesg). Review cgroup memory limits. Verify swap configuration. "
                    "Check for memory leaks in long-running services."
                ),
                detail={"count": oom_count},
            ))

        # lockups (soft + hard)
        lockup_count = categories.get("soft_lockup", 0) + categories.get("hard_lockup", 0)
        if lockup_count:
            has_hard = categories.get("hard_lockup", 0) > 0
            findings.append(Finding(
                code="lockup_detected",
                severity=Severity.CRITICAL,
                summary=(
                    f"CPU {'hard ' if has_hard else ''}lockup detected "
                    f"({lockup_count} occurrences)"
                ),
                explanation=(
                    "One or more CPUs became unresponsive for an extended period. "
                    "Hard lockups mean the CPU stopped processing interrupts entirely. "
                    "This can cause workload failures and system instability."
                ),
                client_action="Contact support immediately. CPU lockups require investigation.",
                engineer_action=(
                    "Check for interrupt storms (cat /proc/interrupts). Review driver "
                    "and firmware versions. Check for known kernel bugs matching this "
                    "hardware. Inspect thermal and power delivery. A hard lockup often "
                    "points to a driver or firmware bug."
                ),
                detail={
                    "soft": categories.get("soft_lockup", 0),
                    "hard": categories.get("hard_lockup", 0),
                },
            ))

        # hung tasks
        hung_count = categories.get("hung_task", 0)
        if hung_count:
            findings.append(Finding(
                code="hung_task_detected",
                severity=Severity.WARNING,
                summary=f"Hung task warnings detected ({hung_count} occurrences)",
                explanation=(
                    "The kernel detected processes blocked in uninterruptible sleep "
                    "for longer than the hung_task_timeout. This often indicates I/O "
                    "stalls, NFS hangs, or driver deadlocks."
                ),
                client_action=(
                    "If workloads are stalling, contact support. Hung tasks can be "
                    "caused by storage or network issues."
                ),
                engineer_action=(
                    "Check storage health and NFS mounts. Look for blocked processes "
                    "in /proc/*/wchan. Review I/O wait with iostat."
                ),
                detail={"count": hung_count},
            ))

        # I/O errors
        io_count = categories.get("nvme_io_error", 0) + categories.get("ro_remount", 0)
        if io_count:
            findings.append(Finding(
                code="io_errors",
                severity=Severity.CRITICAL,
                summary=f"Storage I/O errors in kernel log ({io_count} occurrences)",
                explanation=(
                    "The kernel logged NVMe I/O errors or filesystem read-only "
                    "remounts. This means disk or NVMe communication failures occurred, "
                    "which can cause data loss and workload crashes."
                ),
                client_action=(
                    "Contact support for storage diagnostics. Do not ignore I/O "
                    "errors -- they can indicate imminent drive failure."
                ),
                engineer_action=(
                    "Check NVMe SMART health (nvme smart-log /dev/nvmeXn1). Inspect "
                    "cable connections. Review dmesg for the specific device and error "
                    "codes. If a filesystem went read-only, check fsck status."
                ),
                detail={
                    "nvme_errors": categories.get("nvme_io_error", 0),
                    "ro_remounts": categories.get("ro_remount", 0),
                },
            ))

        # IOMMU faults
        iommu_count = categories.get("iommu_fault", 0)
        if iommu_count:
            findings.append(Finding(
                code="iommu_fault",
                severity=Severity.WARNING,
                summary=f"IOMMU/DMAR faults detected ({iommu_count} occurrences)",
                explanation=(
                    "The kernel reported IOMMU translation faults (Intel DMAR or "
                    "AMD-Vi). This can indicate DMA misconfiguration, a driver bug, "
                    "or a device trying to access memory outside its allowed range."
                ),
                client_action="Contact support if experiencing device errors or crashes.",
                engineer_action=(
                    "Review IOMMU group assignments. Check if passthrough mode is "
                    "needed (intel_iommu=on iommu=pt). Verify driver compatibility."
                ),
                detail={"count": iommu_count},
            ))

        # serialize xid_summary keys to strings for JSON compat
        xid_summary_str = {str(k): v for k, v in xid_summary.items()}

        # last error age -- how recent was the most recent problem entry
        last_error_ts = ""
        for entry in reversed(entries):
            cat = entry.get("category", "")
            if cat in ("xid", "sxid", "nvrm_error", "oom", "soft_lockup",
                        "hard_lockup", "nvme_io_error", "iommu_fault"):
                last_error_ts = entry.get("timestamp", "")
                break

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data={
                "entries": entries,
                "xid_summary": xid_summary_str,
                "categories": categories,
                "last_error_ts": last_error_ts,
            },
        )
