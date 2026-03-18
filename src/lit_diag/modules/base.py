"""Base classes for all diagnostic modules."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    DEGRADED = "degraded"
    ERROR = "error"


class Finding(BaseModel):
    """A single diagnostic finding -- the core unit of output."""

    code: str
    severity: Severity
    summary: str
    explanation: str
    client_action: str
    engineer_action: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    fix_command: str = ""
    fix_description: str = ""
    fix_impact: str = ""
    fix_requires_root: bool = False


class DependencyStatus(BaseModel):
    """Status of external tool dependencies for a module."""

    available: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    degraded: bool = False
    message: str = ""


class ModuleResult(BaseModel):
    """Result from a single diagnostic module."""

    module_name: str
    status: Severity = Severity.OK
    findings: list[Finding] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    deps: Optional[DependencyStatus] = None
    duration_ms: float = 0.0
    error_message: str = ""

    def worst_severity(self) -> Severity:
        """Roll up to the worst severity across all findings."""
        priority = {
            Severity.OK: 0,
            Severity.DEGRADED: 1,
            Severity.WARNING: 2,
            Severity.CRITICAL: 3,
            Severity.ERROR: 4,
        }
        worst = self.status
        for f in self.findings:
            if priority.get(f.severity, 0) > priority.get(worst, 0):
                worst = f.severity
        return worst


class DiagnosticReport(BaseModel):
    """Full report from all modules -- the top-level output."""

    tool: str = "Lit-Diag by Lightning AI"
    version: str = "0.1.0"
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    hostname: str = ""
    overall_status: Severity = Severity.OK
    degraded: bool = False
    degraded_codes: list[str] = Field(default_factory=list)
    summary: str = ""
    modules: dict[str, ModuleResult] = Field(default_factory=dict)
    duration_ms: float = 0.0

    def roll_up(self) -> None:
        """Compute overall status and degraded codes from module results."""
        priority = {
            Severity.OK: 0,
            Severity.DEGRADED: 1,
            Severity.WARNING: 2,
            Severity.CRITICAL: 3,
            Severity.ERROR: 4,
        }
        worst = Severity.OK
        codes: list[str] = []
        finding_summaries: list[str] = []

        for result in self.modules.values():
            sev = result.worst_severity()
            result.status = sev
            if priority.get(sev, 0) > priority.get(worst, 0):
                worst = sev
            for f in result.findings:
                if f.severity in (Severity.DEGRADED, Severity.WARNING, Severity.CRITICAL, Severity.ERROR):
                    codes.append(f.code)
                    finding_summaries.append(f.summary)
            # modules that are degraded/worse but have no findings still count
            if sev != Severity.OK and not result.findings:
                codes.append(f"{result.module_name}_degraded")
                msg = result.error_message or f"{result.module_name} is {sev.value}"
                finding_summaries.append(msg)

        self.overall_status = worst
        self.degraded = worst in (Severity.DEGRADED, Severity.WARNING, Severity.CRITICAL, Severity.ERROR)
        self.degraded_codes = codes

        if finding_summaries:
            count = len(finding_summaries)
            noun = "issue" if count == 1 else "issues"
            top = ". ".join(finding_summaries[:3])
            self.summary = f"{count} {noun} found. {top}."
        else:
            self.summary = "All checks passed. No issues detected."


class BaseDiagnosticModule(ABC):
    """Base class all diagnostic modules inherit from."""

    name: str = "unnamed"
    display_name: str = "Unnamed Check"
    requires_root: bool = False
    required_tools: list[str] = []
    optional_tools: list[str] = []

    def check_deps(self) -> DependencyStatus:
        """Check which external tools are available."""
        from lit_diag.utils.deps import check_tools

        available, missing = check_tools(
            self.required_tools + self.optional_tools
        )
        required_missing = [t for t in self.required_tools if t in missing]
        degraded = len(required_missing) > 0

        msg = ""
        if required_missing:
            msg = f"Missing required tools: {', '.join(required_missing)}"
        elif missing:
            msg = f"Missing optional tools: {', '.join(missing)}. Some data may be unavailable."

        return DependencyStatus(
            available=available,
            missing=missing,
            degraded=degraded,
            message=msg,
        )

    @abstractmethod
    async def collect(self) -> ModuleResult:
        """Run the diagnostic and return results."""
        ...

    async def run(self) -> ModuleResult:
        """Execute the module with timing, dep checks, and error handling."""
        import time

        start = time.monotonic()
        deps = self.check_deps()

        if deps.degraded:
            return ModuleResult(
                module_name=self.name,
                status=Severity.DEGRADED,
                deps=deps,
                error_message=deps.message,
                duration_ms=(time.monotonic() - start) * 1000,
            )

        try:
            result = await self.collect()
            result.deps = deps
            result.module_name = self.name
            result.duration_ms = (time.monotonic() - start) * 1000
            result.status = result.worst_severity()
            return result
        except Exception as e:
            return ModuleResult(
                module_name=self.name,
                status=Severity.ERROR,
                deps=deps,
                error_message=f"Module failed: {e}",
                duration_ms=(time.monotonic() - start) * 1000,
            )
