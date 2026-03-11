"""Report generation helpers."""

from __future__ import annotations

from lit_diag.modules.base import DiagnosticReport
from lit_diag.output.formatters import report_to_json, save_report_json


def generate_filename(report: DiagnosticReport) -> str:
    """Generate a default report filename."""
    ts = report.timestamp[:19].replace(":", "-").replace("T", "_")
    hostname = report.hostname or "unknown"
    return f"lit-diag-{hostname}-{ts}.json"
