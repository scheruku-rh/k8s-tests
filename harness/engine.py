"""The container engine: which one to use, and the build, push and save calls to make."""

from __future__ import annotations

import os
from shutil import which

from harness.config import Settings
from harness.shell import HarnessError, info, output, require_tool, run, succeeded, warn

ENGINES = ("podman", "docker")


def cluster_owner_engine(settings: Settings) -> str:
    """The engine that has this cluster's node containers, or "" when neither does."""
    for engine in ENGINES:
        # Only ask the engines this host has: probing the other one raises ENOENT
        # rather than answering "no cluster" (docker is not installed on podman-only
        # hosts, and the probe runs on every kind command).
        if which(engine) is None or not succeeded([engine, "info"]):
            continue
        listed = output(
            [engine, "ps", "--all", "--quiet", "--filter", f"label=io.x-k8s.kind.cluster={settings.kind_cluster}"],
            check=False,
        )
        if listed:
            return engine
    return ""


def container_engine(settings: Settings) -> str:
    """ENGINE, then KIND_PROVIDER, then the cluster's own engine, then whichever answers."""
    if settings.engine:
        return require_tool(settings.engine, "install it, or set ENGINE to docker or podman")
    if settings.kind_provider:
        return require_tool(settings.kind_provider, "install it, or set KIND_PROVIDER to docker or podman")
    owner = cluster_owner_engine(settings)
    if owner:
        return owner
    for engine in ENGINES:
        if succeeded([engine, "info"]):
            return engine
    if any(which(engine) for engine in ENGINES):
        raise HarnessError(
            "podman/docker is installed but not reachable; start the podman machine (podman machine start) "
            "or Docker Desktop"
        )
    raise HarnessError("neither podman nor docker is installed")


def reachable_engine(settings: Settings) -> str | None:
    """The engine to use when having one is optional: None when none answers."""
    try:
        engine = container_engine(settings)
    except HarnessError:
        return None
    return engine if succeeded([engine, "info"]) else None


def normalize_arch(machine: str) -> str:
    """Map uname/engine architecture names onto the GOARCH names the build tools use."""
    return {"x86_64": "amd64", "x86-64": "amd64", "aarch64": "arm64"}.get(machine, machine)


def host_platform(settings: Settings) -> str:
    """The engine's own OS/arch, named the way `--platform` and TARGETARCH expect."""
    if container_engine(settings) == "docker":
        reported = output(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], check=False)
        if reported and "/" in reported:
            operating_system, _, architecture = reported.partition("/")
            return f"{operating_system}/{normalize_arch(architecture)}"
    machine = os.uname().machine
    if machine in ("arm64", "aarch64", "x86_64", "amd64"):
        return f"linux/{normalize_arch(machine)}"
    raise HarnessError(f"unsupported host architecture: {machine}")


def platform(settings: Settings) -> str:
    return settings.platform or host_platform(settings)


def build_command(engine: str) -> list[str]:
    """`build` plus `--load`: docker buildx otherwise keeps the image in its cache only."""
    command = [engine, "build"]
    if engine == "docker" and succeeded(["docker", "buildx", "version"]):
        command.append("--load")
    return command


def build(
    settings: Settings,
    image: str,
    context: str | os.PathLike[str],
    dockerfile: str | os.PathLike[str],
    *build_args: str,
) -> None:
    """Build one image for the run's platform, into the engine's local store."""
    engine = container_engine(settings)
    target = platform(settings)
    info(f"building {image} for {target} with {engine}")
    run([*build_command(engine), "--platform", target, *build_args, "-t", image, "-f", dockerfile, context])


def push(settings: Settings, image: str) -> None:
    """Push to the local registry, which is plain HTTP."""
    engine = container_engine(settings)
    info(f"pushing {image}")
    if engine == "podman":
        run([engine, "push", "--tls-verify=false", image])
    else:
        run([engine, "push", image])


def save(settings: Settings, image: str, archive: str | os.PathLike[str]) -> None:
    """Export an image to an archive the Kind node can import."""
    engine = container_engine(settings)
    info(f"exporting {image} with {engine}")
    run([engine, "save", "-o", archive, image])


def image_present(settings: Settings, image: str) -> bool:
    return succeeded([container_engine(settings), "image", "inspect", image])


def remove(settings: Settings, image: str) -> None:
    """Remove an image from the engine's store, complaining but not failing."""
    if not image_present(settings, image):
        info(f"image {image} is not present")
        return
    info(f"removing image {image}")
    if not succeeded([container_engine(settings), "rmi", image]):
        warn(f"could not remove {image}")


def container_state(settings: Settings, name: str) -> str:
    """A container's state - running, exited, ... - or "" when it does not exist."""
    return output(
        [container_engine(settings), "container", "inspect", "--format", "{{.State.Status}}", name], check=False
    )


def start_container(settings: Settings, name: str) -> None:
    run([container_engine(settings), "container", "start", name], capture=True)


def split_image(reference: str) -> tuple[str, str]:
    """Split "registry:port/name:tag" into the image name and its tag."""
    name, _, last = reference.rpartition("/")
    if ":" in last:
        image, _, tag = last.rpartition(":")
        return (f"{name}/{image}" if name else image), tag
    return reference, "latest"


__all__ = [
    "ENGINES",
    "build",
    "build_command",
    "cluster_owner_engine",
    "container_engine",
    "container_state",
    "host_platform",
    "image_present",
    "normalize_arch",
    "platform",
    "push",
    "reachable_engine",
    "remove",
    "save",
    "split_image",
    "start_container",
]
