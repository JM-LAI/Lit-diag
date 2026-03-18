"""System information -- CPU, memory, NUMA, kernel, uptime."""

from __future__ import annotations

import os
import re
from typing import Any

from lit_diag.modules.base import BaseDiagnosticModule, Finding, ModuleResult, Severity
from lit_diag.utils.commands import run_command
from lit_diag.engine.module_loader import register_module


def _read_proc_file(path: str) -> str:
    """Read a /proc or /sys file, returning empty string on failure."""
    try:
        with open(path) as fh:
            return fh.read()
    except (OSError, PermissionError):
        return ""


def _parse_cpuinfo(text: str) -> tuple[str, int, int]:
    """Extract model name, total cores, and socket count from /proc/cpuinfo."""
    model = ""
    physical_ids: set[str] = set()
    core_count = 0

    for line in text.splitlines():
        if line.startswith("model name") and not model:
            model = line.split(":", 1)[1].strip()
        if line.startswith("physical id"):
            physical_ids.add(line.split(":", 1)[1].strip())
        if line.startswith("processor"):
            core_count += 1

    sockets = len(physical_ids) if physical_ids else 1
    return model, core_count, sockets


def _parse_meminfo_kb(text: str, field: str) -> int:
    """Pull a kB value from /proc/meminfo (e.g. MemTotal, MemAvailable)."""
    m = re.search(rf"^{re.escape(field)}:\s+(\d+)\s+kB", text, re.MULTILINE)
    return int(m.group(1)) if m else 0


def _format_uptime(seconds: float) -> str:
    """Turn seconds into a readable 'Xd Xh Xm' string."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _count_numa_nodes() -> int:
    """Count NUMA nodes from sysfs."""
    node_dir = "/sys/devices/system/node"
    if not os.path.isdir(node_dir):
        return 0
    try:
        return len([
            d for d in os.listdir(node_dir)
            if d.startswith("node") and d[4:].isdigit()
        ])
    except OSError:
        return 0


@register_module
class SystemModule(BaseDiagnosticModule):
    name = "system"
    display_name = "System Info"
    requires_root = False

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        data: dict[str, Any] = {
            "cpu": "",
            "cpu_cores": 0,
            "cpu_sockets": 0,
            "architecture": "",
            "kernel": "",
            "os": "",
            "memory_total": "",
            "numa_nodes": 0,
            "uptime": "",
            "load_average": "",
            "hostname": "",
        }

        # -- CPU info --
        cpuinfo = _read_proc_file("/proc/cpuinfo")
        if cpuinfo:
            model, cores, sockets = _parse_cpuinfo(cpuinfo)
            data["cpu"] = model
            data["cpu_cores"] = cores
            data["cpu_sockets"] = sockets

        # -- NUMA --
        data["numa_nodes"] = _count_numa_nodes()

        # -- Memory --
        meminfo = _read_proc_file("/proc/meminfo")
        if meminfo:
            total_kb = _parse_meminfo_kb(meminfo, "MemTotal")
            available_kb = _parse_meminfo_kb(meminfo, "MemAvailable")
            total_gb = round(total_kb / (1024 * 1024), 1)
            data["memory_total"] = f"{total_gb} GB"

            if total_kb > 0 and available_kb > 0:
                avail_pct = (available_kb / total_kb) * 100
                data["memory_available_pct"] = round(avail_pct, 1)
                if avail_pct < 10:
                    findings.append(Finding(
                        code="low_memory",
                        severity=Severity.WARNING,
                        summary=f"Available memory is low ({avail_pct:.0f}% free)",
                        explanation=(
                            f"Only {avail_pct:.1f}% of system memory is "
                            "available. Heavy memory pressure can cause OOM "
                            "kills and degrade GPU workload performance."
                        ),
                        client_action=(
                            "Check if any unexpected processes are consuming "
                            "memory. Contact support if this persists."
                        ),
                        engineer_action=(
                            "Run 'ps aux --sort=-%mem | head' to find top "
                            "memory consumers. Check for memory leaks or "
                            "orphaned processes."
                        ),
                        detail={
                            "total_kb": total_kb,
                            "available_kb": available_kb,
                            "available_pct": round(avail_pct, 1),
                        },
                    ))

        # -- Kernel / Arch --
        uname_r = await run_command("uname -r", timeout=5.0)
        if uname_r.success:
            data["kernel"] = uname_r.stdout.strip()

        uname_m = await run_command("uname -m", timeout=5.0)
        if uname_m.success:
            data["architecture"] = uname_m.stdout.strip()

        # -- OS release --
        os_release = _read_proc_file("/etc/os-release")
        if os_release:
            m = re.search(r'PRETTY_NAME="([^"]+)"', os_release)
            if m:
                data["os"] = m.group(1)

        # -- Uptime --
        uptime_secs = 0.0
        uptime_raw = _read_proc_file("/proc/uptime")
        if uptime_raw:
            try:
                uptime_secs = float(uptime_raw.split()[0])
                data["uptime"] = _format_uptime(uptime_secs)
                data["uptime_seconds"] = uptime_secs
            except (ValueError, IndexError):
                pass

        # -- Short uptime: rebooted < 1h ago (not first boot) --
        if uptime_secs > 0 and uptime_secs < 3600:
            findings.append(Finding(
                code="short_uptime",
                severity=Severity.WARNING,
                summary="This node rebooted less than 1 hour ago",
                explanation=(
                    f"System uptime is {_format_uptime(uptime_secs)}. "
                    "If this reboot was unexpected, it may indicate a crash or "
                    "automatic update."
                ),
                client_action="Check if this reboot was expected. Contact support if not.",
                engineer_action=(
                    "Check 'last reboot' and kernel logs for panic/oops. "
                    "Review MCE logs if available."
                ),
                detail={"uptime_seconds": uptime_secs},
            ))

        # -- Load average --
        loadavg_raw = _read_proc_file("/proc/loadavg")
        if loadavg_raw:
            parts = loadavg_raw.split()
            if len(parts) >= 3:
                data["load_average"] = f"{parts[0]} {parts[1]} {parts[2]}"

                # check if load is way above core count
                try:
                    load_1m = float(parts[0])
                    core_count = data.get("cpu_cores", 0)
                    if core_count > 0 and load_1m > (core_count * 2):
                        findings.append(Finding(
                            code="high_load",
                            severity=Severity.WARNING,
                            summary=(
                                f"System load ({load_1m:.1f}) is over 2x "
                                f"CPU core count ({core_count})"
                            ),
                            explanation=(
                                "The system's 1-minute load average is more "
                                "than double the number of CPU cores. This "
                                "typically means processes are waiting for "
                                "CPU time and the system is oversubscribed."
                            ),
                            client_action=(
                                "Check running workloads and consider "
                                "reducing concurrency or contacting support."
                            ),
                            engineer_action=(
                                "Run 'top' or 'htop' to identify CPU-heavy "
                                "processes. Check for runaway jobs or "
                                "misconfigured batch schedulers."
                            ),
                            detail={
                                "load_1m": load_1m,
                                "cpu_cores": core_count,
                            },
                        ))
                except ValueError:
                    pass

        # -- Hostname --
        hostname = await run_command("hostname -f 2>/dev/null || hostname", timeout=5.0)
        if hostname.success:
            data["hostname"] = hostname.stdout.strip()

        # -- Primary IP (for tickets / SSH reference) --
        ip_result = await run_command(
            "hostname -I 2>/dev/null | awk '{print $1}'", timeout=5.0
        )
        if ip_result.success and ip_result.stdout.strip():
            data["primary_ip"] = ip_result.stdout.strip().split()[0]

        # -- NTP / time sync --
        time_sync = await self._check_time_sync()
        if time_sync:
            data["time_sync"] = time_sync

        # -- Reboot history (last 5 reboots) --
        reboot_result = await run_command("last reboot 2>/dev/null | head -6", timeout=5.0)
        if reboot_result.success and reboot_result.stdout:
            lines = [ln.strip() for ln in reboot_result.stdout.splitlines() if ln.strip()]
            data["reboot_history"] = lines[:5]

        # -- Kernel taint check --
        tainted = _read_proc_file("/proc/sys/kernel/tainted")
        if tainted:
            try:
                taint_val = int(tainted.strip())
                data["kernel_tainted"] = taint_val
                data["kernel_tainted_decoded"] = _decode_taint(taint_val)
                if taint_val > 0:
                    # P (1) = proprietary module, O (4096) = out-of-tree module
                    # E (8192) = unsigned module -- all expected with nvidia driver
                    nvidia_expected_flags = 1 | 4096 | 8192  # 12289
                    non_nvidia_flags = taint_val & ~nvidia_expected_flags

                    if non_nvidia_flags > 0:
                        # taint has flags beyond what nvidia normally sets
                        findings.append(Finding(
                            code="kernel_tainted",
                            severity=Severity.WARNING,
                            summary=f"Kernel has unusual taint flags ({taint_val})",
                            explanation=(
                                "The kernel taint flags include values beyond "
                                "what the NVIDIA driver normally sets. This could "
                                "indicate a previous crash, hardware error, or "
                                "other kernel issue worth investigating."
                            ),
                            client_action=(
                                "Mention this to support if you're seeing "
                                "stability issues or crashes."
                            ),
                            engineer_action=(
                                "Decode flags: "
                                "https://docs.kernel.org/admin-guide/tainted-kernels.html "
                                f"Total={taint_val}, non-nvidia flags={non_nvidia_flags}. "
                                "G(2)=GPU error, D(8)=died/oops, W(4)=warning. "
                                "Check dmesg for the root cause."
                            ),
                            detail={"taint_value": taint_val, "non_nvidia_flags": non_nvidia_flags},
                        ))
                    # if only nvidia-expected flags, skip the finding entirely --
                    # no need to alarm anyone about something totally normal
            except ValueError:
                pass

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data=data,
        )

    async def _check_time_sync(self) -> dict[str, Any]:
        """Check NTP / time sync status via timedatectl or chronyc."""
        info: dict[str, Any] = {}

        tdc = await run_command("timedatectl show 2>/dev/null", timeout=5.0)
        if tdc.success and tdc.stdout:
            for line in tdc.stdout.splitlines():
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if key == "NTP":
                    info["ntp_enabled"] = val.lower() == "yes"
                elif key == "NTPSynchronized":
                    info["ntp_synced"] = val.lower() == "yes"
                elif key == "TimeUSec":
                    info["system_time"] = val
            if info:
                return info

        # fallback: chronyc
        chrony = await run_command("chronyc tracking 2>/dev/null", timeout=5.0)
        if chrony.success and chrony.stdout:
            info["source"] = "chrony"
            for line in chrony.stdout.splitlines():
                if "Leap status" in line:
                    info["ntp_synced"] = "Normal" in line
                elif "System time" in line:
                    info["offset"] = line.split(":", 1)[1].strip() if ":" in line else ""
                elif "Stratum" in line:
                    info["stratum"] = line.split(":", 1)[1].strip() if ":" in line else ""
            return info

        return info


# kernel taint bitmask decoder
_TAINT_FLAGS: dict[int, str] = {
    0: "P (proprietary module)",
    1: "F (module force-loaded)",
    2: "S (SMP with non-SMP kernel)",
    3: "R (module force-unloaded)",
    4: "M (machine check exception)",
    5: "B (bad page in page tables)",
    6: "U (user-requested taint)",
    7: "D (kernel died / oops)",
    8: "A (ACPI table overridden)",
    9: "W (kernel warning issued)",
    10: "C (staging driver loaded)",
    11: "I (workaround for firmware bug)",
    12: "O (out-of-tree module)",
    13: "E (unsigned module)",
    14: "L (soft lockup occurred)",
    15: "K (live-patched kernel)",
    16: "X (auxiliary taint)",
    17: "T (build-time known issue)",
}


def _decode_taint(taint_val: int) -> str:
    """Decode kernel taint bitmask to human-readable flags."""
    if taint_val == 0:
        return "clean"
    flags = []
    for bit, label in sorted(_TAINT_FLAGS.items()):
        if taint_val & (1 << bit):
            flags.append(label)
    return ", ".join(flags) if flags else str(taint_val)
