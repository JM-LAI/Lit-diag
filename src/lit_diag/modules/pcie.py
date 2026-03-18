"""PCIe bus diagnostics -- link health, AER errors, IOMMU status."""

from __future__ import annotations

import os
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


def _parse_link_field(text: str, field: str) -> tuple[str, str]:
    """Pull speed and width from an LnkCap or LnkSta line.

    Returns (speed, width) e.g. ("8GT/s", "x16").
    """
    pattern = rf"{field}:\s+Speed\s+(\S+).*?Width\s+(x\d+)"
    m = re.search(pattern, text)
    if m:
        return m.group(1), m.group(2)
    return ("unknown", "unknown")


def _speed_to_gen(speed: str) -> int:
    """Map PCIe link speed string to generation number."""
    mapping = {
        "2.5GT/s": 1,
        "5GT/s": 2,
        "8GT/s": 3,
        "16GT/s": 4,
        "32GT/s": 5,
        "64GT/s": 6,
    }
    return mapping.get(speed, 0)


def _width_to_int(width: str) -> int:
    """x16 -> 16, x8 -> 8, etc."""
    m = re.match(r"x(\d+)", width)
    return int(m.group(1)) if m else 0


def _read_aer_file(sysfs_path: str) -> int:
    """Sum all AER counters from a sysfs file. Returns 0 on any failure."""
    try:
        if not os.path.isfile(sysfs_path):
            return 0
        with open(sysfs_path) as fh:
            total = 0
            for line in fh:
                parts = line.strip().split()
                if len(parts) >= 2:
                    try:
                        total += int(parts[-1])
                    except ValueError:
                        continue
            return total
    except (OSError, PermissionError):
        return 0


def _sysfs_bdf(bdf: str) -> str:
    """Convert lspci short BDF to sysfs directory name.

    lspci gives '41:00.0', sysfs wants '0000:41:00.0'.
    """
    if not bdf.startswith("0000:"):
        return f"0000:{bdf}"
    return bdf


@register_module
class PCIeModule(BaseDiagnosticModule):
    name = "pcie"
    display_name = "PCIe Bus"
    required_tools = ["lspci"]
    optional_tools = ["setpci"]

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        devices: list[dict[str, Any]] = []

        # grab PCIe info from nvidia-smi as a reliable fallback
        # (lspci -vvv needs root for link cap/status)
        nvsmi_pcie = await self._get_nvidia_smi_pcie()

        # enumerate NVIDIA PCIe devices
        enum_result = await run_command("lspci -d 10de: -nn", timeout=10.0)
        if not enum_result.success:
            return ModuleResult(
                module_name=self.name,
                status=Severity.ERROR,
                error_message=f"lspci enumeration failed: {enum_result.stderr}",
            )

        lines = [l for l in enum_result.stdout.splitlines() if l.strip()]
        if not lines:
            return ModuleResult(
                module_name=self.name,
                data={"devices": []},
            )

        gpu_idx = 0
        for idx, line in enumerate(lines):
            bdf = line.split()[0]
            dev_name = line.split(maxsplit=1)[1] if len(line.split()) > 1 else "Unknown"

            detail = await run_command(f"lspci -vvv -s {bdf}", timeout=10.0)
            detail_text = detail.stdout if detail.success else ""

            cap_speed, cap_width = _parse_link_field(detail_text, "LnkCap")
            sta_speed, sta_width = _parse_link_field(detail_text, "LnkSta")

            capable_gen = _speed_to_gen(cap_speed)
            current_gen = _speed_to_gen(sta_speed)
            capable_width = _width_to_int(cap_width)
            current_width = _width_to_int(sta_width)

            # fallback to nvidia-smi data when lspci didn't return link info
            # (typically because we're not root)
            is_gpu = "3D controller" in dev_name or "VGA" in dev_name
            if is_gpu and (current_gen == 0 or current_width == 0):
                if gpu_idx < len(nvsmi_pcie):
                    pcie_info = nvsmi_pcie[gpu_idx]
                    if current_gen == 0:
                        current_gen = pcie_info.get("gen_current", 0)
                    if capable_gen == 0:
                        capable_gen = pcie_info.get("gen_max", 0)
                    if current_width == 0:
                        current_width = pcie_info.get("width_current", 0)
                    if capable_width == 0:
                        capable_width = pcie_info.get("width_max", 0)
                gpu_idx += 1

            # AER counters from sysfs
            sysfs_dev = f"/sys/bus/pci/devices/{_sysfs_bdf(bdf)}"
            correctable = _read_aer_file(f"{sysfs_dev}/aer_dev_correctable")
            fatal = _read_aer_file(f"{sysfs_dev}/aer_dev_fatal")

            # PCIe replay count from lspci output (early link degradation signal)
            replay_count = 0
            replay_match = re.search(r"RlmtRep\+\s*(\d+)", detail_text)
            if not replay_match:
                replay_match = re.search(r"Replay\s+Timer.*?Count:\s*(\d+)", detail_text)
            if replay_match:
                replay_count = int(replay_match.group(1))

            devices.append({
                "bdf": bdf,
                "name": dev_name,
                "current_gen": current_gen,
                "capable_gen": capable_gen,
                "current_width": current_width,
                "capable_width": capable_width,
                "correctable": correctable,
                "fatal": fatal,
                "replay_count": replay_count,
            })

            # link width degradation
            if capable_width and current_width and current_width < capable_width:
                findings.append(Finding(
                    code="pcie_link_degraded",
                    severity=Severity.WARNING,
                    summary=(
                        f"PCIe link for GPU {idx} is running at x{current_width} "
                        f"instead of x{capable_width}"
                    ),
                    explanation=(
                        "The PCIe link is operating at half (or less) of its designed "
                        "bandwidth. This reduces GPU-to-host transfer speed and can "
                        "bottleneck workloads that depend on PCIe throughput."
                    ),
                    client_action=(
                        "Contact support and reference this report. The GPU may need "
                        "physical reseating or the riser cable may need replacement."
                    ),
                    engineer_action=(
                        "Reseat the GPU in its slot. Inspect and reseat the riser cable "
                        "if present. Check for bent pins or debris in the PCIe slot. "
                        "Try a different slot if available."
                    ),
                    detail={"bdf": bdf, "current": current_width, "capable": capable_width},
                ))

            # link gen degradation
            if capable_gen and current_gen and current_gen < capable_gen:
                findings.append(Finding(
                    code="pcie_gen_degraded",
                    severity=Severity.WARNING,
                    summary=(
                        f"PCIe link for GPU {idx} running at Gen{current_gen} "
                        f"instead of Gen{capable_gen}"
                    ),
                    explanation=(
                        "The PCIe link negotiated a lower generation than the hardware "
                        "supports. Each generation roughly doubles bandwidth, so this "
                        "means reduced transfer speed between GPU and host."
                    ),
                    client_action=(
                        "Contact support and reference this report. A cable, riser, "
                        "or motherboard slot issue is likely."
                    ),
                    engineer_action=(
                        "Reseat the GPU. Check the riser cable and PCIe slot for "
                        "physical damage. Verify BIOS settings haven't capped PCIe "
                        "gen. Test with a different slot or riser."
                    ),
                    detail={"bdf": bdf, "current_gen": current_gen, "capable_gen": capable_gen},
                ))

            # AER errors
            if correctable > 0 or fatal > 0:
                sev = Severity.CRITICAL if fatal > 0 else Severity.WARNING
                findings.append(Finding(
                    code="pcie_aer_errors",
                    severity=sev,
                    summary=(
                        f"AER errors detected on PCIe device {bdf} "
                        f"(correctable={correctable}, fatal={fatal})"
                    ),
                    explanation=(
                        "Advanced Error Reporting (AER) counters indicate bus-level "
                        "communication errors between the device and the host. "
                        "Correctable errors are recoverable but indicate signal "
                        "integrity problems. Fatal errors mean data was lost."
                    ),
                    client_action=(
                        "Contact support with this report. PCIe bus errors can "
                        "indicate hardware problems that need physical inspection."
                    ),
                    engineer_action=(
                        "Check the upstream PCIe switch and retimers. Inspect power "
                        "delivery to the slot. Reseat the GPU and riser cable. If "
                        "fatal errors are present, schedule a maintenance window to "
                        "replace suspect hardware."
                    ),
                    detail={"bdf": bdf, "correctable": correctable, "fatal": fatal},
                ))

        # IOMMU check -- purely informational on bare-metal GPU nodes
        iommu_enabled = os.path.isdir("/sys/kernel/iommu_groups/") and bool(
            os.listdir("/sys/kernel/iommu_groups/")
        )

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data={"devices": devices, "iommu_enabled": iommu_enabled},
        )

    async def _get_nvidia_smi_pcie(self) -> list[dict[str, int]]:
        """Get PCIe gen/width from nvidia-smi (works without root)."""
        result = await run_command(
            "nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.width.current,"
            "pcie.link.gen.max,pcie.link.width.max --format=csv,noheader,nounits",
            timeout=15.0,
        )
        if not result.success:
            return []

        entries = []
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                try:
                    entries.append({
                        "gen_current": int(parts[0]),
                        "width_current": int(parts[1]),
                        "gen_max": int(parts[2]),
                        "width_max": int(parts[3]),
                    })
                except ValueError:
                    continue
        return entries
