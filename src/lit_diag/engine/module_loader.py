"""Discover and load diagnostic modules."""

from __future__ import annotations

from typing import Optional

from lit_diag.modules.base import BaseDiagnosticModule

_REGISTRY: dict[str, type[BaseDiagnosticModule]] = {}


def register_module(cls: type[BaseDiagnosticModule]) -> type[BaseDiagnosticModule]:
    """Decorator to register a diagnostic module."""
    _REGISTRY[cls.name] = cls
    return cls


def get_module(name: str) -> Optional[type[BaseDiagnosticModule]]:
    """Get a module class by name."""
    return _REGISTRY.get(name)


def get_all_modules() -> dict[str, type[BaseDiagnosticModule]]:
    """Get all registered modules."""
    return dict(_REGISTRY)


def get_module_names() -> list[str]:
    """Get sorted list of module names."""
    return sorted(_REGISTRY.keys())


def load_all_modules() -> None:
    """Import all module files to trigger registration."""
    from lit_diag.modules import (  # noqa: F401
        gpu,
        nvlink,
        pcie,
        kernel_logs,
        storage,
        thermal,
        infiniband,
        cuda_tests,
        driver,
        system,
    )
