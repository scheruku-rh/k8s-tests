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

from collections.abc import Iterable

import pytest
from kubernetes import client

from utilities.trustyai import crd_exists

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
