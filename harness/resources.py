"""Where the harness reads what it did not write: the checks, the Kind config, the emulator.

A checkout wins (editable installs point here, and the checks are the ones in front of
you); otherwise the copy the wheel installed as `harness/data/`.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from harness.config import repository_root

TESTS = "tests"
PYTEST_CONFIG = "pyproject.toml"
CONFTEST = "conftest.py"
KIND_CONFIG = Path("hack") / "kind" / "cluster.yaml"
EMULATOR_WRAPPER = Path("hack") / "emulator" / "auth_emulator.py"
REPORT_LOGO = Path("docs") / "trustyai.png"


def package_data_root() -> Path:
    """The copy of the checks and their assets a wheel installs."""
    return Path(str(files("harness").joinpath("data")))


def data_root() -> Path:
    """The root this run reads from: the checkout when there is one, else the installed copy."""
    return repository_root() or package_data_root()


def from_package() -> bool:
    return repository_root() is None


def describe() -> str:
    """One line saying which copy of the checks this run reads."""
    if from_package():
        return f"the checks installed in the package at {data_root()} (no checkout on this path)"
    return f"the checkout at {data_root()}"


def tests_root() -> Path:
    """The directory pytest collects."""
    return data_root() / TESTS


def pytest_config() -> Path:
    """The pytest configuration: markers, addopts and testpaths."""
    return data_root() / PYTEST_CONFIG


def kind_config() -> Path:
    """The Kind cluster configuration the harness ships."""
    return data_root() / KIND_CONFIG


def emulator_wrapper() -> Path:
    """The auth emulator script, mounted into the emulator pod as a ConfigMap."""
    return data_root() / EMULATOR_WRAPPER


def resolve(path: str, default: Path) -> Path:
    """A path from a setting, or `default` when it names none; relative paths are from cwd."""
    return Path(path).expanduser() if path.strip() else default


__all__ = [
    "CONFTEST",
    "EMULATOR_WRAPPER",
    "KIND_CONFIG",
    "PYTEST_CONFIG",
    "REPORT_LOGO",
    "TESTS",
    "data_root",
    "describe",
    "emulator_wrapper",
    "from_package",
    "kind_config",
    "package_data_root",
    "pytest_config",
    "resolve",
    "tests_root",
]
