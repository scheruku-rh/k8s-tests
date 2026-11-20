"""The harness unit tests are pure: nothing here talks to a cluster.

The root conftest has a session-scoped autouse fixture that logs where the run points
and skips the session when there is no usable kubeconfig. These tests shadow it, so
`pytest tests/harness` runs on a machine with no cluster at all.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def report_target() -> None:
    """Nothing to report: the tests below read settings, records and command lines."""
