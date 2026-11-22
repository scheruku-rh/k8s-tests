"""The pytest selections the sections and `--select` produce."""

from __future__ import annotations

import pytest

from harness import tests
from harness.config import Settings
from harness.shell import HarnessError


@pytest.mark.parametrize(
    ("section", "marker"),
    [
        ("standard", "not adapters"),
        ("all", None),
        ("adapters", "adapters and not atif"),
        ("adapters-atif", "atif"),
        ("evalhub", "evalhub and not adapters"),
        ("generation", "evalhub and generation"),
        ("isvc-free", "isvc_free"),
        ("lmeval", "lmeval"),
        ("tas-payload", "tas_payload"),
    ],
)
def test_each_section_names_its_marker(section, marker):
    assert tests.section_marker(section, Settings()) == marker


def test_an_unknown_section_is_an_error():
    with pytest.raises(HarnessError, match="unknown section"):
        tests.section_marker("everything", Settings())


def test_the_standard_section_writes_the_main_report():
    arguments, report = tests.pytest_arguments(Settings(), section="standard", marker=None, select=(), report=None)
    assert report == Settings().report_path
    assert "-m" in arguments and "not adapters" in arguments
    assert f"--html={Settings().report_path}" in arguments
    assert "--self-contained-html" in arguments


def test_the_adapters_section_writes_its_own_report():
    _, report = tests.pytest_arguments(Settings(), section="adapters", marker=None, select=(), report=None)
    assert report == Settings().adapters_report_path


def test_a_report_named_outright_wins():
    _, report = tests.pytest_arguments(
        Settings(), section="adapters", marker=None, select=(), report="/tmp/somewhere.html"
    )
    assert str(report) == "/tmp/somewhere.html"


def test_select_bypasses_the_section_marker():
    arguments, _ = tests.pytest_arguments(
        Settings(), section="standard", marker=None, select=("-m generation",), report=None
    )
    assert "not adapters" not in arguments
    # The only -m left is the interpreter's `-m pytest`.
    assert arguments.count("-m") == 2


def test_an_explicit_marker_applies_to_a_selection():
    arguments, _ = tests.pytest_arguments(
        Settings(), section="standard", marker="benchmark", select=("tests/evalhub",), report=None
    )
    assert "benchmark" in arguments
    assert "not adapters" not in arguments


def test_a_selection_path_is_resolved_inside_the_suite():
    arguments, _ = tests.pytest_arguments(
        Settings(), section="standard", marker=None, select=("tests/evalhub/test_evalhub.py::test_health",), report=None
    )
    named = [argument for argument in arguments if argument.endswith("::test_health")]
    assert named and named[0].startswith(str(tests.resources.tests_root()))


def test_pytest_args_are_split_and_appended():
    arguments, _ = tests.pytest_arguments(
        Settings(pytest_args="-x -k tenant"), section="all", marker=None, select=(), report=None
    )
    assert ["-x", "-k", "tenant"] == arguments[arguments.index("-x") :][:3]


def test_the_suite_and_its_configuration_are_named_outright():
    root = tests.resources.tests_root()
    arguments, _ = tests.pytest_arguments(Settings(), section="all", marker=None, select=(), report=None)
    assert str(root) in arguments
    assert "-c" in arguments
    assert str(tests.resources.pytest_config()) in arguments


def test_an_empty_select_is_an_error():
    with pytest.raises(HarnessError, match="must name a pytest selection"):
        tests.pytest_arguments(Settings(), section="all", marker=None, select=("   ",), report=None)
