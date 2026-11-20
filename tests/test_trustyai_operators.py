"""Health checks for the TrustyAI operators deployed by the Kind harness.

On a modular checkout the workload operator is deployed by the module operator, so both
Deployments appearing and staying Ready is the first end-to-end signal that the harness
works. A pre-module checkout (3.5 and earlier) has only the workload operator: the
module operator's rows are skipped by release when the run declares one, and by what the
cluster has when it does not.
"""

from __future__ import annotations

import logging

import pytest
from kubernetes import client

from utilities.kube import assert_deployment_healthy, container_images, read_deployment
from utilities.release import release
from utilities.trustyai import MODULE_OPERATOR_DEPLOYMENT, deployment_exists

LOGGER = logging.getLogger(__name__)

OPERATOR_DEPLOYMENTS = [
    pytest.param(
        MODULE_OPERATOR_DEPLOYMENT,
        id="module-operator",
        marks=[pytest.mark.module, release(min="3.6")],
    ),
    pytest.param(
        "trustyai-service-operator-controller-manager",
        id="workload-operator",
        marks=pytest.mark.workload,
    ),
]

EXPECTED_REPLICAS = 1


def _deployment(apps_client: client.AppsV1Api, namespace: str, name: str) -> client.V1Deployment:
    """The parametrized Deployment, skipped on a layout that does not deploy it.

    The module operator's rows also carry a release bound, which skips them before the
    body runs when a release without one was declared; this keeps them honest when no
    release was declared and the cluster simply has no module operator.
    """
    require_layout(apps_client, namespace, name)
    return read_deployment(apps_client, name=name, namespace=namespace)


def require_layout(apps_client: client.AppsV1Api, namespace: str, name: str) -> None:
    """Skip a row whose Deployment this layout does not deploy."""
    if name == MODULE_OPERATOR_DEPLOYMENT and not deployment_exists(apps_client, name, namespace):
        pytest.skip(f"deployment {namespace}/{name} is not present; this cluster has no module operator")


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_exists(apps_client: client.AppsV1Api, namespace: str, name: str) -> None:
    LOGGER.info("checking that deployment %s/%s exists", namespace, name)
    deployment = _deployment(apps_client, namespace, name)
    LOGGER.info(
        "deployment %s/%s exists: %s replica(s), image(s) %s",
        namespace,
        name,
        deployment.spec.replicas,
        container_images(deployment),
    )

    assert deployment.metadata.name == name
    assert deployment.spec.replicas, f"deployment {namespace}/{name} asks for no replicas"


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_has_single_replica(apps_client: client.AppsV1Api, namespace: str, name: str) -> None:
    LOGGER.info("checking that deployment %s/%s asks for %d replica", namespace, name, EXPECTED_REPLICAS)
    deployment = _deployment(apps_client, namespace, name)
    LOGGER.info(
        "deployment %s/%s wants %s replica(s), %s ready",
        namespace,
        name,
        deployment.spec.replicas,
        deployment.status.ready_replicas or 0,
    )

    assert deployment.spec.replicas == EXPECTED_REPLICAS, (
        f"deployment {namespace}/{name} asks for {deployment.spec.replicas} replica(s), expected {EXPECTED_REPLICAS}"
    )


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_is_healthy(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, namespace: str, name: str
) -> None:
    LOGGER.info("checking that deployment %s/%s and its pods are healthy", namespace, name)
    require_layout(apps_client, namespace, name)
    assert_deployment_healthy(apps_client, core_client, name=name, namespace=namespace)
