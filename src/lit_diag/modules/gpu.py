"""GPU hardware diagnostics -- ECC errors, thermals, power, PCIe, memory."""

from __future__ import annotations

import os
import re
from typing import Any

from lit_diag.modules.base import BaseDiagnosticModule, Finding, ModuleResult, Severity
from lit_diag.utils.commands import run_command
from lit_diag.engine.module_loader import register_module

ECC_DRAM_CORRECTABLE_THRESHOLD = 8


def _safe_int(value: str, default: int = 0) -> int:
    """Parse an int from nvidia-smi output, handling N/A and junk."""
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in ("n/a", "not found", "[n/a]"):
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default


def _safe_float(value: str, default: float = 0.0) -> float:
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in ("n/a", "not found", "[n/a]"):
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def _parse_ecc_field(text: str, field_name: str) -> int:
    """Pull a single ECC counter from nvidia-smi -q output block."""
    pattern = rf"{re.escape(field_name)}\s*:\s*(\S+)"
    m = re.search(pattern, text)
    if m:
        return _safe_int(m.group(1))
    return 0


def _parse_clock_mhz(text: str, section: str, field: str) -> int:
    """Pull clock value in MHz from nvidia-smi -q (e.g. Max Clocks -> Memory)."""
    # section like "Max Clocks", field like "Memory" or "Graphics"
    block = re.search(
        rf"{re.escape(section)}\s*\n(.*?)(?=\n\w|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not block:
        return 0
    pattern = rf"{re.escape(field)}\s*:\s*(\d+)\s*MHz"
    m = re.search(pattern, block.group(1), re.IGNORECASE)
    return _safe_int(m.group(1)) if m else 0


def _parse_csv_row(line: str, expected_cols: int) -> list[str]:
    """Split a CSV row and pad/truncate to expected length."""
    parts = [c.strip() for c in line.split(",")]
    while len(parts) < expected_cols:
        parts.append("")
    return parts[:expected_cols]


@register_module
class GPUModule(BaseDiagnosticModule):
    name = "gpu"
    display_name = "GPU Health"
    required_tools = ["nvidia-smi"]

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        devices: list[dict[str, Any]] = []
        data: dict[str, Any] = {"devices": devices}

        # bail early if no gpu device nodes exist
        if not any(
            os.path.exists(f"/dev/nvidia{i}") for i in range(16)
        ) and not os.path.exists("/dev/nvidiactl"):
            findings.append(Finding(
                code="no_gpus_found",
                severity=Severity.ERROR,
                summary="No NVIDIA GPUs detected",
                explanation=(
                    "No /dev/nvidia* device nodes found. Either no GPUs are "
                    "installed or the NVIDIA kernel driver is not loaded."
                ),
                client_action="Verify GPUs are physically installed and contact support.",
                engineer_action="Check lsmod | grep nvidia, verify driver loaded with modprobe nvidia.",
            ))
            return ModuleResult(
                module_name=self.name,
                findings=findings,
                data=data,
            )

        # -- structured CSV query for per-gpu metrics --
        csv_cmd = (
            "nvidia-smi --query-gpu="
            "index,name,temperature.gpu,temperature.memory,"
            "power.draw,power.limit,clocks.current.sm,"
            "pcie.link.gen.current,pcie.link.width.current,"
            "memory.used,memory.total,utilization.gpu,"
            "clocks_throttle_reasons.active,"
            "retired_pages.sbe,retired_pages.dbe,"
            "power.draw.instant "
            "--format=csv,noheader,nounits"
        )
        csv_result = await run_command(csv_cmd, timeout=60.0)

        if csv_result.timed_out:
            findings.append(Finding(
                code="gpu_driver_hung",
                severity=Severity.CRITICAL,
                summary="GPU driver is not responding",
                explanation=(
                    "nvidia-smi timed out, which means the GPU driver is "
                    "frozen or a GPU has crashed. The node is effectively "
                    "unusable for GPU workloads."
                ),
                client_action="Contact support immediately -- this node needs intervention.",
                engineer_action=(
                    "Check dmesg for XID errors. Try nvidia-smi -r to reset "
                    "the driver. If that hangs too, the node needs a hard reboot."
                ),
            ))
            return ModuleResult(
                module_name=self.name,
                findings=findings,
                data=data,
            )

        if csv_result.success and csv_result.stdout:
            for line in csv_result.stdout.splitlines():
                if not line.strip():
                    continue
                cols = _parse_csv_row(line, 16)
                devices.append({
                    "index": _safe_int(cols[0]),
                    "name": cols[1],
                    "temp": _safe_float(cols[2]),
                    "temp_memory": _safe_float(cols[3]),
                    "power_draw": _safe_float(cols[4]),
                    "power_limit": _safe_float(cols[5]),
                    "clocks_sm": _safe_int(cols[6]),
                    "pcie_gen": _safe_int(cols[7]),
                    "pcie_width": _safe_int(cols[8]),
                    "memory_used": _safe_float(cols[9]),
                    "memory_total": _safe_float(cols[10]),
                    "utilization": _safe_int(cols[11]),
                    "throttle_reasons": cols[12].strip() if cols[12].strip().lower() not in ("", "n/a", "not found", "[n/a]", "0x0000000000000000") else "",
                    "retired_pages_sbe": _safe_int(cols[13]),
                    "retired_pages_dbe": _safe_int(cols[14]),
                    "power_draw_instant": _safe_float(cols[15]),
                })

        # -- ECC checks per GPU --
        gpu_count = len(devices)
        if gpu_count == 0:
            # fallback: try nvidia-smi -L to count GPUs
            count_result = await run_command("nvidia-smi -L", timeout=15.0)
            if count_result.success and count_result.stdout:
                gpu_count = len([
                    ln for ln in count_result.stdout.splitlines() if ln.strip()
                ])

        for i in range(gpu_count):
            ecc_result = await run_command(f"nvidia-smi -q -i {i}", timeout=30.0)
            if not ecc_result.success:
                continue

            output = ecc_result.stdout
            gpu_label = f"GPU {i}"
            if i < len(devices):
                gpu_label = f"GPU {i} ({devices[i].get('name', 'unknown')})"

            # nvidia-smi -q nests ECC errors under "Volatile" and "Aggregate"
            # sections -- we care about Volatile (since last reboot).
            # grab the Volatile block
            volatile_match = re.search(
                r"Volatile\s*\n(.*?)(?=Aggregate|ECC Errors\s*$|\Z)",
                output,
                re.DOTALL | re.IGNORECASE,
            )
            volatile_block = volatile_match.group(1) if volatile_match else output

            sram_unc = _parse_ecc_field(volatile_block, "SRAM Uncorrectable SEC-DED")
            dram_unc = _parse_ecc_field(volatile_block, "DRAM Uncorrectable")
            dram_corr = _parse_ecc_field(volatile_block, "DRAM Correctable")

            # stash counts and UUID on the device dict for the report
            if i < len(devices):
                devices[i]["ecc_sram_uncorrectable"] = sram_unc
                devices[i]["ecc_dram_uncorrectable"] = dram_unc
                devices[i]["ecc_dram_correctable"] = dram_corr
                # UUID for scheduler/job log matching
                uuid_match = re.search(r"GPU\s+UUID\s*:\s*(\S+)", output, re.IGNORECASE)
                if uuid_match:
                    devices[i]["uuid"] = uuid_match.group(1).strip()
                # Serial for RMA / support tickets
                serial_match = re.search(r"Serial\s+Number\s*:\s*(\S+)", output, re.IGNORECASE)
                if serial_match:
                    devices[i]["serial"] = serial_match.group(1).strip()
                # Max clocks for application-clocks fix
                max_mem = _parse_clock_mhz(output, "Max Clocks", "Memory")
                max_sm = _parse_clock_mhz(output, "Max Clocks", "Graphics")
                if not max_sm:
                    max_sm = _parse_clock_mhz(output, "Max Clocks", "SM")
                devices[i]["max_clock_mem"] = max_mem
                devices[i]["max_clock_sm"] = max_sm
                app_mem = _parse_clock_mhz(output, "Applications Clocks", "Memory")
                app_sm = _parse_clock_mhz(output, "Applications Clocks", "Graphics")
                if not app_sm:
                    app_sm = _parse_clock_mhz(output, "Applications Clocks", "SM")
                devices[i]["app_clock_mem"] = app_mem
                devices[i]["app_clock_sm"] = app_sm
                # ECC mode (disabled = memory errors not detected)
                ecc_mode_match = re.search(
                    r"ECC\s+Mode\s*\n\s*Current\s*:\s*(\w+)",
                    output,
                    re.IGNORECASE,
                )
                if ecc_mode_match:
                    ecc_val = ecc_mode_match.group(1).lower()
                    devices[i]["ecc_enabled"] = "enabled" in ecc_val

                # row remapping (H100+) -- separate from retired pages
                remap_corr = _parse_ecc_field(output, "Remapped Rows Correctable")
                remap_unc = _parse_ecc_field(output, "Remapped Rows Uncorrectable")
                remap_pending = "Yes" in re.findall(r"Remapping Failure Occurred\s*:\s*(\S+)", output) if output else False
                devices[i]["remap_correctable"] = remap_corr
                devices[i]["remap_uncorrectable"] = remap_unc
                devices[i]["remap_failure"] = remap_pending

            if sram_unc > 0:
                findings.append(Finding(
                    code="ecc_sram_uncorrectable",
                    severity=Severity.CRITICAL,
                    summary=f"{gpu_label} has uncorrectable SRAM errors",
                    explanation=(
                        f"{gpu_label} reports {sram_unc} SRAM Uncorrectable "
                        "SEC-DED errors. These are L2 cache errors that "
                        "indicate hardware aging and cannot be fixed by "
                        "driver resets."
                    ),
                    client_action="Contact support -- this GPU likely needs replacement.",
                    engineer_action=(
                        "Check if errors persist across GPU resets. Review XID "
                        "errors in dmesg. Likely needs RMA."
                    ),
                    detail={"gpu_index": i, "count": sram_unc},
                ))

            if dram_unc > 0:
                findings.append(Finding(
                    code="ecc_dram_uncorrectable",
                    severity=Severity.CRITICAL,
                    summary=f"{gpu_label} has uncorrectable memory errors",
                    explanation=(
                        f"{gpu_label} reports {dram_unc} DRAM Uncorrectable "
                        "errors. These are permanent memory faults that can "
                        "cause computation corruption."
                    ),
                    client_action="Contact support -- this GPU needs replacement.",
                    engineer_action=(
                        "Check row remapping status and page retirement counters. "
                        "Try nvidia-smi --gpu-reset -i {i} to see if errors clear."
                    ),
                    detail={"gpu_index": i, "count": dram_unc},
                ))

            if dram_corr > ECC_DRAM_CORRECTABLE_THRESHOLD:
                findings.append(Finding(
                    code="ecc_dram_correctable_high",
                    severity=Severity.WARNING,
                    summary=f"{gpu_label} has elevated correctable memory errors",
                    explanation=(
                        f"{gpu_label} reports {dram_corr} DRAM Correctable "
                        f"errors (threshold: {ECC_DRAM_CORRECTABLE_THRESHOLD}). "
                        "Soft errors above this level may indicate early "
                        "degradation."
                    ),
                    client_action=(
                        "Monitor error counts and contact support if they "
                        "continue increasing."
                    ),
                    engineer_action=(
                        "Check if error rate is tied to specific workloads. "
                        "Compare against fleet baseline for this GPU model."
                    ),
                    detail={"gpu_index": i, "count": dram_corr},
                ))

        # -- Throttle reason findings --
        for dev in devices:
            throttle = dev.get("throttle_reasons", "")
            if throttle and "idle" not in throttle.lower() and "none" not in throttle.lower():
                idx = dev.get("index", "?")
                findings.append(Finding(
                    code="gpu_throttled",
                    severity=Severity.WARNING,
                    summary=f"GPU {idx} is being throttled: {throttle}",
                    explanation=(
                        f"GPU {idx} clock speeds are being reduced. "
                        "This can cause slower workload performance."
                    ),
                    client_action="Contact support if performance is degraded.",
                    engineer_action=(
                        f"Check nvidia-smi -q -i {idx} for throttle details. "
                        "Common causes: thermal limits, power cap, HW slowdown."
                    ),
                    detail={"gpu_index": idx, "throttle_reasons": throttle},
                ))

        # -- Memory pressure: any GPU > 90% used --
        for dev in devices:
            mem_used = dev.get("memory_used", 0) or 0
            mem_total = dev.get("memory_total", 0) or 0
            if mem_total > 0 and (mem_used / mem_total) > 0.9:
                idx = dev.get("index", "?")
                pct = (mem_used / mem_total) * 100
                findings.append(Finding(
                    code="gpu_memory_pressure",
                    severity=Severity.WARNING,
                    summary=f"GPU {idx} is almost out of memory ({pct:.0f}%)",
                    explanation=(
                        f"GPU {idx} has {pct:.0f}% of its memory in use. "
                        "Workloads may crash with CUDA OOM."
                    ),
                    client_action="Free GPU memory or contact support if jobs fail with OOM.",
                    engineer_action=(
                        f"Check nvidia-smi for processes on GPU {idx}. "
                        "Consider exclusive compute mode for single-tenant nodes."
                    ),
                    detail={"gpu_index": idx, "memory_pct": round(pct, 1)},
                ))

        # -- ECC disabled on any GPU --
        for dev in devices:
            if dev.get("ecc_enabled") is False:
                idx = dev.get("index", "?")
                gpu_label = f"GPU {idx} ({dev.get('name', 'unknown')})"
                findings.append(Finding(
                    code="gpu_ecc_disabled",
                    severity=Severity.WARNING,
                    summary=f"{gpu_label} has ECC disabled",
                    explanation=(
                        f"{gpu_label} has Error Correction Code (ECC) disabled. "
                        "Memory errors will not be detected or corrected."
                    ),
                    client_action=(
                        "Enable ECC for data integrity. Requires a reboot. "
                        "Contact support if unsure."
                    ),
                    engineer_action=(
                        f"Run 'nvidia-smi -i {idx} --ecc-config=1' to enable ECC. "
                        "A reboot is required for the change to take effect."
                    ),
                    detail={"gpu_index": idx},
                ))

        # -- GPU clocks not at max (application clocks fix) --
        for dev in devices:
            max_mem = dev.get("max_clock_mem", 0) or 0
            max_sm = dev.get("max_clock_sm", 0) or 0
            app_mem = dev.get("app_clock_mem", 0) or 0
            app_sm = dev.get("app_clock_sm", 0) or 0
            if max_mem > 0 and max_sm > 0 and (app_mem < max_mem or app_sm < max_sm):
                idx = dev.get("index", "?")
                findings.append(Finding(
                    code="gpu_clocks_not_max",
                    severity=Severity.WARNING,
                    summary=f"GPU {idx} is not running at maximum clocks",
                    explanation=(
                        f"GPU {idx} application clocks ({app_sm}/{app_mem} MHz) are below "
                        f"max ({max_sm}/{max_mem} MHz). Performance may be reduced."
                    ),
                    client_action="Contact support if performance is degraded.",
                    engineer_action=(
                        f"Run 'nvidia-smi -i {idx} -ac {max_mem},{max_sm}' to set max clocks. "
                        "Only affects current boot."
                    ),
                    fix_command=f"nvidia-smi -i {idx} -ac {max_mem},{max_sm}",
                    fix_description=f"Set GPU {idx} application clocks to max ({max_sm}/{max_mem} MHz)",
                    fix_impact="May improve performance. Only affects current boot.",
                    fix_requires_root=True,
                    detail={"gpu_index": idx, "max_sm": max_sm, "max_mem": max_mem},
                ))

        # -- Power capped: draw near limit --
        for dev in devices:
            draw = dev.get("power_draw", 0) or 0
            limit = dev.get("power_limit", 0) or 0
            if limit > 0 and draw > 0 and (draw / limit) > 0.95:
                idx = dev.get("index", "?")
                findings.append(Finding(
                    code="gpu_power_capped",
                    severity=Severity.WARNING,
                    summary=f"GPU {idx} is hitting its power limit",
                    explanation=(
                        f"GPU {idx} is drawing {draw:.0f}W of {limit:.0f}W limit. "
                        "Performance may be reduced by power throttling."
                    ),
                    client_action="Contact support if performance is degraded.",
                    engineer_action=(
                        f"Check nvidia-smi -q -i {idx} for throttle reasons. "
                        "Power cap may be set too low for workload."
                    ),
                    detail={"gpu_index": idx, "power_draw": draw, "power_limit": limit},
                ))

        # -- Retired pages findings --
        for dev in devices:
            dbe = dev.get("retired_pages_dbe", 0)
            sbe = dev.get("retired_pages_sbe", 0)
            remap_fail = dev.get("remap_failure", False)
            idx = dev.get("index", "?")
            if dbe > 0:
                findings.append(Finding(
                    code="gpu_retired_pages_dbe",
                    severity=Severity.CRITICAL,
                    summary=f"GPU {idx} has {dbe} double-bit retired page(s)",
                    explanation=(
                        "Double-bit errors in GPU memory are uncorrectable. "
                        "Pages have been permanently retired to prevent data corruption."
                    ),
                    client_action="Contact support -- this GPU may need replacement.",
                    engineer_action=(
                        f"Run nvidia-smi -q -i {idx} to check pending retirements. "
                        "If DBE count is increasing, schedule RMA."
                    ),
                    detail={"gpu_index": idx, "dbe": dbe, "sbe": sbe},
                ))
            if remap_fail:
                findings.append(Finding(
                    code="gpu_remap_failure",
                    severity=Severity.CRITICAL,
                    summary=f"GPU {idx} row remapping failure -- memory bank exhausted",
                    explanation=(
                        "The GPU has run out of spare memory rows for remapping. "
                        "Further ECC errors on this GPU will cause uncorrectable failures."
                    ),
                    client_action="Contact support immediately -- this GPU needs replacement.",
                    engineer_action=f"RMA GPU {idx}. Row remap table is full.",
                    detail={"gpu_index": idx},
                ))

        # -- NUMA topology (which GPU is on which NUMA node) --
        topo_result = await run_command(
            "nvidia-smi topo -m 2>/dev/null | head -20",
            timeout=10.0,
        )
        if topo_result.success and topo_result.stdout:
            data["numa_topology_raw"] = topo_result.stdout.strip()
        for dev in devices:
            idx = dev.get("index", 0)
            numa_path = f"/sys/bus/pci/devices/0000:{dev.get('pcie_bdf', '')}/numa_node"
            if not dev.get("pcie_bdf"):
                # try to get NUMA from nvidia-smi
                numa_result = await run_command(
                    f"nvidia-smi -q -i {idx} 2>/dev/null | grep -i 'NUMA Node'",
                    timeout=5.0,
                )
                if numa_result.success and numa_result.stdout:
                    numa_match = re.search(r"NUMA\s+Node\s*:\s*(\d+)", numa_result.stdout, re.IGNORECASE)
                    if numa_match:
                        dev["numa_node"] = _safe_int(numa_match.group(1))

        # -- GPU process snapshot (nice-to-have, non-fatal if it fails) --
        proc_result = await run_command(
            "nvidia-smi --query-compute-apps=pid,used_memory,gpu_name "
            "--format=csv,noheader,nounits",
            timeout=15.0,
        )
        if proc_result.success and proc_result.stdout:
            processes: list[dict[str, Any]] = []
            for line in proc_result.stdout.splitlines():
                parts = _parse_csv_row(line, 3)
                processes.append({
                    "pid": _safe_int(parts[0]),
                    "memory_used_mib": _safe_float(parts[1]),
                    "gpu_name": parts[2],
                })
            data["processes"] = processes

        # -- Utilization: all GPUs at 0% but processes present --
        processes = data.get("processes", [])
        if processes and devices:
            all_zero_util = all(dev.get("utilization", 0) == 0 for dev in devices)
            if all_zero_util:
                findings.append(Finding(
                    code="gpu_utilization_zero",
                    severity=Severity.WARNING,
                    summary="GPUs have processes attached but are idle",
                    explanation=(
                        "Your GPUs show 0% utilization while processes are running. "
                        "Your workload may not be using the GPUs effectively."
                    ),
                    client_action=(
                        "Check that your application is actually using GPUs. "
                        "If training is slow, contact support."
                    ),
                    engineer_action=(
                        "Verify workload is GPU-bound. Check NCCL/cudaMalloc usage."
                    ),
                ))

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data=data,
        )
