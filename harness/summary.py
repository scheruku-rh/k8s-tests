"""A run's checks as a markdown table, for the run page on GitHub.

The HTML report is the record, and it is an artifact: reading it means downloading it.
This is the same list of checks in the place a reviewer looks first, rendered from the
JUnit XML the checks write beside it, so nothing here parses HTML.

A missing or unreadable report is a sentence, not an exception: the step that appends this
to the job summary runs even when the checks never started, and the run already says why.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from collections import Counter
from pathlib import Path

# A report can hold hundreds of checks; the ones that need reading are the failures, so
# their messages are quoted in full up to these bounds and the rest points at the report.
MAX_FAILURES = 20
MAX_FAILURE_LINES = 12
# A skip reason is usually one sentence, and it says why a check did not run.
MAX_SKIP_LINES = 3

PASSED = "pass"
FAILED = "fail"
ERROR = "error"
SKIPPED = "skip"

# The result cell for each outcome, in the order a reader cares about them.
LABELS = {PASSED: "✅ pass", FAILED: "❌ fail", ERROR: "⚠️ error", SKIPPED: "⏭️ skip"}
ORDER = (PASSED, FAILED, ERROR, SKIPPED)

# The child element a testcase carries for its outcome, and the outcome it means.
OUTCOME_TAGS = (("failure", FAILED), ("error", ERROR), ("skipped", SKIPPED))


def outcome(case: ElementTree.Element) -> str:
    """What happened to one testcase: passed, failed, errored or skipped."""
    for tag, result in OUTCOME_TAGS:
        if case.find(tag) is not None:
            return result
    return PASSED


def check_name(case: ElementTree.Element) -> str:
    """The check as the report names it: the module's file and the function, with parameters."""
    module = case.get("classname") or ""
    if module.startswith("tests."):
        module = module[len("tests.") :]
    name = case.get("name") or ""
    return f"{module.replace('.', '/')}.py::{name}" if module else name


def duration(case: ElementTree.Element) -> float:
    try:
        return float(case.get("time") or 0)
    except ValueError:
        return 0.0


def message(case: ElementTree.Element, limit: int) -> str:
    """The first lines of an outcome's message, trimmed and fenced for markdown."""
    for tag, _ in OUTCOME_TAGS:
        element = case.find(tag)
        if element is not None:
            break
    else:
        return ""
    text = (element.get("message") or element.text or "").strip()
    if not text:
        return ""
    lines = text.splitlines()[:limit]
    return "\n".join(line.rstrip() for line in lines)


def _cell(text: str) -> str:
    """A markdown table cell: pipes escaped so a node id cannot break the table."""
    return str(text).replace("|", "\\|")


# How each outcome is counted out loud.
WORDS = {PASSED: "passed", FAILED: "failed", ERROR: "errored", SKIPPED: "skipped"}


def _totals(counts: Counter[str], seconds: float) -> str:
    """How many of each outcome, and how long the run took."""
    parts = [f"{counts[result]} {WORDS[result]}" for result in ORDER if counts[result]]
    return f"{', '.join(parts)} in {seconds:.0f}s" if parts else "no checks"


def render(path: Path) -> str:
    """Render the checks a JUnit report holds as markdown."""
    if not Path(path).is_file():
        return f"_No JUnit report at `{path}`, so the checks did not run or did not finish._"
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        return f"_`{path}` is not readable as JUnit XML: {exc}._"

    cases = list(root.iter("testcase"))
    if not cases:
        return f"_`{path}` holds no checks._"

    counts: Counter[str] = Counter(outcome(case) for case in cases)
    seconds = sum(duration(case) for case in cases)
    lines = [f"**{_totals(counts, seconds)}**", "", "| Check | Result | Time |", "| --- | --- | --- |"]
    lines += [f"| `{_cell(check_name(case))}` | {LABELS[outcome(case)]} | {duration(case):.1f}s |" for case in cases]

    failures = [case for case in cases if outcome(case) in (FAILED, ERROR)]
    if failures:
        lines += ["", f"<details><summary>{len(failures)} failed</summary>", ""]
        for case in failures[:MAX_FAILURES]:
            quote = message(case, MAX_FAILURE_LINES)
            lines += [f"**`{check_name(case)}`**", "", "```", quote or "(no message)", "```", ""]
        if len(failures) > MAX_FAILURES:
            lines += [f"And {len(failures) - MAX_FAILURES} more in `report.html`.", ""]
        lines += ["</details>"]

    skipped = [case for case in cases if outcome(case) == SKIPPED]
    if skipped:
        lines += ["", f"<details><summary>{len(skipped)} skipped</summary>", ""]
        for case in skipped:
            reason = " ".join(message(case, MAX_SKIP_LINES).split()) or "no reason given"
            lines.append(f"- `{check_name(case)}` — {reason}")
        lines += ["", "</details>"]

    return "\n".join(lines)


__all__ = ["check_name", "duration", "message", "outcome", "render"]
