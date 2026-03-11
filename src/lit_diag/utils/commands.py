"""Subprocess wrapper with timeouts and friendly error handling."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import Optional


@dataclass
class CommandResult:
    """Result of a subprocess execution."""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    command: str = ""

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out


TIMEOUT_MESSAGES = {
    "nvidia-smi": (
        "The GPU driver is not responding. This usually means the "
        "driver is frozen or a GPU has crashed."
    ),
    "lspci": "PCIe device enumeration timed out. The system bus may be in a bad state.",
    "ipmitool": "BMC communication timed out. The IPMI interface may be unresponsive.",
    "nvme": "NVMe command timed out. A storage device may be unresponsive.",
}

PERMISSION_MESSAGES = {
    "nvidia-smi": "GPU queries need appropriate permissions. Try running with sudo.",
    "ipmitool": "IPMI access requires root. Run with sudo for thermal sensor data.",
    "dmesg": "Reading kernel logs requires root. Run with sudo to see log entries.",
    "nvme": "NVMe SMART data requires root. Run with sudo for storage health.",
}


def _friendly_timeout_msg(cmd: str) -> str:
    """Get a user-friendly timeout message based on the command."""
    for tool, msg in TIMEOUT_MESSAGES.items():
        if tool in cmd:
            return msg
    return f"Command timed out: {cmd}"


def _friendly_permission_msg(cmd: str) -> str:
    """Get a user-friendly permission denied message."""
    for tool, msg in PERMISSION_MESSAGES.items():
        if tool in cmd:
            return msg
    return "This check needs root access. Run with sudo for full results."


async def run_command(
    cmd: str,
    timeout: float = 30.0,
    shell: bool = True,
) -> CommandResult:
    """Run a command asynchronously with timeout and error handling."""
    try:
        if shell:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            args = shlex.split(cmd)
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return CommandResult(
                stdout="",
                stderr=_friendly_timeout_msg(cmd),
                returncode=124,
                timed_out=True,
                command=cmd,
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        return CommandResult(
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode or 0,
            command=cmd,
        )

    except PermissionError:
        return CommandResult(
            stdout="",
            stderr=_friendly_permission_msg(cmd),
            returncode=126,
            command=cmd,
        )
    except FileNotFoundError:
        tool = cmd.split()[0] if cmd else cmd
        return CommandResult(
            stdout="",
            stderr=f"{tool} is not installed or not in PATH.",
            returncode=127,
            command=cmd,
        )
    except Exception as e:
        return CommandResult(
            stdout="",
            stderr=str(e),
            returncode=1,
            command=cmd,
        )


def run_command_sync(
    cmd: str,
    timeout: float = 30.0,
) -> CommandResult:
    """Synchronous wrapper for run_command."""
    import subprocess

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            returncode=result.returncode,
            command=cmd,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            stdout="",
            stderr=_friendly_timeout_msg(cmd),
            returncode=124,
            timed_out=True,
            command=cmd,
        )
    except FileNotFoundError:
        tool = cmd.split()[0] if cmd else cmd
        return CommandResult(
            stdout="",
            stderr=f"{tool} is not installed or not in PATH.",
            returncode=127,
            command=cmd,
        )
    except Exception as e:
        return CommandResult(
            stdout="",
            stderr=str(e),
            returncode=1,
            command=cmd,
        )
