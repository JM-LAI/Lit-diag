"""Thermal & IPMI diagnostics -- CPU temps, fans, PSU, system event log."""

from __future__ import annotations

import json
import re
from typing import Any

from lit_diag.modules.base import BaseDiagnosticModule, Finding, ModuleResult, Severity
from lit_diag.utils.commands import run_command
from lit_diag.engine.module_loader import register_module


def _safe_float(value: str, default: float = 0.0) -> float:
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in ("n/a", "na", "not available", "disabled"):
        return default
    cleaned = re.sub(r"[^\d.\-]", "", cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return default


def _parse_ipmi_sdr_line(line: str) -> dict[str, Any] | None:
    """Parse a single IPMI SDR output line.

    Typical format: "Sensor Name   | hex | ok/cr/ns | entity | value"
    Some lines have fewer pipes depending on sensor type.
    """
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 5:
        return None

    name = parts[0]
    raw_status = parts[2].lower()
    value_str = parts[4] if len(parts) > 4 else ""

    # figure out the unit from the value string
    unit = ""
    numeric_val = 0.0
    if "degrees" in value_str.lower() or "celsius" in value_str.lower():
        unit = "C"
        numeric_val = _safe_float(value_str)
    elif "rpm" in value_str.lower():
        unit = "RPM"
        numeric_val = _safe_float(value_str)
    elif "watts" in value_str.lower():
        unit = "W"
        numeric_val = _safe_float(value_str)
    elif "volts" in value_str.lower():
        unit = "V"
        numeric_val = _safe_float(value_str)
    else:
        numeric_val = _safe_float(value_str)

    status = "ok"
    if "cr" in raw_status or "critical" in raw_status:
        status = "critical"
    elif "nc" in raw_status or "non-critical" in raw_status:
        status = "warning"
    elif "ns" in raw_status or "no reading" in raw_status:
        status = "unknown"

    return {
        "name": name,
        "value": numeric_val,
        "unit": unit,
        "status": status,
        "raw_value": value_str,
    }


def _parse_sel_line(line: str) -> dict[str, str] | None:
    """Parse an IPMI SEL entry line.

    Format varies but typically: "ID | Date | Time | Sensor | Event | Direction"
    """
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 4:
        return None
    timestamp = f"{parts[1]} {parts[2]}" if len(parts) > 2 else parts[1]
    event_type = parts[3] if len(parts) > 3 else ""
    message = " | ".join(parts[4:]) if len(parts) > 4 else ""
    return {
        "timestamp": timestamp,
        "type": event_type,
        "message": message,
    }


@register_module
class ThermalModule(BaseDiagnosticModule):
    name = "thermal"
    display_name = "Thermal / Power"
    requires_root = True
    optional_tools = ["ipmitool", "sensors"]

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        sensors_list: list[dict[str, Any]] = []
        sel_entries: list[dict[str, str]] = []
        data: dict[str, Any] = {"sensors": sensors_list, "sel_entries": sel_entries}

        # -- IPMI temperature sensors --
        temp_result = await run_command(
            "ipmitool sdr type Temperature", timeout=15.0
        )
        if temp_result.success and temp_result.stdout:
            for line in temp_result.stdout.splitlines():
                parsed = _parse_ipmi_sdr_line(line)
                if not parsed:
                    continue
                parsed["unit"] = parsed.get("unit") or "C"
                sensors_list.append(parsed)
                self._check_temperature(parsed, findings)

        # -- IPMI fan sensors --
        fan_result = await run_command("ipmitool sdr type Fan", timeout=15.0)
        if fan_result.success and fan_result.stdout:
            for line in fan_result.stdout.splitlines():
                parsed = _parse_ipmi_sdr_line(line)
                if not parsed:
                    continue
                parsed["unit"] = parsed.get("unit") or "RPM"
                sensors_list.append(parsed)
                self._check_fan(parsed, findings)

        # -- IPMI PSU sensors --
        psu_result = await run_command(
            'ipmitool sdr type "Power Supply"', timeout=15.0
        )
        if psu_result.success and psu_result.stdout:
            for line in psu_result.stdout.splitlines():
                parsed = _parse_ipmi_sdr_line(line)
                if not parsed:
                    continue
                sensors_list.append(parsed)
                self._check_psu(parsed, findings)

        # -- lm-sensors fallback/supplement --
        await self._collect_lm_sensors(sensors_list, findings)

        # -- IPMI SEL (last 20 events) --
        sel_result = await run_command(
            "ipmitool sel list last 20", timeout=15.0
        )
        if sel_result.success and sel_result.stdout:
            for line in sel_result.stdout.splitlines():
                entry = _parse_sel_line(line)
                if entry:
                    sel_entries.append(entry)

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data=data,
        )

    def _check_temperature(
        self, sensor: dict[str, Any], findings: list[Finding]
    ) -> None:
        """Evaluate a temperature sensor reading and generate findings."""
        name = sensor["name"].lower()
        temp = sensor["value"]
        status = sensor["status"]

        if temp <= 0:
            return

        # inlet / ambient temperature
        if any(kw in name for kw in ("inlet", "ambient", "intake")):
            if temp > 40:
                findings.append(Finding(
                    code="inlet_temp_high",
                    severity=Severity.CRITICAL,
                    summary=f"Inlet temperature critically high ({temp}°C)",
                    explanation=(
                        f"The ambient/inlet temperature sensor '{sensor['name']}' "
                        f"reads {temp}°C, which is above the 40°C critical threshold. "
                        "Datacenter cooling may be insufficient."
                    ),
                    client_action="Contact DC operations about cooling in this rack/row.",
                    engineer_action="Check CRAC units, airflow, blanking panels. Verify neighboring nodes.",
                    detail={"sensor": sensor["name"], "value": temp},
                ))
            elif temp > 35:
                findings.append(Finding(
                    code="inlet_temp_high",
                    severity=Severity.WARNING,
                    summary=f"Inlet temperature elevated ({temp}°C)",
                    explanation=(
                        f"The ambient/inlet temperature sensor '{sensor['name']}' "
                        f"reads {temp}°C, above the 35°C warning threshold. "
                        "Datacenter cooling might be marginal."
                    ),
                    client_action="Contact DC operations about cooling if temperatures persist.",
                    engineer_action="Monitor trend. Check airflow and neighboring node temps.",
                    detail={"sensor": sensor["name"], "value": temp},
                ))
            return

        # CPU temperature
        if any(kw in name for kw in ("cpu", "processor", "die")):
            if temp > 95:
                findings.append(Finding(
                    code="cpu_temp_high",
                    severity=Severity.CRITICAL,
                    summary=f"CPU temperature critically high ({temp}°C)",
                    explanation=(
                        f"Sensor '{sensor['name']}' reads {temp}°C, exceeding "
                        "the 95°C critical threshold. The CPU may throttle or "
                        "the system may shut down to prevent damage."
                    ),
                    client_action="Contact support -- workloads may be affected by thermal throttling.",
                    engineer_action="Check heatsink seating, thermal paste, fan speeds. May need ILO power cycle.",
                    detail={"sensor": sensor["name"], "value": temp},
                ))
            elif temp > 85:
                findings.append(Finding(
                    code="cpu_temp_high",
                    severity=Severity.WARNING,
                    summary=f"CPU temperature elevated ({temp}°C)",
                    explanation=(
                        f"Sensor '{sensor['name']}' reads {temp}°C, above the "
                        "85°C warning threshold. Not critical yet but worth watching."
                    ),
                    client_action="Monitor workload temperatures. Contact support if it persists.",
                    engineer_action="Check fan speeds, heatsink condition, ambient temperature.",
                    detail={"sensor": sensor["name"], "value": temp},
                ))

        # ipmi-reported critical status overrides our thresholds
        if status == "critical" and not any(
            f.detail.get("sensor") == sensor["name"] for f in findings
        ):
            findings.append(Finding(
                code="cpu_temp_high",
                severity=Severity.CRITICAL,
                summary=f"Sensor '{sensor['name']}' in critical state ({temp}°C)",
                explanation=(
                    f"The BMC reports sensor '{sensor['name']}' at {temp}°C "
                    "has crossed its critical threshold."
                ),
                client_action="Contact support about thermal issues on this node.",
                engineer_action="Check BMC thresholds and physical cooling components.",
                detail={"sensor": sensor["name"], "value": temp},
            ))

    def _check_fan(
        self, sensor: dict[str, Any], findings: list[Finding]
    ) -> None:
        """Check fan sensor for failure."""
        status = sensor["status"]
        rpm = sensor["value"]
        name_lower = sensor["name"].lower()

        # skip IPMI meta-sensors like "Fan Redundancy", "PS Redundancy"
        if "redundancy" in name_lower or "status" in name_lower:
            return

        if status == "critical" or (rpm == 0 and status != "unknown"):
            findings.append(Finding(
                code="fan_failed",
                severity=Severity.CRITICAL,
                summary=f"Fan failure detected: {sensor['name']}",
                explanation=(
                    f"Fan '{sensor['name']}' reports {int(rpm)} RPM with status "
                    f"'{status}'. This fan module has likely failed, reducing "
                    "cooling capacity."
                ),
                client_action="Contact DC operations -- a fan module needs replacement.",
                engineer_action=(
                    f"Check fan module '{sensor['name']}'. Verify redundant fans "
                    "are compensating. Monitor component temperatures."
                ),
                detail={"sensor": sensor["name"], "rpm": rpm},
            ))

    def _check_psu(
        self, sensor: dict[str, Any], findings: list[Finding]
    ) -> None:
        """Check power supply sensor for issues."""
        status = sensor["status"]
        name = sensor["name"]

        if status in ("critical", "warning"):
            findings.append(Finding(
                code="psu_failure",
                severity=Severity.CRITICAL,
                summary=f"Power supply issue: {name}",
                explanation=(
                    f"Power supply sensor '{name}' reports status '{status}'. "
                    "A PSU module may have failed or be operating outside spec."
                ),
                client_action="Contact DC operations -- a power supply module needs attention.",
                engineer_action=(
                    f"Check PSU module '{name}'. Verify PSU redundancy is intact. "
                    "Check input power and cabling."
                ),
                detail={"sensor": name, "status": status},
            ))

    async def _collect_lm_sensors(
        self,
        sensors_list: list[dict[str, Any]],
        findings: list[Finding],
    ) -> None:
        """Collect data from lm-sensors as a supplement to IPMI."""
        json_result = await run_command("sensors -j", timeout=10.0)
        if json_result.success and json_result.stdout:
            try:
                data = json.loads(json_result.stdout)
                self._walk_sensors_json(data, sensors_list, findings)
                return
            except json.JSONDecodeError:
                pass

        # fallback to plain text
        plain_result = await run_command("sensors", timeout=10.0)
        if plain_result.success and plain_result.stdout:
            self._parse_sensors_plain(plain_result.stdout, sensors_list, findings)

    def _walk_sensors_json(
        self,
        data: dict[str, Any],
        sensors_list: list[dict[str, Any]],
        findings: list[Finding],
    ) -> None:
        """Walk the nested sensors -j output and extract temperature readings."""
        for chip_name, chip_data in data.items():
            if not isinstance(chip_data, dict):
                continue
            for sensor_name, sensor_data in chip_data.items():
                if not isinstance(sensor_data, dict):
                    continue
                for key, value in sensor_data.items():
                    if "input" in key.lower() and "temp" in sensor_name.lower():
                        temp = _safe_float(str(value))
                        if temp > 0:
                            entry = {
                                "name": f"{chip_name}/{sensor_name}",
                                "value": temp,
                                "unit": "C",
                                "status": "ok",
                                "source": "lm-sensors",
                            }
                            sensors_list.append(entry)
                            self._check_temperature(entry, findings)

    def _parse_sensors_plain(
        self,
        output: str,
        sensors_list: list[dict[str, Any]],
        findings: list[Finding],
    ) -> None:
        """Parse plain text sensors output for temperature lines."""
        current_chip = ""
        for line in output.splitlines():
            if not line.startswith(" ") and ":" not in line and line.strip():
                current_chip = line.strip()
                continue
            temp_match = re.match(
                r"^(\S[^:]+):\s*\+?([\d.]+)\s*°?C", line
            )
            if temp_match:
                name = temp_match.group(1).strip()
                temp = _safe_float(temp_match.group(2))
                if temp > 0:
                    full_name = f"{current_chip}/{name}" if current_chip else name
                    entry = {
                        "name": full_name,
                        "value": temp,
                        "unit": "C",
                        "status": "ok",
                        "source": "lm-sensors",
                    }
                    sensors_list.append(entry)
                    self._check_temperature(entry, findings)
