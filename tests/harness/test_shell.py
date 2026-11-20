"""`run` and `succeeded`: what a missing binary does, and what a failed command does."""

from __future__ import annotations

import subprocess

import pytest

from harness import shell


def test_a_missing_binary_is_a_harness_error(monkeypatch):
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr(shell.subprocess, "run", missing)
    with pytest.raises(shell.HarnessError, match="docker is not installed"):
        shell.run(["docker", "info"])


def test_a_probe_for_a_missing_binary_answers_no(monkeypatch):
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr(shell.subprocess, "run", missing)
    result = shell.run(["docker", "info"], capture=True, check=False)
    assert result.returncode == 127
    assert "docker is not installed" in (result.stderr or "")
    assert not shell.succeeded(["docker", "info"])


def test_a_failing_command_keeps_its_output():
    with pytest.raises(shell.HarnessError, match="failed with exit status"):
        shell.run(["sh", "-c", "echo boom >&2; exit 3"], capture=True)


def test_output_is_stripped_and_succeeded_reads_the_status():
    assert shell.output(["sh", "-c", "printf 'value\\n'"]) == "value"
    assert shell.succeeded(["sh", "-c", "exit 0"])
    assert not shell.succeeded(["sh", "-c", "exit 1"])


def test_a_missing_tool_is_named_with_its_hint(monkeypatch):
    monkeypatch.setattr(shell.shutil, "which", lambda name: None)
    with pytest.raises(shell.HarnessError, match="docker is not installed; install it"):
        shell.require_tool("docker", "install it")


def test_environment_layers_the_non_empty_additions():
    resolved = shell.environment({"KIND_EXPERIMENTAL_PROVIDER": "podman", "EMPTY": ""})
    assert resolved["KIND_EXPERIMENTAL_PROVIDER"] == "podman"
    assert "EMPTY" not in resolved
    assert isinstance(resolved, dict)
    assert resolved is not subprocess.os.environ
