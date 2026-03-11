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
            "memory.used,memory.total,utilization.gpu "
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
                cols = _parse_csv_row(line, 12)
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

            # stash counts on the device dict for the report
            if i < len(devices):
                devices[i]["ecc_sram_uncorrectable"] = sram_unc
                devices[i]["ecc_dram_uncorrectable"] = dram_unc
                devices[i]["ecc_dram_correctable"] = dram_corr

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

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data=data,
        )
