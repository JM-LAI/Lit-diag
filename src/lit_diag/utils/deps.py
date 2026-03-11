"""Optional tool detection -- check what's available on this system."""

from __future__ import annotations

import shutil
from typing import Tuple


ALL_TOOLS = {
    "nvidia-smi": "NVIDIA GPU management and monitoring",
    "lspci": "PCI device enumeration",
    "setpci": "PCI configuration space access",
    "dmidecode": "System hardware information",
    "ipmitool": "BMC/IPMI sensor and event log access",
    "sensors": "Hardware sensor readings (lm-sensors)",
    "nvme": "NVMe device management (nvme-cli)",
    "smartctl": "SMART disk health data (smartmontools)",
    "mdadm": "MD RAID management",
    "ibstat": "InfiniBand device status",
    "perfquery": "InfiniBand performance counters",
    "dcgmi": "NVIDIA Data Center GPU Manager",
    "dmesg": "Kernel ring buffer",
    "journalctl": "Systemd journal logs",
}

INSTALL_HINTS = {
    "ipmitool": "apt install ipmitool  (or yum install ipmitool)",
    "sensors": "apt install lm-sensors  (or yum install lm_sensors)",
    "nvme": "apt install nvme-cli  (or yum install nvme-cli)",
    "smartctl": "apt install smartmontools  (or yum install smartmontools)",
    "mdadm": "apt install mdadm  (or yum install mdadm)",
    "ibstat": "apt install infiniband-diags  (or yum install infiniband-diags)",
    "perfquery": "apt install infiniband-diags  (or yum install infiniband-diags)",
    "dcgmi": "See NVIDIA DCGM docs: https://developer.nvidia.com/dcgm",
    "lspci": "apt install pciutils  (or yum install pciutils)",
    "setpci": "apt install pciutils  (or yum install pciutils)",
    "dmidecode": "apt install dmidecode  (or yum install dmidecode)",
}


def check_tool(name: str) -> bool:
    """Check if a single tool is available in PATH."""
    return shutil.which(name) is not None


def check_tools(tools: list[str]) -> Tuple[list[str], list[str]]:
    """Check a list of tools. Returns (available, missing)."""
    available = []
    missing = []
    for tool in tools:
        if check_tool(tool):
            available.append(tool)
        else:
            missing.append(tool)
    return available, missing


def get_all_tool_status() -> dict[str, dict]:
    """Get status of all known tools. Used by `lit-diag deps`."""
    result = {}
    for tool, description in ALL_TOOLS.items():
        installed = check_tool(tool)
        entry = {
            "installed": installed,
            "description": description,
        }
        if not installed and tool in INSTALL_HINTS:
            entry["install_hint"] = INSTALL_HINTS[tool]
        result[tool] = entry
    return result
