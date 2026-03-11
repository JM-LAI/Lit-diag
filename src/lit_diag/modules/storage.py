"""Storage & NVMe diagnostics -- device health, wear, RAID, filesystem checks."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from lit_diag.modules.base import BaseDiagnosticModule, Finding, ModuleResult, Severity
from lit_diag.utils.commands import run_command
from lit_diag.engine.module_loader import register_module


def _safe_int(value: str, default: int = 0) -> int:
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in ("n/a", "not found", "unknown"):
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default


def _safe_float(value: str, default: float = 0.0) -> float:
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in ("n/a", "not found", "unknown"):
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def _parse_df_line(line: str) -> dict[str, Any] | None:
    """Parse a single df -h output line into a dict."""
    parts = line.split()
    if len(parts) < 6:
        return None
    use_pct = parts[4].rstrip("%")
    return {
        "filesystem": parts[0],
        "size": parts[1],
        "used": parts[2],
        "available": parts[3],
        "use_pct": _safe_int(use_pct),
        "mountpoint": parts[5],
    }


@register_module
class StorageModule(BaseDiagnosticModule):
    name = "storage"
    display_name = "Storage"
    requires_root = True
    optional_tools = ["nvme", "smartctl", "mdadm"]

    async def collect(self) -> ModuleResult:
        findings: list[Finding] = []
        devices: list[dict[str, Any]] = []
        filesystems: list[dict[str, Any]] = []
        data: dict[str, Any] = {"devices": devices, "filesystems": filesystems}

        # -- NVMe device enumeration --
        nvme_devs: list[dict[str, Any]] = []
        nvme_json = await run_command("nvme list -o json", timeout=15.0)
        if nvme_json.success and nvme_json.stdout:
            try:
                parsed = json.loads(nvme_json.stdout)
                nvme_devs = parsed.get("Devices", [])
            except (json.JSONDecodeError, KeyError):
                pass

        if not nvme_devs:
            # fallback to plain text nvme list
            nvme_plain = await run_command("nvme list", timeout=15.0)
            if nvme_plain.success and nvme_plain.stdout:
                for line in nvme_plain.stdout.splitlines():
                    if line.startswith("/dev/nvme"):
                        parts = line.split()
                        nvme_devs.append({
                            "DevicePath": parts[0] if parts else "",
                            "ModelNumber": " ".join(parts[2:4]) if len(parts) > 3 else "",
                            "PhysicalSize": parts[-2] if len(parts) > 5 else "",
                        })

        # -- SMART data per NVMe device --
        for dev_info in nvme_devs:
            dev_path = dev_info.get("DevicePath", "")
            if not dev_path:
                continue

            model = dev_info.get("ModelNumber", "unknown")
            size = dev_info.get("PhysicalSize", "unknown")
            if isinstance(size, (int, float)):
                size = f"{size / 1e12:.1f} TB" if size > 1e9 else str(size)

            dev_entry: dict[str, Any] = {
                "device": dev_path,
                "model": model,
                "size": size,
                "health": "ok",
                "wear_pct": 0,
                "spare_pct": 100,
                "temp": 0,
                "media_errors": 0,
            }

            smart = await self._get_smart_data(dev_path)

            if smart:
                crit_warn = smart.get("critical_warning", 0)
                temp_c = smart.get("temperature", 0)
                avail_spare = smart.get("avail_spare", 100)
                pct_used = smart.get("percent_used", 0)
                media_errs = smart.get("media_errors", 0)
                err_log = smart.get("num_err_log_entries", 0)

                dev_entry.update({
                    "health": "critical" if crit_warn else "ok",
                    "wear_pct": pct_used,
                    "spare_pct": avail_spare,
                    "temp": temp_c,
                    "media_errors": media_errs,
                    "error_log_entries": err_log,
                    "critical_warning": crit_warn,
                })

                dev_label = f"{dev_path} ({model})"

                if crit_warn:
                    findings.append(Finding(
                        code="nvme_critical_warning",
                        severity=Severity.CRITICAL,
                        summary=f"NVMe critical warning flags set on {dev_path}",
                        explanation=(
                            f"Storage device {dev_label} is reporting a critical "
                            "condition via its warning register. This can indicate "
                            "imminent failure, overheating, or backup device issues."
                        ),
                        client_action="Contact support about storage health on this node.",
                        engineer_action=(
                            f"Check specific warning bits: {crit_warn:#x}. "
                            "Bit 0=spare, 1=temp, 2=reliability, 3=read-only, 4=backup."
                        ),
                        detail={"device": dev_path, "warning_bits": crit_warn},
                    ))

                if pct_used > 95:
                    findings.append(Finding(
                        code="nvme_wear_high",
                        severity=Severity.CRITICAL,
                        summary=f"NVMe drive {dev_path} is critically worn ({pct_used}% used)",
                        explanation=(
                            f"Drive {dev_label} has consumed {pct_used}% of its rated "
                            "write endurance. It is approaching or past end of life."
                        ),
                        client_action="Plan for immediate drive replacement.",
                        engineer_action="Check write patterns and schedule replacement ASAP.",
                        detail={"device": dev_path, "percent_used": pct_used},
                    ))
                elif pct_used > 80:
                    findings.append(Finding(
                        code="nvme_wear_high",
                        severity=Severity.WARNING,
                        summary=f"NVMe drive {dev_path} wear level elevated ({pct_used}%)",
                        explanation=(
                            f"Drive {dev_label} has consumed {pct_used}% of its rated "
                            "write endurance. It's approaching end of life."
                        ),
                        client_action="Plan for drive replacement in the near future.",
                        engineer_action="Check write patterns and compare against expected fleet lifetime.",
                        detail={"device": dev_path, "percent_used": pct_used},
                    ))

                if avail_spare < 20:
                    findings.append(Finding(
                        code="nvme_spare_low",
                        severity=Severity.WARNING,
                        summary=f"NVMe spare capacity low on {dev_path} ({avail_spare}%)",
                        explanation=(
                            f"Drive {dev_label} has only {avail_spare}% spare blocks "
                            "remaining. The drive has reduced redundancy for bad block "
                            "replacement."
                        ),
                        client_action="Schedule a replacement for this drive.",
                        engineer_action="Monitor spare trend and correlate with wear percentage.",
                        detail={"device": dev_path, "avail_spare": avail_spare},
                    ))

                if media_errs > 0:
                    findings.append(Finding(
                        code="nvme_media_errors",
                        severity=Severity.WARNING,
                        summary=f"Media errors detected on {dev_path} ({media_errs} errors)",
                        explanation=(
                            f"Drive {dev_label} has {media_errs} physical storage "
                            "errors in its media error log. These indicate actual "
                            "defects on the storage media."
                        ),
                        client_action="Contact support about storage reliability on this node.",
                        engineer_action=(
                            f"Check error log details: nvme error-log {dev_path}. "
                            "Correlate with dmesg for I/O errors."
                        ),
                        detail={"device": dev_path, "media_errors": media_errs},
                    ))

            devices.append(dev_entry)

        # -- MD RAID status --
        if os.path.exists("/proc/mdstat"):
            md_result = await run_command("cat /proc/mdstat", timeout=5.0)
            if md_result.success and md_result.stdout:
                data["mdstat"] = md_result.stdout
                if re.search(r"\[.*_.*\]", md_result.stdout):
                    # underscores in the bitmap pattern means a missing/failed drive
                    degraded_arrays = re.findall(
                        r"^(md\d+)\s+:", md_result.stdout, re.MULTILINE
                    )
                    for arr in degraded_arrays:
                        findings.append(Finding(
                            code="raid_degraded",
                            severity=Severity.CRITICAL,
                            summary=f"RAID array {arr} is degraded",
                            explanation=(
                                f"RAID array /dev/{arr} is operating with reduced "
                                "redundancy -- at least one member drive has failed "
                                "or been removed."
                            ),
                            client_action="Contact support immediately -- data redundancy is compromised.",
                            engineer_action=(
                                f"Identify failed drive with mdadm --detail /dev/{arr}. "
                                "Check if rebuild is in progress. Replace failed member."
                            ),
                            detail={"array": arr},
                        ))

        # -- LVM layout (informational) --
        lvm_result = await run_command(
            "lvs --noheadings -o lv_name,vg_name,lv_size 2>/dev/null", timeout=10.0
        )
        if lvm_result.success and lvm_result.stdout:
            lvm_vols: list[dict[str, str]] = []
            for line in lvm_result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    lvm_vols.append({
                        "lv_name": parts[0],
                        "vg_name": parts[1],
                        "lv_size": parts[2],
                    })
            data["lvm_volumes"] = lvm_vols

        # -- Filesystem health: read-only remounts --
        mount_result = await run_command("mount", timeout=5.0)
        if mount_result.success and mount_result.stdout:
            for line in mount_result.stdout.splitlines():
                # look for (ro, ...) which indicates read-only remount
                if re.search(r"\(\s*ro[,\)]", line) and "/snap/" not in line:
                    # snap mounts are always ro, skip those
                    mount_match = re.search(r"on\s+(\S+)\s+type", line)
                    mountpoint = mount_match.group(1) if mount_match else "unknown"
                    if mountpoint in ("/", "/home", "/var", "/tmp", "/opt"):
                        findings.append(Finding(
                            code="fs_readonly",
                            severity=Severity.CRITICAL,
                            summary=f"Filesystem {mountpoint} remounted read-only",
                            explanation=(
                                f"The filesystem at {mountpoint} has been remounted "
                                "read-only, likely due to storage errors triggering "
                                "the kernel's filesystem protection mechanism."
                            ),
                            client_action="Contact support immediately -- the node has storage issues.",
                            engineer_action=(
                                f"Check dmesg for I/O errors and filesystem journal "
                                f"issues. Run fsck on the underlying device for {mountpoint}."
                            ),
                            detail={"mountpoint": mountpoint},
                        ))

        # -- Disk usage (all real partitions, not just / and /home) --
        df_result = await run_command(
            "df -h -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null",
            timeout=5.0,
        )
        if df_result.success and df_result.stdout:
            seen_mounts = set()
            for line in df_result.stdout.splitlines()[1:]:
                parsed = _parse_df_line(line)
                if not parsed:
                    continue
                mp = parsed["mountpoint"]
                if mp in seen_mounts or mp.startswith("/snap"):
                    continue
                seen_mounts.add(mp)

                filesystems.append({
                    "mountpoint": mp,
                    "size": parsed["size"],
                    "used": parsed["used"],
                    "available": parsed["available"],
                    "use_pct": parsed["use_pct"],
                })
                pct = parsed["use_pct"]

                if pct > 95:
                    findings.append(Finding(
                        code="disk_usage_high",
                        severity=Severity.CRITICAL,
                        summary=f"Partition {mp} is {pct}% full",
                        explanation=(
                            f"The {mp} partition is critically full at {pct}%. "
                            "Services may fail to write logs, temp files, or data."
                        ),
                        client_action="Free up space or contact support about expanding storage.",
                        engineer_action=f"Check large files: du -sh {mp}/* | sort -rh | head -20",
                        detail={"mountpoint": mp, "use_pct": pct},
                    ))
                elif pct > 85:
                    findings.append(Finding(
                        code="disk_usage_high",
                        severity=Severity.WARNING,
                        summary=f"Partition {mp} is {pct}% full",
                        explanation=(
                            f"The {mp} partition is at {pct}% usage. It's not "
                            "critical yet but worth keeping an eye on."
                        ),
                        client_action="Consider cleaning up unnecessary files.",
                        engineer_action=f"Check large files: du -sh {mp}/* | sort -rh | head -20",
                        detail={"mountpoint": mp, "use_pct": pct},
                    ))

        # -- Unused / unmounted block devices --
        await self._check_unused_drives(data, findings)

        return ModuleResult(
            module_name=self.name,
            findings=findings,
            data=data,
        )

    async def _check_unused_drives(
        self, data: dict[str, Any], findings: list[Finding]
    ) -> None:
        """Find block devices that aren't mounted, in LVM, or part of RAID."""
        lsblk = await run_command(
            "lsblk -dpno NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE 2>/dev/null",
            timeout=10.0,
        )
        if not lsblk.success or not lsblk.stdout:
            return

        # also grab which PVs are in use by LVM
        pvs_result = await run_command(
            "pvs --noheadings -o pv_name 2>/dev/null", timeout=5.0
        )
        lvm_pvs = set()
        if pvs_result.success and pvs_result.stdout:
            for line in pvs_result.stdout.splitlines():
                lvm_pvs.add(line.strip())

        # grab RAID members from mdstat
        md_members = set()
        mdstat = data.get("mdstat", "")
        if mdstat:
            for m in re.findall(r"(sd[a-z]+\d*|nvme\d+n\d+p?\d*)", mdstat):
                md_members.add(f"/dev/{m}")

        unused_drives: list[dict[str, str]] = []

        for line in lsblk.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 3:
                continue

            name = parts[0]
            size = parts[1]
            dtype = parts[2]
            mountpoint = parts[3] if len(parts) > 3 else ""
            fstype = parts[4] if len(parts) > 4 else ""

            # only look at whole disks, not partitions or loops
            if dtype != "disk":
                continue
            # skip tiny devices (< 1GB -- likely boot media or USB)
            if re.match(r"^\d+(\.\d+)?[KM]$", size):
                continue

            # check if this disk is in use
            is_mounted = bool(mountpoint and mountpoint != "")
            is_lvm = name in lvm_pvs
            is_raid = name in md_members

            # also check if any partitions of this disk are in use
            part_check = await run_command(
                f"lsblk -no MOUNTPOINT,FSTYPE {name} 2>/dev/null", timeout=5.0
            )
            has_partitions_in_use = False
            if part_check.success and part_check.stdout:
                for pline in part_check.stdout.splitlines():
                    pline = pline.strip()
                    if pline and pline != name:
                        has_partitions_in_use = True
                        break

            if not is_mounted and not is_lvm and not is_raid and not has_partitions_in_use:
                unused_drives.append({
                    "device": name,
                    "size": size,
                })

        if unused_drives:
            data["unused_drives"] = unused_drives
            drive_list = ", ".join(
                f"{d['device']} ({d['size']})" for d in unused_drives
            )
            findings.append(Finding(
                code="unused_drives",
                severity=Severity.DEGRADED,
                summary=f"{len(unused_drives)} drive(s) not in use: {drive_list}",
                explanation=(
                    "These drives are present in the system but are not mounted, "
                    "not part of any LVM volume group, and not in a RAID array. "
                    "They may be spare drives, or they may need to be configured."
                ),
                client_action=(
                    "If you expected all drives to be in use, contact support. "
                    "These drives are just sitting there."
                ),
                engineer_action=(
                    f"Check with lsblk and fdisk -l. Drives: {drive_list}. "
                    "May need partitioning, LVM setup, or filesystem creation."
                ),
                detail={"drives": [d["device"] for d in unused_drives]},
            ))

    async def _get_smart_data(self, dev_path: str) -> dict[str, Any]:
        """Try nvme smart-log first, fall back to smartctl."""
        result = await run_command(f"nvme smart-log {dev_path} -o json", timeout=15.0)
        if result.success and result.stdout:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                pass

        # fallback: smartctl JSON output
        smart_result = await run_command(f"smartctl -a {dev_path} -j", timeout=15.0)
        if smart_result.success and smart_result.stdout:
            try:
                raw = json.loads(smart_result.stdout)
                nvme_log = raw.get("nvme_smart_health_information_log", {})
                return {
                    "critical_warning": nvme_log.get("critical_warning", 0),
                    "temperature": nvme_log.get("temperature", 0),
                    "avail_spare": nvme_log.get("available_spare", 100),
                    "percent_used": nvme_log.get("percentage_used", 0),
                    "media_errors": nvme_log.get("media_errors", 0),
                    "num_err_log_entries": nvme_log.get("num_err_log_entries", 0),
                }
            except (json.JSONDecodeError, KeyError):
                pass

        return {}
