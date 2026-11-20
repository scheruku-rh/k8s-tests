"""The engine helpers and the registry naming that the flows agree on."""

from __future__ import annotations

import pytest

from harness import engine, registry
from harness.config import Settings


def test_split_image_keeps_the_registry_port_in_the_name():
    assert engine.split_image("localhost:5005/evalhub/evalhub:dev") == ("localhost:5005/evalhub/evalhub", "dev")
    assert engine.split_image("quay.io/me/operator:1.2.3") == ("quay.io/me/operator", "1.2.3")
    assert engine.split_image("evalhub") == ("evalhub", "latest")


def test_architecture_names_are_mapped_onto_goarch():
    assert engine.normalize_arch("x86_64") == "amd64"
    assert engine.normalize_arch("aarch64") == "arm64"
    assert engine.normalize_arch("ppc64le") == "ppc64le"


def test_the_host_platform_is_named_the_way_buildkit_expects(monkeypatch):
    monkeypatch.setattr(engine, "container_engine", lambda settings: "podman")
    expected = "arm64" if engine.os.uname().machine in ("arm64", "aarch64") else "amd64"
    assert engine.platform(Settings()) == f"linux/{expected}"
    assert engine.platform(Settings(platform="linux/s390x")) == "linux/s390x"


def test_docker_builds_are_loaded_out_of_the_builder_cache(monkeypatch):
    monkeypatch.setattr(engine, "succeeded", lambda command, **kwargs: True)
    assert engine.build_command("docker") == ["docker", "build", "--load"]
    assert engine.build_command("podman") == ["podman", "build"]


def test_registry_names_are_stable_and_short():
    settings = Settings(kind_cluster="trustyai")
    assert registry.registry_name(settings) == "k8s-tests-registry-trustyai"
    alias = registry.registry_alias(settings)
    assert alias == registry.registry_alias(Settings(kind_cluster="trustyai"))
    assert len(alias) <= 63
    assert alias != registry.registry_alias(Settings(kind_cluster="another-cluster"))


def test_registry_references_carry_the_port_and_tag():
    settings = Settings(registry_port="5005", tag="dev")
    assert registry.registry_reference(settings, "evalhub/evalhub") == "localhost:5005/evalhub/evalhub:dev"


@pytest.mark.parametrize(
    ("adapter", "image", "context"),
    [
        ("clear", "community-ibm-clear", "adapters/clear"),
        ("evalhub-post-processor", "evalhub-post-processor", "adapters/evalhub-post-processor"),
        ("toolbench-server", "community-toolbench-server", "adapters/tooleval/tool-server"),
        ("ragas", "community-ragas", "adapters/ragas"),
    ],
)
def test_adapter_images_and_contexts(adapter, image, context):
    assert registry.adapter_image_name(adapter) == image
    assert registry.adapter_context(adapter) == context


def test_all_selects_every_adapter_and_the_lmes_image():
    settings = Settings(adapters="all")
    assert registry.selected_adapters(settings) == list(registry.CONTRIB_ADAPTERS)
    assert registry.builds_lmes(settings)


def test_a_named_adapter_builds_only_that_one():
    settings = Settings(adapters="inspect,ragas")
    assert registry.selected_adapters(settings) == ["inspect", "ragas"]
    assert not registry.builds_lmes(settings)


def test_lmes_alone_builds_no_contrib_adapter():
    settings = Settings(adapters="lmes")
    assert registry.selected_adapters(settings) == []
    assert registry.builds_lmes(settings)


def test_an_unknown_adapter_is_an_error():
    with pytest.raises(Exception, match="unknown ADAPTERS entry 'inspct'"):
        registry.selected_adapters(Settings(adapters="inspct"))


def test_the_mirror_config_points_at_the_registry_alias():
    settings = Settings(kind_cluster="trustyai", registry_port="5005")
    hosts = registry.hosts_config(settings)
    assert f'[host."http://{registry.registry_alias(settings)}:5000"]' in hosts
    assert 'capabilities = ["pull", "resolve"]' in hosts


def test_certs_d_patching_can_be_refused():
    assert registry.patch_containerd_enabled(Settings())
    assert registry.patch_containerd_enabled(Settings(patch_containerd="true"))
    assert not registry.patch_containerd_enabled(Settings(patch_containerd="0"))
    assert not registry.patch_containerd_enabled(Settings(patch_containerd="false"))


def test_an_engine_that_is_not_installed_is_never_probed(monkeypatch):
    """`docker` is absent on a podman-only host; probing it raises ENOENT, not "no cluster"."""
    probed: list[str] = []

    def installed(name: str) -> str | None:
        return "/usr/bin/podman" if name == "podman" else None

    def answered(command: list[object], **kwargs: object) -> bool:
        probed.append(str(command[0]))
        return False

    monkeypatch.setattr(engine, "which", installed)
    monkeypatch.setattr(engine, "succeeded", answered)
    assert engine.cluster_owner_engine(Settings()) == ""
    assert probed == ["podman"]


def test_the_engine_owning_the_cluster_wins(monkeypatch):
    monkeypatch.setattr(engine, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(engine, "succeeded", lambda command, **kwargs: True)
    monkeypatch.setattr(engine, "output", lambda command, **kwargs: "trustyai-control-plane")
    assert engine.cluster_owner_engine(Settings()) == "podman"


def test_an_engine_is_still_chosen_when_no_cluster_owns_one(monkeypatch):
    """The regression: `k8s-tests down` on a host without docker raised FileNotFoundError."""
    monkeypatch.setattr(engine, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)
    monkeypatch.setattr(engine, "succeeded", lambda command, **kwargs: str(command[0]) == "podman")
    monkeypatch.setattr(engine, "output", lambda command, **kwargs: "")
    assert engine.cluster_owner_engine(Settings()) == ""
    assert engine.container_engine(Settings()) == "podman"
