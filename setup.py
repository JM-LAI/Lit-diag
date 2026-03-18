"""Backwards-compatible setup.py for older pip versions."""

import re
from pathlib import Path
from setuptools import setup, find_packages

_version = re.search(
    r'__version__\s*=\s*["\']([^"\']+)["\']',
    (Path(__file__).parent / "src" / "lit_diag" / "__init__.py").read_text(),
).group(1)

setup(
    name="lit-diag",
    version=_version,
    description="Client-first GPU cluster diagnostics tool",
    author="Joe Mannix",
    python_requires=">=3.9",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "click>=8.0.0",
        "rich>=13.0.0",
        "pydantic>=2.0.0",
    ],
    entry_points={
        "console_scripts": [
            "lit-diag=lit_diag.cli:main",
        ],
    },
)
