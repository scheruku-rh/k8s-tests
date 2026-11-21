"""The run-page summary: what a JUnit report is rendered as."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from harness import cli, summary

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="5" failures="1" errors="1" skipped="1">
    <testcase classname="tests.test_trustyai_operators" name="test_deployment_is_healthy[workload-operator]" time="0.6">
      <system-out>the check logged this</system-out>
    </testcase>
    <testcase classname="tests.test_trustyai_service" name="test_service_crd_serves_both_versions" time="1.25">
      <failure message="assert served &gt;= {'v1'}&#10;E   AssertionError: served versions are ['v1alpha1']">
        traceback line
      </failure>
    </testcase>
    <testcase classname="tests.test_emulator" name="test_emulator_requires_a_token" time="2">
      <error message="fixture 'emulator' raised ApiException: 503">traceback line</error>
    </testcase>
    <testcase classname="tests.test_trustyai_module" name="test_trustyai_module_cr_ready" time="0">
      <skipped message="applies to operator releases 3.7 and later; this run targets 3.6" />
    </testcase>
    <testcase classname="harness.test_arch" name="test_a|b" time="0.1" />
  </testsuite>
</testsuites>
"""


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT)
    return path


def test_a_case_with_no_outcome_passed(report):
    root = summary.ElementTree.parse(report).getroot()
    case = root.find(".//testcase")
    assert summary.outcome(case) == summary.PASSED


def test_the_check_is_named_the_way_the_report_names_it(report):
    root = summary.ElementTree.parse(report).getroot()
    names = [summary.check_name(case) for case in root.iter("testcase")]
    assert names[0] == "test_trustyai_operators.py::test_deployment_is_healthy[workload-operator]"
    assert names[4] == "harness/test_arch.py::test_a|b"


def test_every_check_is_listed_with_its_result(report):
    rendered = summary.render(report)
    assert "**2 passed, 1 failed, 1 errored, 1 skipped in 4s**" in rendered
    assert "| Check | Result | Time |" in rendered
    assert "`test_trustyai_service.py::test_service_crd_serves_both_versions` | ❌ fail | 1.2s" in rendered
    assert "`test_emulator.py::test_emulator_requires_a_token` | ⚠️ error | 2.0s" in rendered
    assert "`test_trustyai_module.py::test_trustyai_module_cr_ready` | ⏭️ skip | 0.0s" in rendered
    assert "`test_trustyai_operators.py::test_deployment_is_healthy[workload-operator]` | ✅ pass | 0.6s" in rendered


def test_a_pipe_in_a_name_does_not_break_the_table(report):
    assert "`harness/test_arch.py::test_a\\|b`" in summary.render(report)


def test_the_failure_message_comes_with_it(report):
    rendered = summary.render(report)
    assert "<details><summary>2 failed</summary>" in rendered
    assert "AssertionError: served versions are ['v1alpha1']" in rendered
    assert "fixture 'emulator' raised ApiException: 503" in rendered
    # The logged output of a passing check is not part of a failure quote.
    assert "the check logged this" not in rendered


def test_the_skip_reason_is_quoted(report):
    rendered = summary.render(report)
    assert "<details><summary>1 skipped</summary>" in rendered
    assert "applies to operator releases 3.7 and later; this run targets 3.6" in rendered


def test_a_long_failure_message_is_trimmed(tmp_path: Path):
    """pytest writes the message's newlines as character references; a raw one is normalised."""
    message = "&#10;".join(f"line {number}" for number in range(summary.MAX_FAILURE_LINES + 5))
    path = tmp_path / "junit.xml"
    path.write_text(
        "<testsuites><testsuite><testcase classname='tests.test_a' name='test_b'>"
        f'<failure message="{message}"/></testcase></testsuite></testsuites>'
    )
    rendered = summary.render(path)
    assert f"line {summary.MAX_FAILURE_LINES - 1}" in rendered
    assert f"line {summary.MAX_FAILURE_LINES}" not in rendered


def test_failures_past_the_bound_point_at_the_report(tmp_path: Path):
    cases = "".join(
        f"<testcase classname='tests.test_a' name='test_b{number}'><failure message='no'/></testcase>"
        for number in range(summary.MAX_FAILURES + 3)
    )
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    assert "And 3 more in `report.html`." in summary.render(path)


def test_a_missing_report_is_a_sentence(tmp_path: Path):
    assert "did not run or did not finish" in summary.render(tmp_path / "absent.xml")


def test_an_unreadable_report_is_a_sentence(tmp_path: Path):
    path = tmp_path / "junit.xml"
    path.write_text("not xml")
    assert "is not readable as JUnit XML" in summary.render(path)


def test_a_report_with_no_checks_is_a_sentence(tmp_path: Path):
    path = tmp_path / "junit.xml"
    path.write_text("<testsuites/>")
    assert "holds no checks" in summary.render(path)


def test_the_command_prints_the_summary(report):
    result = CliRunner().invoke(cli.cli, ["summary", str(report)], obj=None, catch_exceptions=False)
    assert result.exit_code == 0
    assert "| Check | Result | Time |" in result.output
