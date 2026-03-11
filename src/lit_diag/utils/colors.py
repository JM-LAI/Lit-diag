"""Shared color/style constants for Rich output."""

from lit_diag.modules.base import Severity

STATUS_STYLES = {
    Severity.OK: "[bold green]OK[/bold green]",
    Severity.WARNING: "[bold yellow]WARNING[/bold yellow]",
    Severity.CRITICAL: "[bold red]CRITICAL[/bold red]",
    Severity.DEGRADED: "[bold cyan]DEGRADED[/bold cyan]",
    Severity.ERROR: "[bold red]ERROR[/bold red]",
}

STATUS_ICONS = {
    Severity.OK: "[green]✓[/green]",
    Severity.WARNING: "[yellow]⚠[/yellow]",
    Severity.CRITICAL: "[red]✗[/red]",
    Severity.DEGRADED: "[cyan]~[/cyan]",
    Severity.ERROR: "[red]✗[/red]",
}

BANNER_STYLES = {
    Severity.OK: "green",
    Severity.WARNING: "yellow",
    Severity.CRITICAL: "red",
    Severity.DEGRADED: "cyan",
    Severity.ERROR: "red",
}

FINDING_LABELS = {
    Severity.OK: "[bold green]  ✓ OK  [/bold green]",
    Severity.WARNING: "[bold yellow]  ⚠ WARN[/bold yellow]",
    Severity.CRITICAL: "[bold red]  ✗ CRIT[/bold red]",
    Severity.DEGRADED: "[bold cyan]  ~ INFO[/bold cyan]",
    Severity.ERROR: "[bold red]  ✗ ERR [/bold red]",
}


def color_temp(temp: float, warn: float = 75.0, crit: float = 90.0) -> str:
    """Color a temperature value based on thresholds."""
    if temp >= crit:
        return f"[bold red]{temp}°C[/bold red]"
    elif temp >= warn:
        return f"[yellow]{temp}°C[/yellow]"
    else:
        return f"[green]{temp}°C[/green]"


def color_gpu_temp(temp: float) -> str:
    """Color GPU temperature -- GPUs run hotter than CPUs."""
    return color_temp(temp, warn=75.0, crit=85.0)


def color_pct(value: float, warn: float = 70.0, crit: float = 90.0, invert: bool = False) -> str:
    """Color a percentage. Set invert=True for values where lower is worse (like spare capacity)."""
    if invert:
        if value <= (100 - crit):
            return f"[bold red]{value}%[/bold red]"
        elif value <= (100 - warn):
            return f"[yellow]{value}%[/yellow]"
        else:
            return f"[green]{value}%[/green]"
    else:
        if value >= crit:
            return f"[bold red]{value}%[/bold red]"
        elif value >= warn:
            return f"[yellow]{value}%[/yellow]"
        else:
            return f"[green]{value}%[/green]"


def color_power(draw: float, limit: float) -> str:
    """Color power usage relative to limit."""
    if limit <= 0:
        return f"{draw}W"
    pct = (draw / limit) * 100
    if pct >= 95:
        return f"[bold red]{draw}W[/bold red]"
    elif pct >= 80:
        return f"[yellow]{draw}W[/yellow]"
    else:
        return f"[green]{draw}W[/green]"


def color_state(state: str, good: str = "ACTIVE") -> str:
    """Color a state string -- green if it matches the good state, red otherwise."""
    if good.upper() in state.upper():
        return f"[green]{state}[/green]"
    elif "DOWN" in state.upper() or "DISABLED" in state.upper():
        return f"[red]{state}[/red]"
    else:
        return f"[yellow]{state}[/yellow]"


def color_errors(count: int, warn: int = 1, crit: int = 100) -> str:
    """Color an error count."""
    if count >= crit:
        return f"[bold red]{count}[/bold red]"
    elif count >= warn:
        return f"[yellow]{count}[/yellow]"
    else:
        return f"[green]{count}[/green]"


def color_health(health: str) -> str:
    """Color a health status string."""
    h = health.lower()
    if h in ("healthy", "ok", "good", "passed"):
        return f"[green]{health}[/green]"
    elif h in ("degraded", "warning"):
        return f"[yellow]{health}[/yellow]"
    else:
        return f"[red]{health}[/red]"


def color_wear(pct: float) -> str:
    """Color NVMe wear percentage -- higher is worse."""
    if pct >= 90:
        return f"[bold red]{pct}%[/bold red]"
    elif pct >= 70:
        return f"[yellow]{pct}%[/yellow]"
    else:
        return f"[green]{pct}%[/green]"


def color_rpm(rpm: float) -> str:
    """Color a fan RPM -- 0 is bad."""
    if rpm == 0:
        return f"[bold red]{int(rpm)} RPM[/bold red]"
    else:
        return f"[green]{int(rpm)} RPM[/green]"
