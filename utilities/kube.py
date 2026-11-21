"""Thin helpers over the Kubernetes Python client used by the tests."""

from __future__ import annotations

import logging
import os
import subprocess
import time

import pytest
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from utilities.portforward import kubectl_binary

DEFAULT_CONTEXT = "kind-trustyai"
DEFAULT_NAMESPACE = "system"

# Set by `k8s-tests test --keep-resources` (or pytest's --keep-resources): the fixtures
# then leave what they created on the cluster for inspection.
KEEP_RESOURCES_VAR = "KEEP_RESOURCES"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Lifetime of the bearer tokens the API checks mint for a ServiceAccount.
TOKEN_DURATION = "1h"

LOGGER = logging.getLogger(__name__)


def context_from_env() -> str:
    """Kubernetes context under test; the harness creates `kind-trustyai`."""
    return os.environ.get("KUBE_CONTEXT", DEFAULT_CONTEXT)


def namespace_from_env() -> str:
    """Namespace under test; the harness deploys into `system`."""
    return os.environ.get("NAMESPACE", DEFAULT_NAMESPACE)


def keep_resources() -> bool:
    """Whether the fixtures should leave their resources on the cluster."""
    return os.environ.get(KEEP_RESOURCES_VAR, "").strip().lower() in _TRUTHY


def load_clients(context: str) -> tuple[client.CoreV1Api, client.AppsV1Api]:
    """Build API clients for `context`.

    A missing kubeconfig or context means the harness was never pointed at a
    cluster, so the suite skips instead of failing. An unreachable API server is
    a genuine failure and surfaces from the first API call.
    """
    try:
        config.load_kube_config(context=context)
    except (ConfigException, FileNotFoundError) as exc:
        pytest.skip(f"no usable kubeconfig for context {context!r}: {exc}")
    api_client = client.ApiClient()
    LOGGER.info("using kubeconfig context %s (%s)", context, api_client.configuration.host)
    return client.CoreV1Api(api_client), client.AppsV1Api(api_client)


def read_deployment(apps_client: client.AppsV1Api, name: str, namespace: str) -> client.V1Deployment:
    """Return the named Deployment, failing with the harness hint when absent."""
    LOGGER.info("reading deployment %s/%s", namespace, name)
    try:
        return apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
    # Outside the handler, so the API error does not get chained onto the failure.
    pytest.fail(
        f"deployment {namespace}/{name} was not found on context {context_from_env()!r}; "
        "run 'k8s-tests up' or 'k8s-tests deploy' first, or point KUBE_CONTEXT/NAMESPACE at a cluster that has it",
        pytrace=False,
    )


def container_images(deployment: client.V1Deployment) -> list[str]:
    """Images the Deployment's pod template runs, in container order."""
    return [container.image for container in deployment.spec.template.spec.containers]


def _label_selector(deployment: client.V1Deployment) -> str:
    selector = deployment.spec.selector.match_labels or {}
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))


def _pods_for(deployment: client.V1Deployment, core_client: client.CoreV1Api, namespace: str) -> list[client.V1Pod]:
    return core_client.list_namespaced_pod(namespace=namespace, label_selector=_label_selector(deployment)).items


def _describe_conditions(deployment: client.V1Deployment) -> str:
    conditions = deployment.status.conditions or []
    return ", ".join(f"{condition.type}={condition.status} ({condition.reason})" for condition in conditions) or "none"


def _describe_pod(pod: client.V1Pod) -> str:
    statuses = pod.status.container_statuses or []
    ready = sum(1 for status in statuses if status.ready)
    restarts = sum(status.restart_count or 0 for status in statuses)
    return f"phase={pod.status.phase} containers={ready}/{len(statuses)} ready restarts={restarts}"


def _container_not_ready_reason(status: client.V1ContainerStatus) -> str:
    state = status.state
    if state is None:
        return "no state reported"
    if state.waiting is not None:
        # The message is where the kubelet says why: an image that has no manifest
        # for the node's architecture, a certificate it cannot read, and so on.
        waiting = state.waiting
        return f"waiting: {waiting.reason}: {waiting.message}" if waiting.message else f"waiting: {waiting.reason}"
    if state.terminated is not None:
        return f"terminated: {state.terminated.reason}"
    return "running but not ready"


def _pod_problems(pod: client.V1Pod) -> list[str]:
    name = pod.metadata.name
    problems = []
    if pod.status.phase != "Running":
        problems.append(f"pod/{name} phase is {pod.status.phase}")
    for container_status in pod.status.container_statuses or []:
        if not container_status.ready:
            reason = _container_not_ready_reason(container_status)
            problems.append(f"pod/{name} container {container_status.name} is not ready ({reason})")
    return problems


def assert_deployment_healthy(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, name: str, namespace: str
) -> None:
    """Fail with a readable summary unless the Deployment and its pods are healthy."""
    deployment = read_deployment(apps_client, name, namespace)
    desired = deployment.spec.replicas if deployment.spec.replicas is not None else 1
    status = deployment.status
    problems: list[str] = []

    LOGGER.info("%s/%s images: %s", namespace, name, container_images(deployment))
    LOGGER.info("%s/%s conditions: %s", namespace, name, _describe_conditions(deployment))
    available = status.available_replicas or 0
    LOGGER.info(
        "%s/%s replicas: %d/%d available, %d/%d ready, %d updated",
        namespace,
        name,
        available,
        desired,
        status.ready_replicas or 0,
        desired,
        status.updated_replicas or 0,
    )
    if available < desired:
        problems.append(f"available replicas {available}/{desired}")

    conditions = {condition.type: condition for condition in status.conditions or []}
    available_condition = conditions.get("Available")
    if available_condition is None:
        problems.append("Available condition is absent")
    else:
        LOGGER.info(
            "%s/%s Available condition: %s (%s)",
            namespace,
            name,
            available_condition.status,
            available_condition.reason,
        )
        if available_condition.status != "True":
            problems.append(f"Available condition is {available_condition.status} ({available_condition.reason})")

    pods = _pods_for(deployment, core_client, namespace)
    LOGGER.info(
        "%s/%s pods: %d matching selector %s", namespace, name, len(pods), _label_selector(deployment) or "<empty>"
    )
    if not pods:
        problems.append("no pods match the deployment selector")
    for pod in pods:
        LOGGER.info("%s/%s pod %s: %s", namespace, name, pod.metadata.name, _describe_pod(pod))
        problems.extend(_pod_problems(pod))

    if problems:
        details = "\n  - ".join(problems)
        pytest.fail(f"deployment {namespace}/{name} is not healthy:\n  - {details}", pytrace=False)
    LOGGER.info("%s/%s is healthy", namespace, name)


def _terminating_pods(deployment: client.V1Deployment, core_client: client.CoreV1Api, namespace: str) -> list[str]:
    """Names of pods that still match the selector but are being deleted."""
    return [
        pod.metadata.name
        for pod in _pods_for(deployment, core_client, namespace)
        if pod.metadata.deletion_timestamp is not None
    ]


def restart_deployment(
    apps_client: client.AppsV1Api,
    name: str,
    namespace: str,
    core_client: client.CoreV1Api,
    timeout: int = 300,
    interval: int = 5,
) -> client.V1Deployment:
    """Delete the Deployment's pods and wait for the replacements to be rolled out.

    The process the pods run is what a check restarts: the operator comes back with an
    empty cache and an empty informer set, which is the only way to see whether it still
    starts on this cluster. The Deployment itself is left alone, so `kubectl rollout
    restart` is not needed and its annotation does not have to be undone afterwards.
    """
    deployment = read_deployment(apps_client, name=name, namespace=namespace)
    pods = _pods_for(deployment, core_client, namespace)
    replaced = {pod.metadata.uid for pod in pods}
    LOGGER.info("restarting %s/%s: deleting %d pod(s)", namespace, name, len(pods))
    for pod in pods:
        core_client.delete_namespaced_pod(name=pod.metadata.name, namespace=namespace)
    wait_for_replacement_pods(
        apps_client, core_client, name=name, namespace=namespace, replaced=replaced, timeout=timeout, interval=interval
    )
    return wait_for_deployment_available(
        apps_client, name, namespace, timeout=timeout, interval=interval, core_client=core_client
    )


def wait_for_replacement_pods(
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    name: str,
    namespace: str,
    replaced: set[str],
    timeout: int = 300,
    interval: int = 5,
) -> None:
    """Wait until none of the pods that were replaced is listed and the new ones are Ready.

    The Deployment's status is the controller's last observation, so straight after a delete
    it can still say `ready_replicas == desired` while the only pod is already gone. A
    waiter that trusts that status (and treats an absent pod list as no pod terminating)
    returns before anything is serving, which is what a check that reads the pods next then
    sees. Whichever pods were deleted have to be gone and their replacements Ready first.
    """
    deadline = time.monotonic() + timeout
    while True:
        deployment = read_deployment(apps_client, name=name, namespace=namespace)
        wanted = deployment.spec.replicas if deployment.spec.replicas is not None else 1
        pods = _pods_for(deployment, core_client, namespace)
        remaining = sorted(pod.metadata.name for pod in pods if pod.metadata.uid in replaced)
        ready = [pod for pod in pods if _is_ready(pod)]
        if not remaining and len(ready) >= wanted:
            LOGGER.info("%s/%s has %d ready replacement pod(s)", namespace, name, len(ready))
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"the pods of {namespace}/{name} were not replaced within {timeout}s: "
                f"still terminating {', '.join(remaining) or 'none'}, {len(ready)}/{wanted} ready",
                pytrace=False,
            )
        LOGGER.info(
            "%s/%s waiting for replacements: still there %s, %d/%d ready",
            namespace,
            name,
            ", ".join(remaining) or "none",
            len(ready),
            wanted,
        )
        time.sleep(interval)


def _is_ready(pod: client.V1Pod) -> bool:
    """Whether the pod is running every container it asks for and is not being deleted."""
    if pod.metadata.deletion_timestamp is not None:
        return False
    statuses = pod.status.container_statuses or []
    return bool(statuses) and all(status.ready for status in statuses)


def wait_for_deployment_available(
    apps_client: client.AppsV1Api,
    name: str,
    namespace: str,
    timeout: int = 300,
    interval: int = 5,
    core_client: client.CoreV1Api | None = None,
) -> client.V1Deployment:
    """Poll until the Deployment has finished rolling out.

    With `core_client`, a pod set that still holds a terminating pod does not
    count: a Service keeps routing to it for as long as it is listed, so a check
    that immediately calls the Service can be served by the pod being replaced.
    """
    deadline = time.monotonic() + timeout
    deployment = read_deployment(apps_client, name=name, namespace=namespace)
    while True:
        desired = deployment.spec.replicas if deployment.spec.replicas is not None else 1
        status = deployment.status
        ready = status.ready_replicas or 0
        updated = status.updated_replicas or 0
        # A complete rollout, not just one available replica: while an old
        # ReplicaSet is still draining, two pods exist and a test that looks at
        # this Deployment's pods sees a moving target.
        terminating = _terminating_pods(deployment, core_client, namespace) if core_client else []
        if ready == desired and updated == desired and not status.unavailable_replicas and not terminating:
            LOGGER.info("%s/%s is rolled out: %d/%d ready", namespace, name, ready, desired)
            return deployment
        if terminating:
            LOGGER.info("%s/%s still terminating: %s", namespace, name, ", ".join(sorted(terminating)))
        if time.monotonic() >= deadline:
            break
        LOGGER.info(
            "%s/%s rolling out: %d/%d ready, %d/%d updated: %s",
            namespace,
            name,
            ready,
            desired,
            updated,
            desired,
            _describe_conditions(deployment),
        )
        time.sleep(interval)
        deployment = read_deployment(apps_client, name=name, namespace=namespace)
    # The Deployment's conditions only say that the rollout is still in progress;
    # the pods say why, and an image the node cannot pull is otherwise a timeout
    # with no cause in it.
    problems: list[str] = []
    if core_client is not None:
        for pod in _pods_for(deployment, core_client, namespace):
            problems.extend(_pod_problems(pod))
    details = "".join(f"\n  - {problem}" for problem in problems)
    pytest.fail(
        f"deployment {namespace}/{name} did not become available within {timeout}s: "
        f"{_describe_conditions(deployment)}{details}",
        pytrace=False,
    )


def mint_token(context: str, namespace: str, service_account: str, duration: str = TOKEN_DURATION) -> str:
    """Mint a bearer token for a ServiceAccount, the way a user would."""
    command = [
        kubectl_binary(),
        "--context",
        context,
        "--namespace",
        namespace,
        "create",
        "token",
        service_account,
        f"--duration={duration}",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"could not mint a token for {namespace}/{service_account}: {result.stderr.strip()}", pytrace=False)
    LOGGER.info("minted a token for %s/%s", namespace, service_account)
    return result.stdout.strip()
