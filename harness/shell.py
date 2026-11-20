"""The primitives the commands are built from: messages, `run`, tool checks."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence

import click

LOGGER = logging.getLogger("harness")


class HarnessError(click.ClickException):
    """A failure the harness diagnosed: printed as `Error: ...` with a non-zero status."""

    exit_code = 1


def info(message: str) -> None:
    click.echo(f"{click.style('==>', fg='blue', bold=True)} {message}")


def warn(message: str) -> None:
    click.echo(f"{click.style('warning:', fg='yellow', bold=True)} {message}", err=True)


def run(
    command: Sequence[object],
    *,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, raising `HarnessError` with its output when it fails."""
    argv = [str(part) for part in command]
    LOGGER.debug("running %s", " ".join(argv))
    result = subprocess.run(
        argv,
        capture_output=capture,
        text=True,
        input=input_text,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        check=False,
    )
    if check and result.returncode != 0:
        detail = ((result.stderr or "") or (result.stdout or "")).strip() if capture else ""
        message = f"{argv[0]} failed with exit status {result.returncode}"
        raise HarnessError(f"{message}: {detail}" if detail else message)
    return result


def output(command: Sequence[object], **kwargs: object) -> str:
    """Run a command and return its standard output, stripped."""
    return (run(command, capture=True, **kwargs).stdout or "").strip()  # type: ignore[arg-type]


def succeeded(command: Sequence[object], **kwargs: object) -> bool:
    """Run a command for its exit status alone, keeping its output quiet."""
    return run(command, capture=True, check=False, **kwargs).returncode == 0  # type: ignore[arg-type]


def require_tool(name: str, hint: str = "") -> str:
    """Return `name` when it is installed, and fail with `hint` when it is not."""
    if shutil.which(name) is None:
        raise HarnessError(f"{name} is not installed" + (f"; {hint}" if hint else ""))
    return name


def environment(additions: Mapping[str, str] | None = None) -> dict[str, str]:
    """The current environment, with the non-empty additions layered on top."""
    resolved = dict(os.environ)
    for key, value in (additions or {}).items():
        if value:
            resolved[key] = value
    return resolved


__all__ = ["HarnessError", "environment", "info", "LOGGER", "output", "require_tool", "run", "succeeded", "warn"]
