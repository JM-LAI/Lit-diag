"""NVIDIA driver diagnostics -- modules, DKMS, persistence, VBIOS."""

from __future__ import annotations

import os
import re
from typing import Any

from lit_diag.modules.base import BaseDiagnosticModule, Finding, ModuleResult, Severity
from lit_diag.utils.commands import run_command
from lit_diag.engine.module_loader import register_module

EXPECTED_MODULES = [
    "nvidia",
    "nvidia_modeset",
    "nvidia_uvm",
    "nvidia_drm",
    "nvidia_peermem",
]


def _parse_lsmod_nvidia(output: str) -> list[str]:
    """Pull loaded nvidia-related module names from lsmod output."""
    loaded: list[str] = []
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("nvidia"):
            loaded.append(parts[0])
    return loaded


def _summarize_vbios(versions: list[str]) -> str:
    """Build a human-readable VBIOS summary.

    If all GPUs match, return 'XX.XX (all N GPUs match)'.
    If there's a mismatch, list each one.
    """
    versions = [v.strip() for v in versions if v.strip()]
    if not versions:
        return "unknown"
    unique = set(versions)
    if len(unique) == 1:
        return f"{versions[0]} (all {len(versions)} GPUs match)"
    lines = [f"GPU {i}: {v}" for i, v in enumerate(versions)]
    return "; ".join(lines)


@register_module
class DriverModule(BaseDiagnosticModule):
    name = "driver"
    display_name = "NVIDIA Driver"
    requires_root = False
    required_tools = ["nvidia-smi"]

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        data: dict[str, Any] = {
            "driver_version": "",
            "cuda_version": "",
            "kernel_version": "",
            "vbios": "",
            "modules": [],
            "persistence_mode": "",
        }

        # -- driver version --
        drv = await run_command(
            "nvidia-smi --query-gpu=driver_version --format=csv,noheader",
            timeout=30.0,
        )
        if drv.success and drv.stdout:
            data["driver_version"] = drv.stdout.splitlines()[0].strip()

        # -- CUDA version from nvidia-smi header --
        smi = await run_command("nvidia-smi", timeout=30.0)
        if smi.success:
            m = re.search(r"CUDA Version:\s*(\S+)", smi.stdout)
            if m:
                data["cuda_version"] = m.group(1)

        # -- kernel version --
        kern = await run_command("uname -r", timeout=5.0)
        if kern.success:
            data["kernel_version"] = kern.stdout.strip()

        # -- VBIOS versions per GPU --
        vbios = await run_command(
            "nvidia-smi --query-gpu=vbios_version --format=csv,noheader",
            timeout=30.0,
        )
        if vbios.success and vbios.stdout:
            versions = vbios.stdout.strip().splitlines()
            data["vbios"] = _summarize_vbios(versions)
            # check for mismatches
            unique = set(v.strip() for v in versions if v.strip())
            if len(unique) > 1:
                findings.append(Finding(
                    code="vbios_mismatch",
                    severity=Severity.WARNING,
                    summary="VBIOS versions differ across GPUs",
                    explanation=(
                        "Not all GPUs are running the same VBIOS firmware. "
                        "Inconsistent firmware can cause subtle behavioral "
                        "differences between GPUs in the same node."
                    ),
                    client_action=(
                        "Contact support about a firmware update to bring "
                        "all GPUs to the same VBIOS version."
                    ),
                    engineer_action=(
                        "Compare VBIOS versions per GPU and check OEM "
                        "release notes for known issues. Flash to the "
                        "latest approved VBIOS for this SKU."
                    ),
                    detail={"versions": list(unique)},
                ))

        # -- kernel modules --
        lsmod = await run_command("lsmod | grep nvidia", timeout=10.0)
        loaded_modules: list[str] = []
        if lsmod.success and lsmod.stdout:
            loaded_modules = _parse_lsmod_nvidia(lsmod.stdout)
        data["modules"] = loaded_modules

        if not loaded_modules:
            findings.append(Finding(
                code="driver_not_loaded",
                severity=Severity.CRITICAL,
                summary="NVIDIA driver is not loaded",
                explanation=(
                    "No nvidia kernel modules are present in lsmod. The "
                    "GPU driver isn't running, so GPUs are completely "
                    "inaccessible to applications."
                ),
                client_action="Contact support immediately.",
                engineer_action=(
                    "Check 'dkms status' for build errors. Try "
                    "'modprobe nvidia' and check dmesg for failures. "
                    "May need driver reinstall."
                ),
            ))
        else:
            for mod in EXPECTED_MODULES:
                if mod not in loaded_modules:
                    findings.append(Finding(
                        code="module_missing",
                        severity=Severity.WARNING,
                        summary=f"NVIDIA kernel module '{mod}' not loaded",
                        explanation=(
                            f"The '{mod}' kernel module is expected on GPU "
                            "nodes but isn't loaded. Some GPU functionality "
                            "may be unavailable."
                        ),
                        client_action=(
                            "This can usually be fixed with a quick command."
                        ),
                        engineer_action=(
                            f"Try 'modprobe {mod}' and check dmesg. "
                            "Verify 'dkms status' shows the module built "
                            "for the running kernel."
                        ),
                        detail={"module": mod},
                        fix_command=f"modprobe {mod}",
                        fix_description=f"Load the {mod} kernel module",
                        fix_impact="Enables additional GPU functionality. No downtime, no restart needed.",
                        fix_requires_root=True,
                    ))

        # -- module parameters (best-effort, read from sysfs) --
        params_dir = "/sys/module/nvidia/parameters"
        if os.path.isdir(params_dir):
            params: dict[str, str] = {}
            try:
                for name in os.listdir(params_dir):
                    fpath = os.path.join(params_dir, name)
                    try:
                        with open(fpath) as fh:
                            params[name] = fh.read().strip()
                    except (OSError, PermissionError):
                        continue
                data["module_parameters"] = params
            except OSError:
                pass

        # -- DKMS status --
        dkms = await run_command("dkms status 2>/dev/null | grep nvidia", timeout=10.0)
        if dkms.success and dkms.stdout:
            data["dkms_status"] = dkms.stdout.strip()

        # -- NVIDIA installer log (last 20 lines, look for errors) --
        installer_log = "/var/log/nvidia-installer.log"
        if os.path.isfile(installer_log):
            try:
                with open(installer_log) as fh:
                    lines = fh.readlines()
                tail = lines[-20:] if len(lines) > 20 else lines
                log_text = "".join(tail)
                data["installer_log_tail"] = log_text
                error_lines = [
                    l.strip() for l in tail
                    if re.search(r"error|fail|abort", l, re.IGNORECASE)
                ]
                if error_lines:
                    data["installer_log_errors"] = error_lines
            except (OSError, PermissionError):
                pass

        # -- persistence mode --
        pm = await run_command(
            "nvidia-smi --query-gpu=persistence_mode --format=csv,noheader",
            timeout=15.0,
        )
        if pm.success and pm.stdout:
            modes = [l.strip() for l in pm.stdout.splitlines() if l.strip()]
            if modes:
                data["persistence_mode"] = modes[0]
                if any(m.lower() == "disabled" for m in modes):
                    findings.append(Finding(
                        code="persistence_mode_off",
                        severity=Severity.WARNING,
                        summary="GPU persistence mode is disabled",
                        explanation=(
                            "When persistence mode is off, the NVIDIA driver "
                            "unloads between GPU jobs, causing slow "
                            "re-initialization (~1-3s) on the next CUDA call. "
                            "This adds latency and can cause timeout issues."
                        ),
                        client_action=(
                            "This is a quick fix that improves GPU startup times."
                        ),
                        engineer_action=(
                            "Run 'nvidia-smi -pm 1' to enable persistence "
                            "mode. For it to survive reboots, add an "
                            "nvidia-persistenced systemd service."
                        ),
                        fix_command="nvidia-smi -pm 1",
                        fix_description="Enable GPU persistence mode for all GPUs",
                        fix_impact="Faster GPU initialization. No downtime, no restart needed.",
                        fix_requires_root=True,
                    ))

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data=data,
        )
