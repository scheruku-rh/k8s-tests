"""Building the images, loading them into Kind, pushing them to the local registry.

`images` builds the two operator images and loads them into the node. `images-all`
builds every image the suite can build from source and pushes it to the local registry,
recording the references in IMAGES_ENV_FILE for `deploy-registry` to read back.
"""

from __future__ import annotations

import filecmp
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from harness import registry
from harness.config import Settings, record_lines
from harness.engine import (
    build,
    container_engine,
    container_state,
    image_present,
    platform,
    push,
    reachable_engine,
    remove,
    save,
)
from harness.shell import HarnessError, environment, info, output, run, warn

MODULE_DIR = "trustyai-operator-module"
LMES_DOCKERFILE = "Dockerfile.lmes-job"


def operator_repo(settings: Settings) -> Path:
    """The trustyai-service-operator checkout, checked for the files the build needs."""
    if not settings.operator_repo:
        raise HarnessError(
            "OPERATOR_REPO must point at a trustyai-service-operator checkout, "
            "for example --operator-repo ~/Sync/code/reference/trustyai-service-operator"
        )
    repo = Path(settings.operator_repo).expanduser()
    if not repo.is_dir():
        raise HarnessError(f"OPERATOR_REPO={repo} does not exist")
    if not (repo / "Dockerfile").is_file() or not (repo / MODULE_DIR / "Dockerfile").is_file():
        raise HarnessError(f"{repo} does not look like a trustyai-service-operator checkout")
    return repo


def check_operator_repo(settings: Settings) -> None:
    """Report the checkout this run builds and deploys from."""
    info(f"{operator_repo(settings)} is a trustyai-service-operator checkout")


def trees_differ(left: Path, right: Path) -> bool:
    """Whether two directory trees differ in content or in the files they hold."""
    comparison = filecmp.dircmp(left, right)
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return True
    _, mismatch, errors = filecmp.cmpfiles(left, right, comparison.common_files, shallow=False)
    if mismatch or errors:
        return True
    return any(trees_differ(left / name, right / name) for name in comparison.common_dirs)


def warn_on_stale_template(settings: Settings) -> None:
    """The module image bakes the workload manifests, so a stale template deploys the previous ones."""
    repo = operator_repo(settings)
    template = repo / MODULE_DIR / "config" / "manifests-template"
    if template.is_dir() and trees_differ(repo / "config", template):
        warn(f"config/ and {MODULE_DIR}/config/manifests-template/ in {repo} differ;")
        warn(f"run 'make -C {repo} sync-trustyai-module-manifests' to refresh the template")


def images(settings: Settings) -> list[str]:
    """The two operator images a load-path run builds."""
    return [settings.workload_img, settings.module_img]


def build_operators(settings: Settings) -> None:
    """Build the workload and module operator images from OPERATOR_REPO."""
    repo = operator_repo(settings)
    module = repo / MODULE_DIR
    warn_on_stale_template(settings)
    build(
        settings,
        settings.workload_img,
        repo,
        repo / "Dockerfile",
        "--build-arg",
        f"CI_CONTAINER_VERSION={settings.tag}",
    )
    build(settings, settings.module_img, module, module / "Dockerfile", "--build-arg", f"VERSION={settings.tag}")
    info(f"built {settings.workload_img} and {settings.module_img}")


def build_load_path_images(settings: Settings) -> None:
    """Build the operator images and clear the record their rebuild invalidates."""
    if settings.images_env_path.exists():
        info(f"removing {settings.images_env_path}: it records references this build replaces")
        settings.images_env_path.unlink()
    build_operators(settings)


def load(settings: Settings) -> None:
    """Load the built operator images into the Kind cluster.

    `kind load docker-image` shells out to docker even on the podman provider, so export
    each image and hand Kind the archive. One archive per image: `podman save` collapses
    several into a single manifest entry and would load the first under every tag.
    """
    built = images(settings)
    missing = [image for image in built if not image_present(settings, image)]
    if missing:
        raise HarnessError(f"{', '.join(missing)} is not in the engine's store; run 'k8s-tests images' first")

    kind_env = environment({"KIND_EXPERIMENTAL_PROVIDER": settings.kind_provider})
    with tempfile.TemporaryDirectory(prefix="k8s-tests-images-") as workdir:
        for image in built:
            archive = Path(workdir) / f"image-{image.replace('/', '-').replace(':', '-')}.tar"
            save(settings, image, archive)
            info(f"loading {image} into Kind cluster {settings.kind_cluster}")
            run([settings.kind, "load", "image-archive", "--name", settings.kind_cluster, archive], env=kind_env)
    info(f"loaded both operator images into {settings.kind_cluster}")


def require_cluster(settings: Settings) -> None:
    """Fail in seconds when the registry flow is asked for before the cluster exists."""
    from harness.cluster import clusters

    if settings.kind_cluster not in clusters(settings):
        raise HarnessError(
            f"Kind cluster {settings.kind_cluster} not found; create it with 'k8s-tests cluster' "
            "(or set KIND_PROVIDER to the engine that created it)"
        )


def require_checkout(path: str, name: str) -> Path:
    """A checkout the registry flow builds from, checked before anything is built."""
    if not path:
        raise HarnessError(f"{name} is not set; point it at a checkout, or place one beside this one")
    directory = Path(path).expanduser()
    if not directory.is_dir():
        raise HarnessError(f"{name}={directory} does not exist")
    return directory


def build_evalhub(settings: Settings, image: str) -> None:
    repo = require_checkout(settings.evalhub_repo, "EVALHUB_REPO")
    containerfile = repo / "Containerfile"
    if not containerfile.is_file():
        raise HarnessError(f"missing {containerfile}; set EVALHUB_REPO")
    args = [
        "--build-arg",
        "BUILD_NUMBER=k8s-tests",
        "--build-arg",
        f"BUILD_DATE={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ]
    if (repo / ".git").is_dir():
        revision = output(["git", "-C", repo, "rev-parse", "--short", "HEAD"], check=False)
        if revision:
            args += ["--build-arg", f"GIT_HASH={revision}"]
    build(settings, image, repo, containerfile, *args)


def build_adapter(settings: Settings, adapter: str, image: str) -> None:
    """Build one eval-hub-contrib adapter image, with the arguments its Containerfile takes."""
    repo = require_checkout(settings.evalhub_contrib_repo, "EVALHUB_CONTRIB_REPO")
    context = repo / registry.adapter_context(adapter)
    containerfile = context / "Containerfile"
    if not containerfile.is_file():
        raise HarnessError(f"missing {containerfile}; set EVALHUB_CONTRIB_REPO")
    if adapter == "toolbench-server":
        build(settings, image, context, containerfile, "--build-arg", "FETCH_FULL_CACHE=0")
    elif adapter == "promptfoo" and settings.base_image:
        build(settings, image, context, containerfile, "--build-arg", f"BASE_IMAGE={settings.base_image}")
    else:
        build(settings, image, context, containerfile)


def build_lmes(settings: Settings, image: str) -> None:
    """Build the lm_evaluation_harness job image from the fork's own Dockerfile.lmes-job."""
    repo = require_checkout(settings.lmes_repo, "LMES_REPO")
    containerfile = repo / LMES_DOCKERFILE
    if not containerfile.is_file():
        raise HarnessError(f"missing {containerfile}; set LMES_REPO")
    architecture = platform(settings).rpartition("/")[2]
    build(settings, image, repo, containerfile, "--build-arg", f"TARGETARCH={architecture}")


def push_and_maybe_remove(settings: Settings, image: str) -> None:
    """Push an image and, when asked, drop the local copy: the registry has it."""
    push(settings, image)
    if settings.remove_after_push:
        if run([container_engine(settings), "rmi", image], capture=True, check=False).returncode != 0:
            warn(f"could not remove {image} from the local store after pushing")


def images_all(settings: Settings) -> None:
    """Build every source image, push it to the local registry, and record the references."""
    require_cluster(settings)
    operator_repo(settings)
    if settings.skip_build and settings.remove_after_push:
        raise HarnessError(
            "SKIP_BUILD reuses the images in the local store, so it cannot be combined with REMOVE_AFTER_PUSH"
        )

    registry.ensure(settings)
    registry.configure_nodes(settings)

    recorded: dict[str, str] = {}
    built: list[str] = []

    workload = registry.registry_reference(settings, "trustyai-service-operator")
    module = registry.registry_reference(settings, "trustyai-operator-module")
    if settings.skip_build:
        require_present(settings, (workload, module))
    else:
        build_operators(settings.with_updates(workload_img=workload, module_img=module))
    for image in (workload, module):
        push_and_maybe_remove(settings, image)
    built += [workload, module]
    recorded.update(WORKLOAD_IMG=workload, MODULE_IMG=module)

    evalhub = registry.registry_reference(settings, "evalhub/evalhub")
    if settings.skip_build:
        require_present(settings, (evalhub,))
    else:
        build_evalhub(settings, evalhub)
    push_and_maybe_remove(settings, evalhub)
    built.append(evalhub)
    recorded["EVALHUB_IMG"] = evalhub

    for adapter in registry.selected_adapters(settings):
        image = registry.registry_reference(settings, f"evalhub/{registry.adapter_image_name(adapter)}")
        if settings.skip_build:
            require_present(settings, (image,))
        else:
            build_adapter(settings, adapter, image)
        push_and_maybe_remove(settings, image)
        built.append(image)
        match adapter:
            case "inspect":
                recorded["INSPECT_IMG"] = image
            case "lighteval":
                recorded["LIGHTEVAL_IMG"] = image

    if registry.builds_lmes(settings):
        lmes = registry.registry_reference(settings, "evalhub/community-lmes")
        if settings.skip_build:
            require_present(settings, (lmes,))
        else:
            build_lmes(settings, lmes)
        push_and_maybe_remove(settings, lmes)
        built.append(lmes)
        recorded["LMES_IMG"] = lmes

    write_record(settings, recorded)
    info(f"pushed {len(built)} images to {settings.registry_host}")
    info(f"image references written to {settings.images_env_path}")
    info("deploy with those images: k8s-tests deploy-registry")


def require_present(settings: Settings, images_to_check: tuple[str, ...]) -> None:
    for image in images_to_check:
        if not image_present(settings, image):
            raise HarnessError(f"SKIP_BUILD is set but {image} is not present")


def write_record(settings: Settings, recorded: dict[str, str]) -> None:
    """Write what `images-all` pushed, in the order the checks read it."""
    path = settings.images_env_path
    path.parent.mkdir(parents=True, exist_ok=True)
    order = ("WORKLOAD_IMG", "MODULE_IMG", "EVALHUB_IMG", "LMES_IMG", "INSPECT_IMG", "LIGHTEVAL_IMG")
    lines = [f"# Generated by k8s-tests images-all: images pushed to {settings.registry_host}"]
    lines += [f"{key}={recorded[key]}" for key in order if key in recorded]
    path.write_text("\n".join(lines) + "\n")


def clean(settings: Settings) -> None:
    """Remove the built images, the local registry and the HTML report, leaving the cluster."""
    if reachable_engine(settings) is None:
        warn("podman/docker is not reachable; leaving the built images and the local registry alone")
    else:
        for image in images(settings):
            remove(settings, image)
        remove_registry(settings)
    report_dir = settings.report_path.parent
    if report_dir.as_posix() not in ("", ".") and report_dir.is_dir():
        info(f"removing {report_dir}")
        shutil.rmtree(report_dir)


def remove_registry(settings: Settings) -> None:
    """Remove the registry container and the recorded images it and the local store hold.

    Removing the container drops the images the cluster pulled from it. Only the images
    the record names are dropped from the local store: one built and tagged there by hand
    is not this command's to delete.
    """
    engine = container_engine(settings)
    name = registry.registry_name(settings)
    if container_state(settings, name):
        info(f"removing local registry {name}")
        if run([engine, "container", "rm", "-f", name], capture=True, check=False).returncode != 0:
            warn(f"could not remove {name}")

    path = settings.images_env_path
    if not path.exists():
        return
    for line in record_lines(path):
        image = line.partition("=")[2]
        if image:
            remove(settings, image)
    info(f"removing {path}: every image it recorded is gone")
    path.unlink()


__all__ = [
    "LMES_DOCKERFILE",
    "MODULE_DIR",
    "build_adapter",
    "build_evalhub",
    "build_load_path_images",
    "build_lmes",
    "build_operators",
    "check_operator_repo",
    "clean",
    "images",
    "images_all",
    "load",
    "operator_repo",
    "require_checkout",
    "require_cluster",
    "trees_differ",
    "warn_on_stale_template",
    "write_record",
]
