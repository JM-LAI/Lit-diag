"""Backwards-compatible setup.py for older pip versions."""

from setuptools import setup, find_packages

setup(
    name="lit-diag",
    version="0.1.0",
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
