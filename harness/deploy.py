"""Deploy the module operator, its module resource, and the workload operator under it.

The kustomize overlay is rendered from a temporary copy of the checkout's
`trustyai-operator-module/config`, so the operator image, the workload image it renders
and the target namespace can all be overridden without editing the checkout. The
operands on top - EvalHub instances, TrustyAIService, the jobs - are the tests' business.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
import yaml
from kubernetes.client.exceptions import ApiException

from harness.cluster import kube
from harness.config import (
    IMAGES_ENV_DEPLOYED_MARKER,
    Settings,
    is_deployed_record,
    record_lines,
    record_values,
)
from harness.engine import split_image
from harness.images import MODULE_DIR, operator_repo
from harness.shell import HarnessError, info, warn
from utilities.certs import generate_serving_cert, service_dns_names

# Kind has no OpenShift Routes, Prometheus Operator or KServe, but the rendered
# manifests reference their types and the watchers need them to exist.
EXTERNAL_CRDS = (
    ("route_crd.yaml", "routes.route.openshift.io"),
    ("monitoring.coreos.com_prometheuses.yaml", "prometheuses.monitoring.coreos.com"),
    ("monitoring.coreos.com_servicemonitors.yaml", "servicemonitors.monitoring.coreos.com"),
    ("serving.kserve.io_inferenceservices.yaml", "inferenceservices.serving.kserve.io"),
)

# The certificates OpenShift's service CA would issue, and the Services their names cover.
TLS_SECRETS = ("webhook-server-cert", "trustyai-service-operator-metrics-tls")
CERT_SERVICES = ("trustyai-service-operator-webhook-service", "trustyai-service-operator-metrics-service")

MODULE_OPERATOR_DEPLOYMENT = "trustyai-operator-module-controller-manager"
MODULE_OPERATOR_IMAGE = "trustyai-operator-module-controller"

# Each RELATED_IMAGE_* variable is the module operator's own mapping of a workload
# params.env key onto the platform variable that fills it.
RELATED_IMAGES = (
    ("RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE", "trustyai_service_img", "TrustyAI service"),
    ("RELATED_IMAGE_ODH_EVAL_HUB_IMAGE", "evalhub_img", "EvalHub"),
    ("RELATED_IMAGE_ODH_KUBE_RBAC_PROXY_IMAGE", "kube_rbac_proxy_img", "kube-rbac-proxy"),
    ("RELATED_IMAGE_ODH_TA_LMES_JOB_IMAGE", "lmes_img", "lm_evaluation_harness adapter"),
    ("RELATED_IMAGE_ODH_EVAL_HUB_INSPECTAI_IMAGE", "inspect_img", "Inspect AI adapter"),
    ("RELATED_IMAGE_ODH_EVAL_HUB_LIGHTEVAL_IMAGE", "lighteval_img", "LightEval adapter"),
)


def related_image_env(settings: Settings) -> list[tuple[str, str]]:
    """The RELATED_IMAGE_* variables the module operator hands to the workload operator."""
    variables = [("RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE", settings.workload_img)]
    for envvar, field, _ in RELATED_IMAGES:
        value = getattr(settings, field)
        if value:
            variables.append((envvar, value))
    return variables


def ensure_namespace(settings: Settings) -> None:
    info(f"creating namespace {settings.namespace}")
    manifest = kube(settings, "create", "namespace", settings.namespace, "--dry-run=client", "-o", "yaml", capture=True)
    kube(settings, "apply", "-f", "-", input_text=manifest)


def apply_external_crds(settings: Settings) -> None:
    directory = operator_repo(settings) / "tests" / "crds"
    if not directory.is_dir():
        raise HarnessError(f"missing {directory} (external CRD stand-ins)")
    info(f"applying external CRDs from {directory}")
    for filename, _ in EXTERNAL_CRDS:
        path = directory / filename
        if not path.is_file():
            raise HarnessError(f"missing {path}")
        kube(settings, "apply", "-f", path, capture=True)
    for _, name in EXTERNAL_CRDS:
        kube(
            settings,
            "wait",
            "--for=condition=Established",
            f"crd/{name}",
            f"--timeout={settings.timeout}",
            capture=True,
        )


def ensure_tls_secrets(settings: Settings, workdir: Path) -> None:
    """Create the webhook and metrics certificates once, so repeat deploys keep the same one."""
    if kube(settings, "-n", settings.namespace, "get", "secret", "webhook-server-cert", check=False, capture=True):
        return
    info("creating the webhook and metrics TLS secrets")
    names = [
        name for service in CERT_SERVICES for name in service_dns_names(service=service, namespace=settings.namespace)
    ]
    certificate, key = generate_serving_cert(dns_names=names)
    certs = workdir / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    (certs / "tls.crt").write_bytes(certificate)
    (certs / "tls.key").write_bytes(key)
    (certs / "tls.key").chmod(0o600)
    for secret in TLS_SECRETS:
        kube(
            settings,
            "-n",
            settings.namespace,
            "create",
            "secret",
            "tls",
            secret,
            f"--cert={certs / 'tls.crt'}",
            f"--key={certs / 'tls.key'}",
        )


def render_module_operator(settings: Settings, workdir: Path) -> Path:
    """Write the kustomization that deploys the module operator with this run's images."""
    module_config = operator_repo(settings) / MODULE_DIR / "config"
    if not (module_config / "default").is_dir():
        raise HarnessError(f"missing {module_config}/default")
    shutil.copytree(module_config, workdir / "config")

    image_name, image_tag = split_image(settings.module_img)
    operations = [
        {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": name, "value": value}}
        for name, value in related_image_env(settings)
    ]
    kustomization = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": settings.namespace,
        "resources": ["./config/default"],
        "images": [{"name": MODULE_OPERATOR_IMAGE, "newName": image_name, "newTag": image_tag}],
        "patches": [
            {
                "target": {"group": "apps", "version": "v1", "kind": "Deployment", "name": MODULE_OPERATOR_DEPLOYMENT},
                "patch": yaml.safe_dump(operations, sort_keys=False),
            }
        ],
    }
    (workdir / "kustomization.yaml").write_text(yaml.safe_dump(kustomization, sort_keys=False))
    return workdir


def seed_prometheus(settings: Settings) -> None:
    info("seeding the Prometheus instance the module operator expects")
    kube(
        settings,
        "apply",
        "-f",
        "-",
        input_text=yaml.safe_dump(
            {
                "apiVersion": "monitoring.coreos.com/v1",
                "kind": "Prometheus",
                "metadata": {"name": "e2e-prometheus", "namespace": settings.namespace},
            },
            sort_keys=False,
        ),
    )


def announce_operands(settings: Settings) -> None:
    for _, field, label in RELATED_IMAGES:
        value = getattr(settings, field)
        if value:
            info(f"passing {label} image {value} to the workload operator")


def bring_up(settings: Settings) -> None:
    """Apply the module resource and wait for the workload operator it deploys."""
    from utilities import stack

    settings.export()
    info("applying the TrustyAI module resource and waiting for the workload operator")
    try:
        stack.bring_up()
    except (pytest.skip.Exception, pytest.fail.Exception) as exc:
        raise HarnessError(str(exc)) from exc
    except ApiException as exc:
        raise HarnessError(f"{exc.status} {exc.reason}: {(exc.body or '').strip()}") from exc


def deploy(settings: Settings, *, next_step: bool = True) -> None:
    """Deploy the module operator and everything under it."""
    operator_repo(settings)
    kept = keep_used_images(settings)

    with tempfile.TemporaryDirectory(prefix="k8s-tests-deploy-") as workdir:
        path = Path(workdir)
        ensure_namespace(settings)
        apply_external_crds(settings)
        render_module_operator(settings, path)
        info(f"deploying the module operator ({settings.module_img}) with workload image {settings.workload_img}")
        announce_operands(settings)
        kube(settings, "apply", "-k", path, "--server-side=true")
        # rollout status, not Available: an existing pod set satisfies Available while a
        # new image is still rolling out.
        info("waiting for the module operator deployment")
        kube(
            settings,
            "-n",
            settings.namespace,
            "rollout",
            "status",
            f"deployment/{MODULE_OPERATOR_DEPLOYMENT}",
            f"--timeout={settings.timeout}",
        )
        seed_prometheus(settings)
        ensure_tls_secrets(settings, path)

    bring_up(settings)
    stamp_record(settings, kept)
    info(f"TrustyAI is deployed in namespace {settings.namespace}")
    if next_step:
        info("its operands are created by the tests: k8s-tests test")


def keep_used_images(settings: Settings) -> list[str] | None:
    """Drop the recorded images this deployment does not use, and say what is left.

    A build that was pushed but never deployed, or a deploy that failed half way, must not
    leave a file the checks treat as a record of what is running: the record stays
    unstamped until `stamp_record` runs at the end of a successful deploy.
    """
    path = settings.images_env_path
    if not path.exists():
        return None
    kept: list[str] = []
    dropped: list[str] = []
    for line in record_lines(path):
        key, _, value = line.partition("=")
        if value and value != getattr(settings, key.lower(), None):
            dropped.append(line)
        else:
            kept.append(line)
    if dropped:
        warn(f"{path} records images this deploy does not use ({' '.join(dropped)}); dropping them")
    if not kept:
        warn(f"removing {path}: it records no image this deploy uses")
        path.unlink()
        return None
    return kept


def stamp_record(settings: Settings, kept: list[str] | None) -> None:
    """Mark the record as describing this deployment, now that the stack runs it."""
    if kept is None:
        return
    path = settings.images_env_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([f"# {IMAGES_ENV_DEPLOYED_MARKER}", *kept]) + "\n")
    info(f"recorded the deployed images in {path}")


def registry_deploy(settings: Settings) -> None:
    """Deploy the images `images-all` pushed, reading the references it recorded."""
    path = settings.images_env_path
    if not path.exists():
        raise HarnessError(f"{path} is missing; run 'k8s-tests images-all' first")
    recorded = record_values(path)
    if is_deployed_record(path):
        info(f"{path} already describes a deployment; reusing its references")
    deploy(settings.with_image_record(recorded))


def registry_up(settings: Settings) -> None:
    """Create the cluster, build and push every image, then deploy them."""
    from harness import cluster, images

    cluster.create(settings)
    images.images_all(settings)
    registry_deploy(settings)


__all__ = [
    "EXTERNAL_CRDS",
    "MODULE_OPERATOR_DEPLOYMENT",
    "RELATED_IMAGES",
    "TLS_SECRETS",
    "apply_external_crds",
    "announce_operands",
    "bring_up",
    "deploy",
    "ensure_namespace",
    "ensure_tls_secrets",
    "keep_used_images",
    "registry_deploy",
    "registry_up",
    "related_image_env",
    "render_module_operator",
    "seed_prometheus",
    "stamp_record",
]
