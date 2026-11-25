"""Exercise the operator's failure reporting with real kubelet image failures.

Two shapes are covered: an adapter image whose tag does not exist, which kubelet
keeps retrying (ImagePullBackOff) and the operator only reports once the
ten-minute grace period has passed, and a malformed image reference
(InvalidImageName), which is fatal without any grace period. Both must end with
the benchmark failed in EvalHub, the reason in its error message, and the
Kubernetes Job promoted to Failed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
import requests
from kubernetes import client

from utilities.evalhub import (
    EVALHUB_JOBS_PATH,
    api_headers,
    instance_tls_cert,
    read_job,
    submit_job,
    wait_for_container_env,
    wait_for_job,
)
from utilities.model import ModelEndpoint
from utilities.trustyai import WORKLOAD_OPERATOR_DEPLOYMENT

pytestmark = [
    pytest.mark.evalhub,
    pytest.mark.benchmark,
    pytest.mark.usefixtures("evalhub_tenant_ca", "reconciler_test_ca"),
]

PROVIDERS_PATH = "/api/v1/evaluations/providers"
FAILURE_PHASE = "trustyai.opendatahub.io/evaluation-phase"
FAILURE_REPORTED = "trustyai.opendatahub.io/evalhub-failure-reported"
POLL_INTERVAL = 5


@pytest.fixture(scope="module")
def reconciler_test_ca(
    kube_context: str,
    namespace: str,
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    evalhub_instance: dict,
    evalhub_instance_namespace: str,
) -> Iterator[None]:
    """Trust the harness-generated EvalHub certificate for the operator's callbacks on Kind.

    On OpenShift the operator already trusts the service CA that signs the
    in-cluster certificate, so the patch is Kind-only. The workload operator's
    Deployment is patched in place and restored afterwards; the module operator
    deploys it once, so nothing re-renders it under the patch mid-run.
    `evalhub_instance` is requested only for ordering: it is what creates the
    TLS secret `instance_tls_cert` reads.
    """
    if not kube_context.startswith("kind-"):
        yield
        return

    deployment = apps_client.read_namespaced_deployment(WORKLOAD_OPERATOR_DEPLOYMENT, namespace)
    manager = next(container for container in deployment.spec.template.spec.containers if container.name == "manager")
    env_name = "TRUSTYAI_EVALHUB_HTTP_EXTRA_CA_BUNDLE"
    previous = next((env for env in manager.env or [] if env.name == env_name), None)
    volume = f"evalhub-test-ca-{uuid4().hex[:8]}"
    mount_path = f"/var/run/{volume}"
    certificate = instance_tls_cert(core_client, evalhub_instance_namespace)
    core_client.create_namespaced_config_map(
        namespace,
        client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=volume),
            data={"ca.crt": certificate.decode()},
        ),
    )

    def patch(volume_patch: dict, mount_patch: dict, env_patch: dict) -> None:
        apps_client.patch_namespaced_deployment(
            name=WORKLOAD_OPERATOR_DEPLOYMENT,
            namespace=namespace,
            body={
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [volume_patch],
                            "containers": [{"name": "manager", "volumeMounts": [mount_patch], "env": [env_patch]}],
                        }
                    }
                }
            },
            _content_type="application/strategic-merge-patch+json",
        )

    def wait_for_rollout(expected: str) -> None:
        wait_for_container_env(apps_client, WORKLOAD_OPERATOR_DEPLOYMENT, namespace, "manager", env_name, expected)

    try:
        patch(
            {"name": volume, "configMap": {"name": volume}},
            {"name": volume, "mountPath": mount_path, "readOnly": True},
            {"name": env_name, "value": f"{mount_path}/ca.crt", "valueFrom": None},
        )
        wait_for_rollout(f"{mount_path}/ca.crt")
        yield
    finally:
        restored_env = (
            {"name": env_name, "$patch": "delete"}
            if previous is None
            else {
                "name": env_name,
                "value": previous.value,
                "valueFrom": apps_client.api_client.sanitize_for_serialization(previous.value_from),
            }
        )
        patch(
            {"name": volume, "$patch": "delete"},
            {"mountPath": mount_path, "$patch": "delete"},
            restored_env,
        )
        wait_for_rollout((previous.value or "") if previous is not None else "")
        core_client.delete_namespaced_config_map(volume, namespace)


@pytest.mark.parametrize(
    ("image", "reason", "minimum_wait", "deadline"),
    [
        (
            f"registry.k8s.io/pause:evalhub-missing-{uuid4().hex}",
            "ImagePullBackOff",
            570,
            750,
        ),
        ("registry.k8s.io/pause:invalid/tag", "InvalidImageName", 0, 120),
    ],
    ids=["image-pull-grace", "invalid-image-name"],
)
def test_reconciler_reports_image_failure(
    core_client: client.CoreV1Api,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    model_endpoint: ModelEndpoint,
    image: str,
    reason: str,
    minimum_wait: int,
    deadline: int,
) -> None:
    namespace = evalhub_tenant_namespace
    headers = api_headers(evalhub_tenant_token, namespace)
    provider_response = requests.post(
        f"{evalhub_api_base}{PROVIDERS_PATH}",
        headers=headers,
        json={
            "name": f"image-failure-{uuid4().hex}",
            "title": "Image failure regression",
            "runtime": {"k8s": {"image": image, "entrypoint": ["/unused"]}},
            "benchmarks": [{"id": "image-failure", "name": "Image failure", "category": "test"}],
        },
        verify=False,
        timeout=30,
    )
    assert provider_response.status_code == 201, provider_response.text
    provider_id = provider_response.json()["resource"]["id"]
    job_id = None
    try:
        submission = submit_job(
            evalhub_api_base,
            headers,
            {
                "name": "image-failure-regression",
                "model": {"name": model_endpoint.name, "url": model_endpoint.url},
                "benchmarks": [{"id": "image-failure", "provider_id": provider_id}],
            },
        )
        job_id = submission["resource"]["id"]
        selector = f"job_id={job_id}"
        # Reaching the first pull failure can take minutes on a Kind node whose
        # path to the registry is slow, so the window to observe it is generous.
        observation_deadline = time.monotonic() + 300
        observed_states = set()
        while time.monotonic() < observation_deadline:
            pods = core_client.list_namespaced_pod(namespace, label_selector=selector).items
            for pod in pods:
                for container in (pod.status.init_container_statuses or []) + (pod.status.container_statuses or []):
                    waiting = container.state.waiting
                    if waiting:
                        observed_states.add(f"{container.name}: {waiting.reason} ({waiting.message})")
            if any(state.startswith(f"adapter: {reason} (") for state in observed_states):
                break
            status = read_job(evalhub_api_base, headers, job_id)["status"]
            assert status["state"] in ("pending", "running"), (
                f"evaluation {job_id} ended before the adapter entered {reason}: "
                f"{status}; observed container states: {sorted(observed_states)}"
            )
            time.sleep(POLL_INTERVAL)
        else:
            pytest.fail(
                f"adapter for evaluation {job_id} never entered {reason}; "
                f"observed container states: {sorted(observed_states)}",
                pytrace=False,
            )

        # The operator measures the grace from kubelet's first BackOff Event, which can
        # precede this observation, so the wait stays below the operator's ten minutes.
        grace_end = time.monotonic() + minimum_wait
        while time.monotonic() < grace_end:
            status = read_job(evalhub_api_base, headers, job_id)["status"]
            assert status["state"] in ("pending", "running"), f"job ended inside grace: {status}"
            time.sleep(POLL_INTERVAL)

        finished = wait_for_job(
            evalhub_api_base, headers, job_id, timeout=deadline - minimum_wait, interval=POLL_INTERVAL
        )
        status = finished["status"]
        assert status["state"] == "failed", status
        benchmark = next(item for item in status["benchmarks"] if item["id"] == "image-failure")
        assert benchmark["status"] == "failed", benchmark
        assert reason in benchmark["error_message"]["message"], benchmark

        jobs_api = client.BatchV1Api(core_client.api_client)
        label_deadline = time.monotonic() + 30
        while time.monotonic() < label_deadline:
            jobs = jobs_api.list_namespaced_job(namespace, label_selector=selector).items
            if any(
                (job.metadata.labels or {}).get(FAILURE_PHASE) == "Failed"
                and (job.metadata.annotations or {}).get(FAILURE_REPORTED) == "true"
                for job in jobs
            ):
                break
            time.sleep(POLL_INTERVAL)
        else:
            pytest.fail(
                f"operator did not promote the Kubernetes Job for evaluation {job_id} to Failed "
                "with its failure reported",
                pytrace=False,
            )
    finally:
        try:
            if job_id:
                requests.delete(
                    f"{evalhub_api_base}{EVALHUB_JOBS_PATH}/{job_id}",
                    params={"hard_delete": "true"},
                    headers=headers,
                    verify=False,
                    timeout=30,
                ).raise_for_status()
        finally:
            requests.delete(
                f"{evalhub_api_base}{PROVIDERS_PATH}/{provider_id}",
                headers=headers,
                verify=False,
                timeout=30,
            ).raise_for_status()
