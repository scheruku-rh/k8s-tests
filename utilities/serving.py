"""The optional KServe serving API, and what the two operators say about it.

TrustyAI integrates with KServe InferenceServices when they are installed: payload capture
reads a predictor and writes the logger field that points at the TrustyAI service. The
integration is optional and the stack runs without it, and since 3.7 nothing in either
operator watches the kind, so a cluster that never installs the CRD is a supported install
rather than a fault (trustyai-service-operator#1136 for the workload operator, #1137 for
the module operator).

The two operators report an absent API in their own words, which is what the checks in
`tests/test_trustyai_serving_optional.py` read:

- the module operator: `KServeAvailable=False`, reason `NotInstalled`, severity `Info`;
- the workload operator: `InferenceServicesPresent=False`, reason `ServingCRDNotFound`,
  with `Ready` left exactly where it was.

Up to 3.6 the module operator reported the same absence through a precondition, as
`PreConditionFailed`, which reads as an unmet requirement; keeping that wording out is the
point of the checks here, so it is named as the value that must not appear.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from utilities.trustyai import crd_exists

LOGGER = logging.getLogger(__name__)

SERVING_GROUP = "serving.kserve.io"
SERVING_KIND = "InferenceService"
SERVING_CRD_NAME = "inferenceservices.serving.kserve.io"

# The condition each operator reports the API's presence under.
MODULE_CONDITION = "KServeAvailable"
SERVICE_CONDITION = "InferenceServicesPresent"

# What the module operator reports, from 3.7 on.
REASON_AVAILABLE = "Available"
REASON_NOT_INSTALLED = "NotInstalled"
REASON_READ_FAILED = "ReadFailed"

# What the workload operator reports when the CRD is not installed.
REASON_SERVING_CRD_NOT_FOUND = "ServingCRDNotFound"

# The reason 3.6 reported an absent API with, and the one these checks must not find.
REASON_PRECONDITION_FAILED = "PreConditionFailed"

# The verbs a rule naming `*` grants, and the resources `*` covers in this API group.
ALL_VERBS = frozenset({"get", "list", "watch", "create", "update", "patch", "delete", "deletecollection"})
SERVING_RESOURCES = (
    "inferenceservices",
    "inferenceservices/finalizers",
    "inferenceservices/status",
    "servingruntimes",
    "servingruntimes/finalizers",
)

# The verb that makes an absent kind fatal: controller-runtime's cache starts an informer
# with list and watch, and an informer for a kind nothing serves never syncs.
WATCH = "watch"

# What a check that needs the API installs: the checkout's own stand-in, the file
# `k8s-tests deploy` applies for a run whose operators watch the kind. The operator's own
# tests check this CRD against the fields payload capture writes, so exercising it here
# exercises the adoption path they describe.
CRD_STAND_IN = ("tests", "crds", "serving.kserve.io_inferenceservices.yaml")
SERVING_VERSION = "v1beta1"
SERVING_PLURAL = "inferenceservices"

# The annotation KServe records the deployment mode in. It selects the write path:
# Raw/Standard get the https sink, Serverless/Knative only when the operator is told
# serverless is enabled, and anything else is skipped.
DEPLOYMENT_MODE_ANNOTATION = "serving.kserve.io/deploymentMode"
DEPLOYMENT_MODE_RAW = "RawDeployment"

# The one field payload capture writes, and the annotations KServe writes beside it.
LOGGER_FIELD_PATH = ("spec", "predictor", "logger")
LOGGER_MODE_ALL = "all"
LOGGER_ANNOTATIONS = (
    "internal.serving.kserve.io/logger",
    "internal.serving.kserve.io/logger-mode",
    "internal.serving.kserve.io/logger-sink-url",
)

# What the workload operator reports once it has adopted a usable serving API.
REASON_INFERENCE_SERVICES_FOUND = "InferenceServicesFound"


def serving_api_installed(api_client: client.ApiClient) -> bool:
    """Whether the InferenceService CRD is installed on this cluster."""
    return crd_exists(api_client, SERVING_CRD_NAME)


def require_serving_api_absent(api_client: client.ApiClient) -> None:
    """Skip the checks in a process where the serving API is installed.

    Getting here means the cluster this run points at has KServe, so the install under
    test is not the one the checks describe. Reinstalling it is not this suite's business,
    so the run says how to get the cluster it needs and stops.
    """
    if not serving_api_installed(api_client):
        return
    pytest.skip(
        f"{SERVING_CRD_NAME} is installed on this cluster, so the serving-free install cannot be "
        "tested against it: deploy without it (SERVING_CRD=never or --serving-crd never, which a "
        "--release 3.7 run does on its own) on a cluster whose CRD has not been created"
    )


def describe(condition: dict | None) -> str:
    """A condition as one readable line, for an assertion message."""
    if not condition:
        return "not reported"
    return (
        f"status={condition.get('status')} reason={condition.get('reason')} "
        f"severity={condition.get('severity')} message={condition.get('message')!r}"
    )


def grants(rules: Iterable[client.V1PolicyRule]) -> dict[str, set[str]]:
    """The verbs RBAC rules grant on the serving API, keyed by resource.

    A rule naming `*` as its group covers this one, a rule naming `*` as its verb grants
    every verb, and a rule naming `*` as its resource covers every resource in the group;
    all three are expanded here so a caller does not have to read wildcards itself.
    """
    granted: dict[str, set[str]] = {}
    for rule in rules:
        groups = set(rule.api_groups or [])
        if SERVING_GROUP not in groups and "*" not in groups:
            continue
        verbs = set(ALL_VERBS) if "*" in set(rule.verbs or []) else {verb.lower() for verb in rule.verbs or []}
        for resource in rule.resources or []:
            names = SERVING_RESOURCES if resource == "*" else (resource,)
            for name in names:
                granted.setdefault(name, set()).update(verbs)
    return granted


def describe_grants(granted: dict[str, set[str]]) -> str:
    """The grants as one readable line, for an assertion message."""
    if not granted:
        return "none"
    return ", ".join(f"{resource}: {','.join(sorted(verbs))}" for resource, verbs in sorted(granted.items()))


def rules_for_deployment(
    apps_client: client.AppsV1Api,
    rbac_client: client.RbacAuthorizationV1Api,
    name: str,
    namespace: str,
) -> list[client.V1PolicyRule]:
    """Every policy rule the named Deployment's ServiceAccount is bound to.

    Both binding kinds are read, because an operator installed by a platform and one
    installed by this harness need not agree on which they get, and the roles are
    resolved through each binding's own `roleRef` rather than by name.
    """
    deployment = apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    service_account = deployment.spec.template.spec.service_account_name
    rules: list[client.V1PolicyRule] = []
    for binding in rbac_client.list_cluster_role_binding().items:
        if not _binds(binding.subjects, service_account, namespace):
            continue
        role = rbac_client.read_cluster_role(name=binding.role_ref.name)
        rules.extend(role.rules or [])
    for binding in rbac_client.list_namespaced_role_binding(namespace=namespace).items:
        if not _binds(binding.subjects, service_account, namespace):
            continue
        role = rbac_client.read_namespaced_role(name=binding.role_ref.name, namespace=namespace)
        rules.extend(role.rules or [])
    return rules


def _binds(subjects: list[client.RbacV1Subject] | None, service_account: str, namespace: str) -> bool:
    """Whether a binding's subjects include this ServiceAccount."""
    for subject in subjects or []:
        if subject.kind != "ServiceAccount" or subject.name != service_account:
            continue
        if (subject.namespace or namespace) == namespace:
            return True
    return False


def logger_url(instance: str, namespace: str) -> str:
    """The sink a Raw/Standard deployment's logger is pointed at, as the operator builds it."""
    return f"https://{instance}.{namespace}.svc.cluster.local"


def install_serving_api(api_client: client.ApiClient, operator_repo: str, timeout: int = 120) -> bool:
    """Apply the checkout's InferenceService stand-in and wait for it to be Established.

    Returns whether this call created it: a cluster that already serves the kind keeps the
    CRD it has, and the caller must not delete one it did not install.
    """
    path = Path(operator_repo).expanduser() / Path(*CRD_STAND_IN)
    if not path.is_file():
        pytest.fail(
            f"{path} is missing; {operator_repo} does not look like a trustyai-service-operator checkout",
            pytrace=False,
        )
    crds = client.ApiextensionsV1Api(api_client)
    try:
        crds.create_custom_resource_definition(body=yaml.safe_load(path.read_text()))
    except ApiException as exc:
        if exc.status != 409:
            raise
        LOGGER.info("%s is already installed; leaving the API it serves in place", SERVING_CRD_NAME)
        return False
    LOGGER.info("installed %s from %s", SERVING_CRD_NAME, path)
    _wait_for_established(crds, timeout)
    return True


def _wait_for_established(crds: client.ApiextensionsV1Api, timeout: int) -> None:
    """Wait for the CRD to report Established, which is when its objects can be created."""
    deadline = time.monotonic() + timeout
    while True:
        crd = crds.read_custom_resource_definition(name=SERVING_CRD_NAME)
        conditions = {condition.type: condition.status for condition in crd.status.conditions or []}
        if conditions.get("Established") == "True":
            LOGGER.info("%s is Established", SERVING_CRD_NAME)
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"{SERVING_CRD_NAME} did not reach Established within {timeout}s: {conditions}", pytrace=False)
        time.sleep(3)


def remove_serving_api(api_client: client.ApiClient, timeout: int = 120, interval: int = 3) -> None:
    """Delete the CRD this suite installed, and wait for the API server to stop serving it."""
    crds = client.ApiextensionsV1Api(api_client)
    try:
        crds.delete_custom_resource_definition(name=SERVING_CRD_NAME)
    except ApiException as exc:
        if exc.status != 404:
            raise
    deadline = time.monotonic() + timeout
    while True:
        try:
            crds.read_custom_resource_definition(name=SERVING_CRD_NAME)
        except ApiException as exc:
            if exc.status == 404:
                LOGGER.info("%s is gone again", SERVING_CRD_NAME)
                return
            raise
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{SERVING_CRD_NAME} is still there {timeout}s after deletion, so this run leaves the cluster "
                "with a serving API the next one does not expect",
                pytrace=False,
            )
        time.sleep(interval)


def mock_inference_service(name: str, annotations: dict[str, str] | None = None) -> dict[str, Any]:
    """The smallest InferenceService that is a valid object and a RawDeployment.

    `spec.predictor` is the only thing the CRD requires of it, and the operator only reads
    the object's annotations and its logger, so nothing else is filled in: a mock that was
    a working predictor would need a model server this harness does not run.
    """
    return {
        "apiVersion": f"{SERVING_GROUP}/{SERVING_VERSION}",
        "kind": SERVING_KIND,
        "metadata": {
            "name": name,
            "annotations": {DEPLOYMENT_MODE_ANNOTATION: DEPLOYMENT_MODE_RAW, **(annotations or {})},
        },
        "spec": {"predictor": {}},
    }


def create_mock_inference_service(
    custom_api: client.CustomObjectsApi,
    namespace: str,
    name: str,
    annotations: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create the mock InferenceService in `namespace`, returning what the API server stored."""
    body = mock_inference_service(name, annotations)
    created = custom_api.create_namespaced_custom_object(
        group=SERVING_GROUP, version=SERVING_VERSION, namespace=namespace, plural=SERVING_PLURAL, body=body
    )
    LOGGER.info("created %s/%s in %s", SERVING_KIND, name, namespace)
    return created


def read_inference_service(custom_api: client.CustomObjectsApi, namespace: str, name: str) -> dict[str, Any]:
    """Read the mock InferenceService."""
    return custom_api.get_namespaced_custom_object(
        group=SERVING_GROUP, version=SERVING_VERSION, namespace=namespace, plural=SERVING_PLURAL, name=name
    )


def logger_of(resource: dict[str, Any]) -> dict[str, Any] | None:
    """The logger an InferenceService carries, or None when it carries none."""
    current: Any = resource
    for step in LOGGER_FIELD_PATH:
        if not isinstance(current, dict):
            return None
        current = current.get(step)
    return current if isinstance(current, dict) and current else None


def annotations_of(resource: dict[str, Any]) -> dict[str, str]:
    """The object's annotations, empty when it carries none."""
    return (resource.get("metadata") or {}).get("annotations") or {}


def wait_for_logger(
    custom_api: client.CustomObjectsApi,
    namespace: str,
    name: str,
    url: str,
    timeout: int = 180,
    interval: int = 5,
) -> dict[str, Any]:
    """Poll until payload capture is configured on the object, and return what is stored."""
    desired = {"mode": LOGGER_MODE_ALL, "url": url}
    deadline = time.monotonic() + timeout
    while True:
        resource = read_inference_service(custom_api, namespace, name)
        logger = logger_of(resource)
        if logger == desired:
            LOGGER.info("%s/%s carries the payload-capture logger %s", namespace, name, logger)
            return resource
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{SERVING_KIND}/{name} in {namespace} did not get the payload-capture logger within {timeout}s: "
                f"expected {desired}, it carries {logger}",
                pytrace=False,
            )
        LOGGER.info("%s/%s logger is %s, waiting for %s", namespace, name, logger, desired)
        time.sleep(interval)


def wait_for_logger_removed(
    custom_api: client.CustomObjectsApi,
    namespace: str,
    name: str,
    timeout: int = 180,
    interval: int = 5,
) -> dict[str, Any]:
    """Poll until the logger and the annotations this operator wrote are gone."""
    deadline = time.monotonic() + timeout
    while True:
        resource = read_inference_service(custom_api, namespace, name)
        logger = logger_of(resource)
        left = sorted(set(annotations_of(resource)) & set(LOGGER_ANNOTATIONS))
        if logger is None and not left:
            LOGGER.info("%s/%s carries neither the logger nor its annotations", namespace, name)
            return resource
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{SERVING_KIND}/{name} in {namespace} still carries {logger} and the annotations {left} "
                f"{timeout}s after payload capture was removed",
                pytrace=False,
            )
        LOGGER.info("%s/%s logger is %s and annotations %s, waiting for both to go", namespace, name, logger, left)
        time.sleep(interval)
