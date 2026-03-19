"""CUDA diagnostics -- DCGM health checks, driver/runtime version validation."""

from __future__ import annotations

import re
from typing import Any

from lit_diag.modules.base import BaseDiagnosticModule, Finding, ModuleResult, Severity
from lit_diag.utils.commands import run_command
from lit_diag.engine.module_loader import register_module

# test names we skip because they fail on healthy systems that just
# don't have dcgm fully configured (persistence mode off, etc)
DCGM_SOFT_FAILURES = {"software", "persistence"}


def _parse_dcgm_tests(output: str) -> list[dict[str, str]]:
    """Parse dcgmi diag output into a list of test results.

    DCGM outputs a table like:
        +---------------------------+------------------------------------------+
        | Diagnostic                | Result                                   |
        +===========================+==========================================+
        | Deployment                | Pass                                     |
        | ...
    We look for real failures -- lines matching '| <test> | Fail' -- but
    skip expected soft failures like 'software' and 'persistence'.
    """
    tests: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line or "===" in line:
            continue
        # strip outer pipes and split on the inner one
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 2:
            continue
        test_name = parts[0].strip().lower()
        result_str = parts[1].strip().lower()
        if not test_name or test_name in ("diagnostic", "test"):
            continue
        if "pass" in result_str:
            tests.append({"test_name": test_name, "result": "pass"})
        elif "fail" in result_str:
            tests.append({"test_name": test_name, "result": "fail"})
        elif "skip" in result_str or "warn" in result_str:
            tests.append({"test_name": test_name, "result": "skip"})
        else:
            tests.append({"test_name": test_name, "result": result_str})
    return tests


def _extract_cuda_versions(smi_output: str) -> tuple[str, str]:
    """Pull driver and CUDA version from nvidia-smi header output.

    nvidia-smi header contains lines like:
        | NVIDIA-SMI 535.129.03   Driver Version: 535.129.03   CUDA Version: 12.2 |
    """
    driver = ""
    cuda = ""
    m = re.search(r"Driver Version:\s*(\S+)", smi_output)
    if m:
        driver = m.group(1)
    m = re.search(r"CUDA Version:\s*(\S+)", smi_output)
    if m:
        cuda = m.group(1)
    return driver, cuda


@register_module
class CUDAModule(BaseDiagnosticModule):
    name = "cuda_tests"
    display_name = "CUDA Tests"
    requires_root = False
    optional_tools = ["dcgmi"]

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        data: dict[str, Any] = {
            "dcgm_available": False,
            "dcgm_tests": [],
            "cuda_driver_version": "",
            "cuda_runtime_version": "",
        }

        # -- DCGM quick health check (level 1) --
        dcgm = await run_command("dcgmi diag -r 1", timeout=120.0)

        if dcgm.success:
            data["dcgm_available"] = True
            tests = _parse_dcgm_tests(dcgm.stdout)
            data["dcgm_tests"] = tests

            real_failures = [
                t for t in tests
                if t["result"] == "fail" and t["test_name"] not in DCGM_SOFT_FAILURES
            ]
            if real_failures:
                failed_names = ", ".join(t["test_name"] for t in real_failures)
                findings.append(Finding(
                    code="dcgm_failure",
                    severity=Severity.CRITICAL,
                    summary=f"DCGM diagnostic test failed: {failed_names}",
                    explanation=(
                        f"One or more GPU hardware validation tests failed "
                        f"({failed_names}). This means a GPU did not pass its "
                        "built-in health check and may have a hardware fault."
                    ),
                    client_action=(
                        "Contact support with this diagnostic report. The "
                        "failing GPU(s) need further investigation."
                    ),
                    engineer_action=(
                        "Check which specific DCGM test failed and correlate "
                        "with ECC error counts and XID data from dmesg. Run "
                        "'dcgmi diag -r 3' for a deeper check if time permits."
                    ),
                    detail={"failed_tests": real_failures},
                ))

        elif dcgm.returncode == 127:
            # dcgmi not installed
            data["dcgm_available"] = False
            findings.append(Finding(
                code="dcgm_not_available",
                severity=Severity.DEGRADED,
                summary="DCGM not installed -- hardware validation limited",
                explanation=(
                    "The NVIDIA Data Center GPU Manager (DCGM) is not "
                    "installed on this node. Without it, we can't run the "
                    "full GPU hardware validation suite."
                ),
                client_action=(
                    "Ask your support team about installing DCGM for "
                    "comprehensive GPU health monitoring."
                ),
                engineer_action=(
                    "Install datacenter-gpu-manager package from NVIDIA "
                    "repos (apt or yum). Ensure nv-hostengine is running."
                ),
                fix_command="__dcgm_install__",
                fix_description="Install NVIDIA DCGM (datacenter-gpu-manager)",
                fix_impact="Enables full GPU hardware validation. No downtime.",
                fix_requires_root=True,
            ))

            # fallback: try basic CUDA check via nvidia-smi
            compute_result = await run_command(
                "nvidia-smi -q -d COMPUTE", timeout=30.0
            )
            if compute_result.success:
                data["compute_info"] = compute_result.stdout[:2000]
        else:
            # dcgm exists but returned an error
            data["dcgm_available"] = True
            data["dcgm_error"] = dcgm.stderr[:500]

        # -- CUDA driver/runtime version check --
        smi_header = await run_command("nvidia-smi", timeout=30.0)
        if smi_header.success:
            driver_ver, cuda_ver = _extract_cuda_versions(smi_header.stdout)
            data["nvidia_driver_version"] = driver_ver
            data["cuda_driver_version"] = cuda_ver
            data["cuda_runtime_version"] = cuda_ver

        # try nvcc for the runtime version if available
        nvcc_result = await run_command("nvcc --version 2>/dev/null", timeout=10.0)
        if nvcc_result.success:
            m = re.search(r"release\s+(\d+\.\d+)", nvcc_result.stdout)
            if m:
                data["cuda_runtime_version"] = m.group(1)

        # compare CUDA version from nvidia-smi header vs nvcc
        drv_cuda = data.get("cuda_driver_version", "")
        rt_cuda = data.get("cuda_runtime_version", "")
        if drv_cuda and rt_cuda and drv_cuda != rt_cuda:
            drv_major = drv_cuda.split(".")[0]
            rt_major = rt_cuda.split(".")[0]
            if drv_major != rt_major:
                findings.append(Finding(
                    code="cuda_version_mismatch",
                    severity=Severity.WARNING,
                    summary=f"CUDA versions differ: driver reports {drv_cuda}, nvcc reports {rt_cuda}",
                    explanation=(
                        f"nvidia-smi reports CUDA {drv_cuda} but the "
                        f"installed CUDA toolkit (nvcc) reports {rt_cuda}. "
                        "This is usually fine -- CUDA maintains backward "
                        "compatibility. Only investigate if you're actually "
                        "seeing CUDA errors in your workloads."
                    ),
                    client_action=(
                        "No action needed unless you're seeing CUDA errors. "
                        "If you are, contact support."
                    ),
                    engineer_action=(
                        "Check 'nvidia-smi' header vs 'nvcc --version'. "
                        "Usually updating the driver or reinstalling the "
                        "matching CUDA toolkit resolves this."
                    ),
                    detail={"driver_cuda": drv_cuda, "runtime_cuda": rt_cuda},
                ))

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data=data,
        )
