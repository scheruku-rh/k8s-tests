"""The command line: the commands, the flags on either side of a name, and the wiring."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from harness import cli, cluster, deploy, images
from harness.config import SETTINGS, Settings


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *arguments: str):
    return runner.invoke(cli.cli, [str(argument) for argument in arguments], obj=None, catch_exceptions=False)


def test_help_lists_every_command(runner):
    result = invoke(runner, "--help")
    assert result.exit_code == 0
    for command in (
        "up",
        "up-registry",
        "down",
        "cluster",
        "images",
        "images-all",
        "load",
        "deploy",
        "deploy-registry",
        "clean",
        "status",
        "logs",
        "test",
        "lint",
        "fmt",
        "completion",
    ):
        assert command in result.output


def test_help_documents_the_settings_with_their_variables(runner):
    output = invoke(runner, "--help").output
    for setting in SETTINGS:
        assert setting.option in output
        assert setting.envvar in output


def test_there_is_a_command_for_every_make_target(runner):
    """`make test-evalhub-adapters` and friends become `test --section ...`."""
    output = invoke(runner, "test", "--help").output
    for section in ("standard", "all", "adapters", "evalhub", "generation", "lmeval"):
        assert section in output


def test_a_setting_works_before_and_after_the_command(runner, monkeypatch):
    seen: list[Settings] = []
    monkeypatch.setattr(cluster, "create", lambda settings: seen.append(settings))
    assert invoke(runner, "--namespace", "before", "cluster").exit_code == 0
    assert invoke(runner, "cluster", "--namespace", "after").exit_code == 0
    assert [settings.namespace for settings in seen] == ["before", "after"]


def test_up_runs_every_step_in_order(runner, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(images, "check_operator_repo", lambda settings: calls.append("check"))
    monkeypatch.setattr(cluster, "create", lambda settings: calls.append("cluster"))
    monkeypatch.setattr(images, "build_load_path_images", lambda settings: calls.append("images"))
    monkeypatch.setattr(images, "load", lambda settings: calls.append("load"))
    monkeypatch.setattr(deploy, "deploy", lambda settings: calls.append("deploy"))
    assert invoke(runner, "up").exit_code == 0
    assert calls == ["check", "cluster", "images", "load", "deploy"]


def test_up_registry_runs_the_registry_flow(runner, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cluster, "create", lambda settings: calls.append("cluster"))
    monkeypatch.setattr(images, "images_all", lambda settings: calls.append("images-all"))
    monkeypatch.setattr(deploy, "registry_deploy", lambda settings: calls.append("deploy-registry"))
    assert invoke(runner, "up-registry").exit_code == 0
    assert calls == ["cluster", "images-all", "deploy-registry"]


def test_down_and_cluster_delete_are_the_same_command(runner, monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(cluster, "delete", lambda settings: deleted.append(settings.kind_cluster))
    assert invoke(runner, "down").exit_code == 0
    assert invoke(runner, "cluster-delete").exit_code == 0
    assert deleted == ["trustyai", "trustyai"]


def test_a_command_reports_a_harness_error_without_a_traceback(runner, monkeypatch):
    def fail(settings):
        raise cli.HarnessError("no checkout there")

    monkeypatch.setattr(images, "check_operator_repo", fail)
    result = invoke(runner, "check-operator-repo")
    assert result.exit_code == 1
    assert "no checkout there" in result.output


def test_lint_and_fmt_need_a_checkout(runner, monkeypatch):
    monkeypatch.setattr(cli, "repository_root", lambda: None)
    for command in ("lint", "fmt"):
        result = invoke(runner, command)
        assert result.exit_code == 1
        assert "K8S_TESTS_ROOT" in result.output


def test_completion_prints_a_script_for_each_shell(runner):
    for shell in ("bash", "zsh", "fish"):
        result = invoke(runner, "completion", shell)
        assert result.exit_code == 0
        assert "k8s-tests" in result.output


def test_an_empty_variable_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("NAMESPACE", "")
    cli._drop_empty_environment()
    import os

    assert "NAMESPACE" not in os.environ
