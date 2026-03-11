"""NVLink interconnect diagnostics -- link state, errors, topology."""

from __future__ import annotations

import re
from typing import Any

from lit_diag.modules.base import BaseDiagnosticModule, Finding, ModuleResult, Severity
from lit_diag.utils.commands import run_command
from lit_diag.engine.module_loader import register_module


def _safe_int(value: str, default: int = 0) -> int:
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in ("n/a", "not found", "[n/a]"):
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default


def _infer_nvlink_version(speed_str: str, gpu_name: str) -> str:
    """Infer NVLink version from link speed or GPU model.

    NVLink speeds:
      v1: ~20 GB/s (P100)
      v2: ~25 GB/s (V100)
      v3: ~25 GB/s (A100, different encoding)
      v4: ~25-26.5 GB/s (H100/H200)
    """
    speed_match = re.search(r"([\d.]+)\s*GB/s", speed_str)
    speed = float(speed_match.group(1)) if speed_match else 0.0

    gpu_lower = gpu_name.lower()
    if "h100" in gpu_lower or "h200" in gpu_lower:
        return "4"
    elif "a100" in gpu_lower or "a800" in gpu_lower:
        return "3"
    elif "v100" in gpu_lower:
        return "2"
    elif speed >= 26:
        return "4"
    elif speed >= 24:
        return "3"
    elif speed >= 19:
        return "2"
    return "?"


def _parse_link_status(output: str) -> list[dict[str, Any]]:
    """Parse nvidia-smi nvlink -s output into per-GPU link summaries.

    Typical output format:
        GPU 0: NVIDIA H100 80GB HBM3 (UUID: GPU-...)
             Link 0: 26.562 GB/s
             Link 1: 26.562 GB/s
             ...
    """
    links: list[dict[str, Any]] = []
    current_gpu: int | None = None
    current_gpu_name: str = ""
    link_count = 0
    active_count = 0
    inactive_count = 0
    version = ""
    first_link_speed = ""

    for line in output.splitlines():
        line = line.strip()
        if not line:
            if current_gpu is not None and link_count > 0:
                if not version:
                    version = _infer_nvlink_version(first_link_speed, current_gpu_name)
                links.append({
                    "gpu": current_gpu,
                    "gpu_name": current_gpu_name,
                    "link_count": link_count,
                    "active": active_count,
                    "inactive": inactive_count,
                    "version": version,
                })
            current_gpu = None
            link_count = 0
            active_count = 0
            inactive_count = 0
            version = ""
            first_link_speed = ""
            continue

        gpu_match = re.match(r"GPU\s+(\d+):\s*(.*)", line)
        if gpu_match:
            if current_gpu is not None and link_count > 0:
                if not version:
                    version = _infer_nvlink_version(first_link_speed, current_gpu_name)
                links.append({
                    "gpu": current_gpu,
                    "gpu_name": current_gpu_name,
                    "link_count": link_count,
                    "active": active_count,
                    "inactive": inactive_count,
                    "version": version,
                })
            current_gpu = int(gpu_match.group(1))
            current_gpu_name = gpu_match.group(2).split("(")[0].strip()
            link_count = 0
            active_count = 0
            inactive_count = 0
            version = ""
            first_link_speed = ""
            continue

        link_match = re.match(r"Link\s+\d+", line, re.IGNORECASE)
        if link_match and current_gpu is not None:
            link_count += 1
            if not first_link_speed:
                first_link_speed = line
            if "inactive" in line.lower() or "down" in line.lower():
                inactive_count += 1
            else:
                active_count += 1

        ver_match = re.search(r"NVLink Version\s*:\s*(\S+)", line, re.IGNORECASE)
        if ver_match:
            version = ver_match.group(1)

    if current_gpu is not None and link_count > 0:
        if not version:
            version = _infer_nvlink_version(first_link_speed, current_gpu_name)
        links.append({
            "gpu": current_gpu,
            "gpu_name": current_gpu_name,
            "link_count": link_count,
            "active": active_count,
            "inactive": inactive_count,
            "version": version,
        })

    return links


def _parse_error_counters(output: str) -> dict[int, dict[str, int]]:
    """Parse nvidia-smi nvlink -e output into per-GPU error totals.

    Returns {gpu_index: {error_type: count}}.
    """
    errors: dict[int, dict[str, int]] = {}
    current_gpu: int | None = None

    for line in output.splitlines():
        line_stripped = line.strip()

        gpu_match = re.match(r"GPU\s+(\d+)", line_stripped)
        if gpu_match:
            current_gpu = int(gpu_match.group(1))
            if current_gpu not in errors:
                errors[current_gpu] = {
                    "crc_flit": 0,
                    "crc_data": 0,
                    "replay": 0,
                    "recovery": 0,
                }
            continue

        if current_gpu is None:
            continue

        for key, pattern in [
            ("replay", r"Replay Errors\s*:\s*(\d+)"),
            ("recovery", r"Recovery Errors\s*:\s*(\d+)"),
            ("crc_flit", r"CRC FLIT Errors?\s*:\s*(\d+)"),
            ("crc_data", r"CRC Data Errors?\s*:\s*(\d+)"),
        ]:
            m = re.search(pattern, line_stripped, re.IGNORECASE)
            if m:
                errors[current_gpu][key] += _safe_int(m.group(1))

    return errors


@register_module
class NVLinkModule(BaseDiagnosticModule):
    name = "nvlink"
    display_name = "NVLink"
    required_tools = ["nvidia-smi"]

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        data: dict[str, Any] = {"links": [], "topology": ""}

        # -- link status --
        status_result = await run_command("nvidia-smi nvlink -s", timeout=30.0)

        if status_result.timed_out:
            findings.append(Finding(
                code="gpu_driver_hung",
                severity=Severity.CRITICAL,
                summary="GPU driver is not responding (NVLink query)",
                explanation="nvidia-smi nvlink timed out -- the driver may be hung.",
                client_action="Contact support immediately.",
                engineer_action="Check dmesg for XID errors, try nvidia-smi -r.",
            ))
            return ModuleResult(
                module_name=self.name,
                findings=findings,
                data=data,
            )

        # some systems just don't have NVLink (single-GPU, consumer cards, etc)
        if not status_result.success or "error" in status_result.stderr.lower():
            no_nvlink = (
                "nvlink is not supported" in status_result.stderr.lower()
                or "not supported" in status_result.stdout.lower()
                or status_result.returncode != 0
            )
            if no_nvlink:
                findings.append(Finding(
                    code="nvlink_not_available",
                    severity=Severity.DEGRADED,
                    summary="NVLink not available on this system",
                    explanation=(
                        "This GPU configuration does not use NVLink. This is "
                        "normal for single-GPU nodes or consumer-grade GPUs "
                        "and is not necessarily an error."
                    ),
                    client_action="No action needed unless multi-GPU training is expected.",
                    engineer_action="Verify this node type is not supposed to have NVLink.",
                ))
                return ModuleResult(
                    module_name=self.name,
                    findings=findings,
                    data=data,
                )

        link_summaries = _parse_link_status(status_result.stdout)
        data["links"] = link_summaries

        # check for inactive links
        for gpu_info in link_summaries:
            if gpu_info["inactive"] > 0:
                findings.append(Finding(
                    code="nvlink_inactive",
                    severity=Severity.CRITICAL,
                    summary=(
                        f"GPU {gpu_info['gpu']} has {gpu_info['inactive']} "
                        f"inactive NVLink connections"
                    ),
                    explanation=(
                        f"GPU {gpu_info['gpu']} ({gpu_info['gpu_name']}) has "
                        f"{gpu_info['inactive']} of {gpu_info['link_count']} "
                        "NVLink lanes down. GPUs cannot communicate at full "
                        "bandwidth, which will hurt multi-GPU workloads."
                    ),
                    client_action="Contact support -- NVLink hardware issue.",
                    engineer_action=(
                        "Check NVSwitch status and reseat GPU trays. Inspect "
                        "NVLink cable/connector for the affected GPU."
                    ),
                    detail={
                        "gpu_index": gpu_info["gpu"],
                        "inactive": gpu_info["inactive"],
                        "total": gpu_info["link_count"],
                    },
                ))

        # -- error counters --
        err_result = await run_command("nvidia-smi nvlink -e", timeout=30.0)
        if err_result.success and err_result.stdout:
            error_counters = _parse_error_counters(err_result.stdout)
            data["error_counters"] = error_counters

            for gpu_idx, counts in error_counters.items():
                total_errors = sum(counts.values())
                if total_errors == 0:
                    continue

                # replay/recovery errors are more concerning than CRC
                replay_recovery = counts.get("replay", 0) + counts.get("recovery", 0)
                severity = Severity.CRITICAL if replay_recovery > 100 else Severity.WARNING

                findings.append(Finding(
                    code="nvlink_errors",
                    severity=severity,
                    summary=f"GPU {gpu_idx} has NVLink errors (total: {total_errors})",
                    explanation=(
                        f"GPU {gpu_idx} NVLink error counters: "
                        f"replay={counts['replay']}, "
                        f"recovery={counts['recovery']}, "
                        f"CRC flit={counts['crc_flit']}, "
                        f"CRC data={counts['crc_data']}. "
                        "These indicate intermittent GPU communication issues "
                        "that can degrade multi-GPU performance."
                    ),
                    client_action=(
                        "Contact support if you are seeing degraded training "
                        "performance or job failures."
                    ),
                    engineer_action=(
                        "Check specific error counters -- high replay/recovery "
                        "counts often point to a cable or connector issue. "
                        "Compare against fleet baseline."
                    ),
                    detail={"gpu_index": gpu_idx, **counts},
                ))

        # -- topology matrix --
        topo_result = await run_command("nvidia-smi topo -m", timeout=15.0)
        if topo_result.success and topo_result.stdout:
            data["topology"] = topo_result.stdout

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data=data,
        )
