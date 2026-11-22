"""An MLflow tracking server the ATIF checks own.

EvalHub records a job's run in MLflow when its instance is told where the tracking
server is (`MLFLOW_TRACKING_URI` in `spec.env`) and the job names an experiment; the
adapter then writes its report artifacts there through eval-hub-sdk. Kind has no
MLflow operator, so the checks deploy a plain tracking server: one Deployment serving
the REST API and the artifact server from a PersistentVolumeClaim, so an artifact
outlives the job Pod that wrote it and can be read back here.

`--artifacts-destination` with MLflow's default `mlflow-artifacts:` artifact root is
the shape eval-hub-sdk uploads to (`/api/2.0/mlflow-artifacts/artifacts/<path>`).
A vanilla server ignores the `X-MLFLOW-WORKSPACE` header the SDK sends, so no
workspace plugin is needed, and it needs no token: the projected ServiceAccount token
EvalHub mounts into job Pods is simply not checked.

The checks read artifacts over a forwarded port with plain `requests`, against the
same endpoints the SDK writes to, so what the checks see is what a consumer sees.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from utilities.certs import generate_serving_cert
from utilities.kube import wait_for_deployment_available
from utilities.portforward import allocate_local_ports, port_forward, release_local_ports

LOGGER = logging.getLogger(__name__)

NAME = "mlflow"
PORT = 5000
IMAGE_VAR = "MLFLOW_IMG"
# MLflow 3 serves both APIs the SDK uses; 2.x serves the REST API but not the
# artifacts server path the SDK uploads to.
DEFAULT_IMAGE = "ghcr.io/mlflow/mlflow:v3.1.0"
STORE_DIR = "/mlflow"
BACKEND_URI = f"sqlite:///{STORE_DIR}/mlflow.db"
ARTIFACT_DESTINATION = f"{STORE_DIR}/artifacts"
CA_BUNDLE_CONFIG_MAP = "mlflow-ca"
CA_BUNDLE_KEY = "ca-bundle.crt"
STORE_CLAIM = "mlflow-store"
STORE_SIZE = "2Gi"
DEPLOY_TIMEOUT = 300
HEALTH_PATH = "/health"
API = "/api/2.0/mlflow"
ARTIFACTS_API = "/api/2.0/mlflow-artifacts/artifacts"
FORWARDED_PORT = 15000
LABELS = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/part-of": "k8s-tests"}


def image() -> str:
    return os.environ.get(IMAGE_VAR) or DEFAULT_IMAGE


def seed_ca_bundle(core_client: client.CoreV1Api, namespace: str) -> dict[str, str]:
    """Create a trust bundle the EvalHub instance can point `spec.mlflow.tls.caBundle` at.

    EvalHub parses `MLFLOW_CA_CERT_PATH` whenever it is told where MLflow is, and the
    operator builds that file from the service-serving CA and any user bundle. Kind has
    no service-serving CA, so without a user bundle the operator writes an empty file and
    EvalHub refuses to start ("file contains no valid PEM certificates"). The tracking
    server this section deploys is plain HTTP, so the certificate only has to be valid
    PEM, not the server's own.
    """
    cert_pem, _ = generate_serving_cert(dns_names=[f"{NAME}.{namespace}.svc"])
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=CA_BUNDLE_CONFIG_MAP, namespace=namespace),
        data={CA_BUNDLE_KEY: cert_pem.decode()},
    )
    try:
        core_client.create_namespaced_config_map(namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core_client.replace_namespaced_config_map(name=CA_BUNDLE_CONFIG_MAP, namespace=namespace, body=body)
    LOGGER.info("seeded %s/%s with a trust bundle for EvalHub", namespace, CA_BUNDLE_CONFIG_MAP)
    return {"name": CA_BUNDLE_CONFIG_MAP, "key": CA_BUNDLE_KEY}


def tracking_uri(namespace: str) -> str:
    """The in-cluster address EvalHub and its job Pods use as MLFLOW_TRACKING_URI."""
    return f"http://{NAME}.{namespace}.svc.cluster.local:{PORT}"


def _claim(namespace: str) -> client.V1PersistentVolumeClaim:
    return client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=STORE_CLAIM, namespace=namespace, labels=LABELS),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            # A plain mapping: the API client's PVC spec takes the resource-requirements
            # model, and which one it is differs between client releases.
            resources={"requests": {"storage": STORE_SIZE}},
        ),
    )


def _deployment(namespace: str, image_name: str) -> client.V1Deployment:
    container = client.V1Container(
        name=NAME,
        image=image_name,
        # The public image has no ENTRYPOINT, so the binary has to be named here.
        command=[
            "mlflow",
            "server",
            "--host",
            "0.0.0.0",
            "--port",
            str(PORT),
            "--backend-store-uri",
            BACKEND_URI,
            "--artifacts-destination",
            ARTIFACT_DESTINATION,
            "--serve-artifacts",
        ],
        ports=[client.V1ContainerPort(name="http", container_port=PORT, protocol="TCP")],
        readiness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path=HEALTH_PATH, port=PORT),
            initial_delay_seconds=5,
            period_seconds=5,
            timeout_seconds=3,
            failure_threshold=12,
        ),
        # The public MLflow image has no unprivileged user and writes its store as root;
        # it drops capabilities and reaches nothing outside its own claim.
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            capabilities=client.V1Capabilities(drop=["ALL"]),
        ),
        volume_mounts=[client.V1VolumeMount(name="store", mount_path=STORE_DIR)],
    )
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=NAME, namespace=namespace, labels=LABELS),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=LABELS),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=LABELS),
                spec=client.V1PodSpec(
                    containers=[container],
                    volumes=[
                        client.V1Volume(
                            name="store",
                            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=STORE_CLAIM),
                        )
                    ],
                ),
            ),
        ),
    )


def _service(namespace: str) -> client.V1Service:
    return client.V1Service(
        metadata=client.V1ObjectMeta(name=NAME, namespace=namespace, labels=LABELS),
        spec=client.V1ServiceSpec(
            selector=LABELS,
            ports=[client.V1ServicePort(name="http", port=PORT, target_port=PORT, protocol="TCP")],
        ),
    )


def deploy_mlflow(
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    namespace: str,
    image_name: str | None = None,
) -> str:
    """Create the store claim, the tracking server and its Service; return its in-cluster URI."""
    image_name = image_name or image()
    LOGGER.info("deploying MLflow %s in namespace %s", image_name, namespace)
    for create, body, kind in (
        (core_client.create_namespaced_persistent_volume_claim, _claim(namespace), "PersistentVolumeClaim"),
        (core_client.create_namespaced_service, _service(namespace), "Service"),
    ):
        try:
            create(namespace=namespace, body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            LOGGER.info("%s %s/%s already exists; leaving it in place", kind, namespace, body.metadata.name)
    deployment = _deployment(namespace, image_name)
    try:
        apps_client.create_namespaced_deployment(namespace=namespace, body=deployment)
    except ApiException as exc:
        if exc.status != 409:
            raise
        apps_client.replace_namespaced_deployment(name=NAME, namespace=namespace, body=deployment)
    wait_for_deployment_available(apps_client, NAME, namespace, timeout=DEPLOY_TIMEOUT, core_client=core_client)
    return tracking_uri(namespace)


def remove_mlflow(core_client: client.CoreV1Api, apps_client: client.AppsV1Api, namespace: str) -> None:
    """Remove the server; the claim is left to the namespace deletion that follows."""
    LOGGER.info("removing the MLflow tracking server from namespace %s", namespace)
    for deleter, kwargs in (
        (apps_client.delete_namespaced_deployment, {"name": NAME, "namespace": namespace}),
        (core_client.delete_namespaced_service, {"name": NAME, "namespace": namespace}),
    ):
        with contextlib.suppress(ApiException):
            deleter(**kwargs)


class TrackingServer:
    """The MLflow REST calls these checks make, against a forwarded port."""

    def __init__(self, base_url: str, workspace: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        # EvalHub tells a job pod its tenant as MLFLOW_WORKSPACE, and eval-hub-sdk prefixes
        # the artifact path with it, so a run's artifact_uri and the stored object differ.
        self.workspace = workspace

    def _get(self, path: str, **params: Any) -> requests.Response:
        response = requests.get(f"{self.base_url}{path}", params=params, timeout=30)
        if response.status_code >= 400:
            pytest.fail(f"GET {path} answered {response.status_code}: {response.text[:500]}", pytrace=False)
        return response

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(f"{self.base_url}{path}", json=payload, timeout=30)
        if response.status_code >= 400:
            pytest.fail(f"POST {path} answered {response.status_code}: {response.text[:500]}", pytrace=False)
        return response.json()

    def experiment_id(self, name: str) -> str:
        """The id of the named experiment, failing with what exists instead."""
        experiment = self.find_experiment(name)
        if experiment is None:
            names = [item.get("name") for item in self.experiments()]
            pytest.fail(f"MLflow has no experiment {name!r}; it has {names}", pytrace=False)
        return experiment["experiment_id"]

    def find_experiment(self, name: str) -> dict[str, Any] | None:
        found = (
            self._post(
                f"{API}/experiments/search",
                {"filter": f"name = '{name}'", "max_results": 10},
            ).get("experiments")
            or []
        )
        for experiment in found:
            if experiment.get("name") == name:
                return experiment
        return None

    def experiments(self) -> list[dict[str, Any]]:
        return self._post(f"{API}/experiments/search", {"max_results": 100}).get("experiments") or []

    def runs(self, experiment_id: str) -> list[dict[str, Any]]:
        return (
            self._post(
                f"{API}/runs/search",
                {"experiment_ids": [experiment_id], "max_results": 50, "order_by": ["attributes.start_time DESC"]},
            ).get("runs")
            or []
        )

    def _artifact_candidates(self, run: dict[str, Any], path: str) -> list[str]:
        """The artifact-server paths a run's artifact may live at, in the order to try."""
        artifact_uri = ((run.get("info") or {}).get("artifact_uri") or "").rstrip("/")
        if not artifact_uri.startswith("mlflow-artifacts:/"):
            pytest.fail(
                f"run artifact_uri is {artifact_uri!r}; this deployment serves artifacts from "
                f"a mlflow-artifacts: root, so a different root means MLflow was configured differently",
                pytrace=False,
            )
        root = artifact_uri.removeprefix("mlflow-artifacts:/").lstrip("/")
        candidates = [f"{ARTIFACTS_API}/{root}/{path.lstrip('/')}"]
        if self.workspace:
            candidates.append(f"{ARTIFACTS_API}/workspaces/{self.workspace}/{root}/{path.lstrip('/')}")
        return candidates

    def find_artifact(self, run: dict[str, Any], path: str) -> str | None:
        """The artifact's content from one run, or None when that run does not carry it."""
        for candidate in self._artifact_candidates(run, path):
            response = requests.get(f"{self.base_url}{candidate}", timeout=30)
            if response.status_code < 400:
                return response.text
        return None

    def wait_for_artifact(self, experiment_name: str, path: str, timeout: int = 120, interval: int = 5) -> str:
        """The artifact's content from whichever run in the experiment carries it.

        An experiment holds more than the adapter's run: EvalHub records its own run for the
        evaluation card in the same experiment, and it starts *after* the adapter's, so the
        newest run is not the one with `atif/evaluation_results.json`. Reading artefacts by
        newest run therefore finds a run that legitimately has them not, which MLflow reports
        as a 500 for a missing file.
        """
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while True:
            experiment = self.find_experiment(experiment_name)
            if experiment is not None:
                for run in self.runs(experiment["experiment_id"]):
                    found = self.find_artifact(run, path)
                    if found is not None:
                        LOGGER.info("read %s from run %s in %r", path, run["info"]["run_id"], experiment_name)
                        return found
                    seen.append(run["info"]["run_id"])
            if time.monotonic() >= deadline:
                pytest.fail(
                    f"no run in {experiment_name!r} carries {path!r} within {timeout}s "
                    f"(runs seen: {sorted(set(seen)) or 'none'})",
                    pytrace=False,
                )
            time.sleep(interval)


@contextlib.contextmanager
def tracking_server(kube_context: str, namespace: str, workspace: str | None = None) -> Iterator[TrackingServer]:
    """A client for the deployed server, reachable through a forwarded port."""
    (local_port,) = allocate_local_ports(FORWARDED_PORT)
    try:
        with port_forward(kube_context, namespace, f"service/{NAME}", local_port, PORT) as url:
            yield TrackingServer(url, workspace=workspace)
    finally:
        release_local_ports(local_port)
