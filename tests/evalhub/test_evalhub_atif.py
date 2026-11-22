"""Checks for the ATIF adapter: generated trajectories, MLflow and the manifest.

The ATIF adapter scores *pre-recorded* trajectories, so this section generates them
(see `utilities.atif`), publishes them onto a claim, and hands them to a job as its
`test_data_ref`. Every job is a real one: EvalHub creates it, the adapter image runs
in the cluster, a stub judge scores each step, and the run is written to the MLflow
tracking server the section deploys.

What each check is for, and the change it pins:

- the manifest drives discovery, so `atif_manifest_entry_count` equals the rows and an
  unlisted file is not loaded; `atif_trajectory_count` counts what scored;
- the producer provenance in the declared `extra` object, and the manifest's task and
  rollout indices with their digest verdict, survive into the run's
  `atif/evaluation_results.json`;
- a digest the producer gets wrong fails the job naming the file under `fail_fast`, and
  withholds that entry's score under `skip_failed_trajectory`;
- a job that scored nothing publishes no `atif_overall_score` and reports
  `atif_complete: false`, while counts stay on every job;
- an empty manifest and a root field the ATIF model does not declare are invalid
  input, not a crash;
- ATIF-v1.6 still scores and ATIF-v1.9 is rejected naming the supported range.

The section is opt-in and slow: it deploys MLflow and a judge, and each job pulls the
ATIF adapter image and runs judge calls. `k8s-tests test --section adapters-atif` runs only
this. `ATIF_IMG` points EvalHub's provider at the image the run built, so the checks
exercise the checkout rather than the operator's pin.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from kubernetes import client

from utilities import atif, mlflow
from utilities.atif import BENCHMARK_ID, PRIMARY_METRIC, Export, export_files, job_payload, publish
from utilities.evalhub import (
    EVALHUB_CONTAINER_NAME,
    EVALHUB_NAME,
    EVALHUB_SERVICE_PORT,
    api_headers,
    benchmark_result,
    evalhub_api_version,
    patch_instance_spec,
    submit_job,
    wait_for_container_env,
    wait_for_job,
)
from utilities.mlflow import TrackingServer, tracking_server
from utilities.portforward import allocate_local_ports, port_forward, release_local_ports

LOGGER = logging.getLogger(__name__)

pytestmark = [pytest.mark.evalhub, pytest.mark.adapters, pytest.mark.atif]

# The adapter runs judge calls, writes to MLflow and, on a cold node, pulls its image.
JOB_TIMEOUT = 1800
ARTIFACT = "atif/evaluation_results.json"
# A local port base of its own: the session-wide EvalHub forward takes 18001.
ATIF_FORWARDED_PORT = 18010
PROVIDER_NAME = "atif"
PROVIDER_LABEL = "trustyai.opendatahub.io/evalhub-provider-name"
PROVIDER_CONFIG_KEY = "atif.yaml"


def _report(report: str) -> dict[str, Any]:
    """The parsed `atif/evaluation_results.json` the adapter persisted."""
    return json.loads(report)


def _metrics(result: dict[str, Any]) -> dict[str, float]:
    """The benchmark's metrics, as the API reports them: a name to value mapping."""
    return dict(result.get("metrics") or {})


def _summary_status(job: dict[str, Any]) -> tuple[str, str]:
    """The job's terminal state and the message the platform recorded with it."""
    status = job.get("status") or {}
    benchmark = (status.get("benchmarks") or [{}])[0]
    message = (status.get("message") or {}).get("message") or benchmark.get("error_message") or ""
    if isinstance(message, dict):
        message = message.get("message") or ""
    return status.get("state", ""), str(message)


@pytest.fixture(scope="module")
def batch_client(core_client: client.CoreV1Api) -> client.BatchV1Api:
    return client.BatchV1Api(core_client.api_client)


def _provider_image(core_client: client.CoreV1Api, namespace: str) -> tuple[str, str]:
    """The image the atif provider will run, and the ConfigMap that says so."""
    config_maps = core_client.list_namespaced_config_map(
        namespace=namespace, label_selector=f"{PROVIDER_LABEL}={PROVIDER_NAME}"
    ).items
    if not config_maps:
        pytest.fail(
            f"no ConfigMap labelled {PROVIDER_LABEL}={PROVIDER_NAME} in {namespace}; "
            f"the operator ships one per provider in its own namespace",
            pytrace=False,
        )
    config_map = config_maps[0]
    provider = (config_map.data or {}).get(PROVIDER_CONFIG_KEY, "")
    in_runtime = False
    for line in provider.splitlines():
        stripped = line.strip()
        if stripped == "runtime:":
            in_runtime = True
            continue
        if in_runtime and stripped.startswith("image:"):
            return stripped.split("image:", 1)[1].strip(), config_map.metadata.name
    pytest.fail(
        f"{config_map.metadata.name} has no runtime image; the provider config changed shape: {provider[:400]}",
        pytrace=False,
    )


def _wait_for_provider_config_map(
    core_client: client.CoreV1Api, namespace: str, timeout: int = 120, interval: int = 5
) -> None:
    """Wait until the operator has copied the provider's ConfigMap into the instance namespace.

    The operator copies a system provider it finds in its own namespace to
    `<instance>-provider-<name>` in the instance's, and that copy is what EvalHub mounts
    and reads at start-up. The copy carries no labels of its own, so it is found by name.
    """
    name = f"{EVALHUB_NAME}-provider-{PROVIDER_NAME}"
    deadline = time.monotonic() + timeout
    while True:
        try:
            core_client.read_namespaced_config_map(name=name, namespace=namespace)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
        else:
            LOGGER.info("%s/%s carries the %s provider", namespace, name, PROVIDER_NAME)
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"the operator did not copy the {PROVIDER_NAME} provider into {namespace} within {timeout}s; "
                f"the instance must list it in spec.providers",
                pytrace=False,
            )
        time.sleep(interval)


@pytest.fixture(scope="module")
def atif_provider_image(core_client: client.CoreV1Api, namespace: str) -> str | None:
    """Check that the provider will run the image this run built.

    The image cannot be changed on a running cluster: the provider ConfigMaps are rendered
    into the operator image from `config/overlays/*/params.env`, and the operator re-applies
    them within about 30 seconds, so a patch to the ConfigMap is reverted. Unlike inspect and
    lighteval, `evalhub-provider-atif-image` has no `RELATED_IMAGE_*` resolver key, so the
    override has to happen before the operator is built. The section fails here rather than
    silently exercising the operator's pin, whose behaviour is not what the checks assert.
    """
    image = os.environ.get("ATIF_IMG", "").strip()
    running, config_map = _provider_image(core_client, namespace)
    if not image:
        LOGGER.warning("ATIF_IMG is unset; the section runs the operator's pinned atif image %s", running)
        return None
    if running != image and os.environ.get("ATIF_ALLOW_PINNED_IMAGE", "").strip() != "1":
        pytest.fail(
            f"the atif provider runs {running} ({namespace}/{config_map}), not {image}. "
            "The provider image is rendered from the operator's config/overlays/*/params.env at build "
            "time (evalhub-provider-atif-image) and re-applied within seconds, so patching the ConfigMap "
            "does not hold: set that key in the operator checkout and rebuild with `k8s-tests up-registry` "
            "before running this section, or set ATIF_ALLOW_PINNED_IMAGE=1 to test the pin on purpose.",
            pytrace=False,
        )
    LOGGER.info("the atif provider runs %s (%s/%s)", running, namespace, config_map)
    return image


@pytest.fixture(scope="module")
def evalhub_mlflow(
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    dynamic_client: Any,
    api_client: client.ApiClient,
    atif_provider_image: str | None,
    evalhub_instance: dict[str, Any],
    evalhub_instance_namespace: str,
) -> str:
    """Deploy MLflow and tell the EvalHub instance where it is.

    The instance patch is what makes a job with an `experiment` resolve an MLflow
    experiment, which is where the adapter's `experiment_name` comes from; without it
    the API refuses the submission rather than running a job with no artifacts.
    """
    uri = mlflow.deploy_mlflow(core_client, apps_client, evalhub_instance_namespace)
    ca_bundle = mlflow.seed_ca_bundle(core_client, evalhub_instance_namespace)
    api_version = evalhub_api_version(api_client)
    providers = list((evalhub_instance.get("spec") or {}).get("providers") or [])
    if PROVIDER_NAME not in providers:
        providers.append(PROVIDER_NAME)
    # One patch, one reconcile, one rollout. EvalHub parses MLFLOW_CA_CERT_PATH as soon as
    # it has a tracking URI, and on Kind the operator's bundle is empty, so the instance
    # needs a user CA bundle or the replacement Pod exits on start-up.
    patch_instance_spec(
        dynamic_client,
        api_version,
        EVALHUB_NAME,
        evalhub_instance_namespace,
        {
            "providers": providers,
            "env": [{"name": "MLFLOW_TRACKING_URI", "value": uri}],
            "mlflow": {"tls": {"caBundle": ca_bundle}},
        },
    )
    wait_for_container_env(
        apps_client,
        EVALHUB_NAME,
        evalhub_instance_namespace,
        EVALHUB_CONTAINER_NAME,
        "MLFLOW_TRACKING_URI",
        uri,
    )
    # EvalHub reads its provider set at start-up: wait for the copy the operator makes,
    # then for the rollout that mounts it. `wait_for_container_env` requires the finished
    # rollout, not just an available Pod, so a crash-looping replacement cannot hide
    # behind the old one still serving.
    _wait_for_provider_config_map(core_client, evalhub_instance_namespace)
    LOGGER.info("EvalHub records runs through %s", uri)
    return uri


@pytest.fixture(scope="module")
def evalhub_api_base(
    kube_context: str,
    evalhub_instance_namespace: str,
    evalhub_mlflow: str,
) -> Iterator[str]:
    """The instance's API, forwarded once the section's own rollout has finished.

    The session-wide fixture forwards as soon as the instance is Ready, which is before this
    section patches the instance with the provider, the tracking URI and the CA bundle: the
    rollout that follows replaces the Pod the forward is attached to, and from then on every
    request through it fails with SSLEOFError. Opening the forward after the rollout keeps it
    attached to the Pod that serves the jobs. The port is allocated rather than fixed, so a
    run that also uses the session-wide forward cannot collide with it.
    """
    (local_port,) = allocate_local_ports(ATIF_FORWARDED_PORT)
    try:
        with port_forward(
            context=kube_context,
            namespace=evalhub_instance_namespace,
            target=f"service/{EVALHUB_NAME}",
            local_port=local_port,
            remote_port=EVALHUB_SERVICE_PORT,
            scheme="https",
        ) as base_url:
            yield base_url
    finally:
        release_local_ports(local_port)


@pytest.fixture(scope="module")
def tenant_ready(evalhub_tenant_ca: None) -> None:
    """Provision the tenant for a job: the operator's RBAC, the job-access bindings, the CA.

    `evalhub_tenant_ca` is otherwise only pulled in by `evalhub_model_credentials`, and this
    section's judge needs no model secret. Without it the instance's ServiceAccount cannot
    create the job's ConfigMap in the tenant namespace ("configmaps is forbidden") and the
    sidecar has no `service-ca.crt` to report results back over TLS.
    """
    return None


@pytest.fixture(scope="module")
def judge(
    core_client: client.CoreV1Api, apps_client: client.AppsV1Api, evalhub_instance_namespace: str, evalhub_mlflow: str
) -> str:
    """The stub judge's OpenAI-compatible base URL."""
    return atif.deploy_judge(core_client, apps_client, evalhub_instance_namespace)


@pytest.fixture(scope="module")
def tracking(
    kube_context: str,
    evalhub_instance_namespace: str,
    evalhub_tenant_namespace: str,
    evalhub_mlflow: str,
) -> Any:
    """An MLflow client on a forwarded port, open for the module.

    The workspace is the tenant namespace: EvalHub gives a job pod its tenant as
    MLFLOW_WORKSPACE and the adapter's SDK prefixes artifact paths with it.
    """
    with tracking_server(kube_context, evalhub_instance_namespace, workspace=evalhub_tenant_namespace) as server:
        yield server


def _publish(core_client, batch_client, namespace: str, name: str, files: dict[str, bytes]) -> Export:
    return publish(core_client, batch_client, namespace, name, files)


def _submit(
    *,
    base_url: str,
    token: str,
    namespace: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Submit one job and wait for it to reach a terminal state."""
    headers = api_headers(token, namespace)
    for attempt in range(3):
        try:
            job = submit_job(base_url, headers, payload)
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError):
            if attempt == 2:
                raise
            LOGGER.warning("the API connection dropped on submit; retrying in 5s")
            time.sleep(5)
    return wait_for_job(base_url, headers, job["resource"]["id"], timeout=JOB_TIMEOUT)


def _run(
    *,
    name: str,
    export: Export,
    judge_url: str,
    experiment: str,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    parameters: dict[str, Any] | None = None,
    model_url: str | None = None,
) -> dict[str, Any]:
    """Submit one job, wait for it, and return what the API reported."""
    payload = job_payload(
        name=name,
        export=export,
        judge_base_url=model_url or judge_url,
        experiment=experiment,
        parameters=parameters,
    )
    job = _submit(
        base_url=evalhub_api_base,
        token=evalhub_tenant_token,
        namespace=evalhub_tenant_namespace,
        payload=payload,
    )
    status, message = _summary_status(job)
    LOGGER.info("job %s finished %s: %s", name, status, message)
    return {"job": job, "payload": payload, "status": status, "message": message}


@pytest.fixture(scope="module")
def manifest_run(
    tenant_ready: None,
    core_client: client.CoreV1Api,
    batch_client: client.BatchV1Api,
    evalhub_instance_namespace: str,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    judge: str,
    evalhub_mlflow: str,
) -> dict[str, Any]:
    """One export the manifest drives: two listed rows and one file it does not list."""
    export = _publish(
        core_client,
        batch_client,
        evalhub_tenant_namespace,
        "atif-manifest-export",
        export_files(entries=2, unlisted=True),
    )
    return _run(
        name="atif-manifest",
        export=export,
        judge_url=judge,
        experiment="atif-manifest",
        evalhub_api_base=evalhub_api_base,
        evalhub_tenant_token=evalhub_tenant_token,
        evalhub_tenant_namespace=evalhub_tenant_namespace,
        parameters={"partial_result_policy": "fail_fast"},
    ) | {"export": export}


def test_the_manifest_job_completes(manifest_run: dict[str, Any]) -> None:
    """The adapter ran the export the manifest listed and the job completed."""
    assert manifest_run["status"] == "completed", f"job ended {manifest_run['status']}: {manifest_run['message']}"


def test_the_manifest_drives_discovery(manifest_run: dict[str, Any]) -> None:
    """AC1/AC2: the entry count is the manifest's, and the unlisted file is not loaded."""
    export: Export = manifest_run["export"]
    result = benchmark_result(manifest_run["job"], BENCHMARK_ID)
    metrics = _metrics(result)
    assert metrics.get("atif_manifest_entry_count") == float(export.entries), (
        f"the result reports {metrics}, not {export.entries} manifest entries"
    )
    assert metrics.get("atif_trajectory_count") == float(export.entries), (
        f"the result reports {metrics}, not {export.entries} scored trajectories"
    )


def test_the_run_records_manifest_provenance(
    manifest_run: dict[str, Any],
    tracking: TrackingServer,
) -> None:
    """AC3: each scored trajectory carries its own manifest indices, digest verdict and producer extra."""
    assert manifest_run["status"] == "completed", manifest_run["message"]
    export: Export = manifest_run["export"]
    metadata = _report(tracking.wait_for_artifact("atif-manifest", ARTIFACT))["evaluation_metadata"]
    scored = {item["trajectory_id"]: item for item in metadata["atif_trajectories"]}
    assert set(scored) == {f"t{index}" for index in range(export.entries)}, (
        f"the report scored {sorted(scored)}, not the manifest's entries"
    )
    for index in range(export.entries):
        entry = scored[f"t{index}"]
        provenance = entry["provenance"]
        manifest = provenance["nemo_gym_manifest"]
        assert manifest["task_index"] == 0, f"t{index} carries task {manifest['task_index']}"
        assert manifest["rollout_index"] == index, f"t{index} carries rollout {manifest['rollout_index']}"
        assert manifest["digest_verified"] is True, f"t{index}'s digest was not verified: {manifest}"
        assert entry["source_path"].endswith(f"0-{index}.json"), (
            f"t{index} came from {entry['source_path']}, not the file the manifest lists for it"
        )
        assert provenance["producer_extra"]["producer_sha256"] == f"sha256-of-t{index}", (
            f"t{index} carries another trajectory's producer extra: {provenance['producer_extra']}"
        )


@pytest.fixture(scope="module")
def digest_mismatch_run(
    tenant_ready: None,
    core_client: client.CoreV1Api,
    batch_client: client.BatchV1Api,
    evalhub_instance_namespace: str,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    judge: str,
    evalhub_mlflow: str,
) -> dict[str, Any]:
    """One export whose manifest declares a digest the bytes do not have."""
    export = _publish(
        core_client,
        batch_client,
        evalhub_tenant_namespace,
        "atif-digest-mismatch",
        export_files(entries=1, digest_mismatch=True, unlisted=False),
    )
    return _run(
        name="atif-digest-mismatch",
        export=export,
        judge_url=judge,
        experiment="atif-digest-mismatch",
        evalhub_api_base=evalhub_api_base,
        evalhub_tenant_token=evalhub_tenant_token,
        evalhub_tenant_namespace=evalhub_tenant_namespace,
        parameters={"partial_result_policy": "fail_fast"},
    ) | {"export": export}


def test_a_digest_mismatch_fails_the_job_naming_the_file(digest_mismatch_run: dict[str, Any]) -> None:
    """AC3 under fail_fast: the job fails and the error names the file the producer got wrong."""
    assert digest_mismatch_run["status"] == "failed", (
        f"a digest mismatch ended {digest_mismatch_run['status']}: {digest_mismatch_run['message']}"
    )
    assert "SHA-256 mismatch" in digest_mismatch_run["message"], (
        f"the failure does not name the digest mismatch: {digest_mismatch_run['message']}"
    )
    assert "0-0.json" in digest_mismatch_run["message"], (
        f"the failure does not name the offending file: {digest_mismatch_run['message']}"
    )


@pytest.fixture(scope="module")
def skip_failed_run(
    tenant_ready: None,
    core_client: client.CoreV1Api,
    batch_client: client.BatchV1Api,
    evalhub_instance_namespace: str,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    judge: str,
    evalhub_mlflow: str,
) -> dict[str, Any]:
    """The same mismatch, with the policy that withholds the score instead of failing."""
    export = _publish(
        core_client,
        batch_client,
        evalhub_tenant_namespace,
        "atif-digest-skip",
        export_files(entries=1, digest_mismatch=True, unlisted=False),
    )
    return _run(
        name="atif-digest-skip",
        export=export,
        judge_url=judge,
        experiment="atif-digest-skip",
        evalhub_api_base=evalhub_api_base,
        evalhub_tenant_token=evalhub_tenant_token,
        evalhub_tenant_namespace=evalhub_tenant_namespace,
        parameters={"partial_result_policy": "skip_failed_trajectory"},
    ) | {"export": export}


def test_skipping_a_failed_digest_withholds_only_its_score(skip_failed_run: dict[str, Any]) -> None:
    """AC3 under skip_failed_trajectory: completed, counted as failed, and no score invented."""
    assert skip_failed_run["status"] == "completed", (
        f"job ended {skip_failed_run['status']}: {skip_failed_run['message']}"
    )
    result = benchmark_result(skip_failed_run["job"], BENCHMARK_ID)
    metrics = _metrics(result)
    assert PRIMARY_METRIC not in metrics, f"an unverifiable entry produced {PRIMARY_METRIC}: {metrics}"
    assert metrics.get("atif_trajectory_count") == 0.0, f"the result scored something: {metrics}"
    report = result.get("trajectory_results") or []
    assert report == [] or all(item.get("status") != "scored" for item in report), (
        f"a mismatched digest was scored: {report}"
    )


@pytest.fixture(scope="module")
def mixed_digest_run(
    tenant_ready: None,
    core_client: client.CoreV1Api,
    batch_client: client.BatchV1Api,
    evalhub_instance_namespace: str,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    judge: str,
    evalhub_mlflow: str,
) -> dict[str, Any]:
    """One verifiable entry and one whose declared digest is wrong."""
    export = _publish(
        core_client,
        batch_client,
        evalhub_tenant_namespace,
        "atif-digest-mixed",
        export_files(entries=2, digest_mismatch=1, unlisted=False),
    )
    return _run(
        name="atif-digest-mixed",
        export=export,
        judge_url=judge,
        experiment="atif-digest-mixed",
        evalhub_api_base=evalhub_api_base,
        evalhub_tenant_token=evalhub_tenant_token,
        evalhub_tenant_namespace=evalhub_tenant_namespace,
        parameters={"partial_result_policy": "skip_failed_trajectory"},
    ) | {"export": export}


def test_a_verified_entry_still_scores_beside_a_wrong_digest(
    mixed_digest_run: dict[str, Any],
    tracking: TrackingServer,
) -> None:
    """AC3 under skip_failed_trajectory: the withheld entry does not take the export with it."""
    assert mixed_digest_run["status"] == "completed", (
        f"job ended {mixed_digest_run['status']}: {mixed_digest_run['message']}"
    )
    result = benchmark_result(mixed_digest_run["job"], BENCHMARK_ID)
    metrics = _metrics(result)
    assert metrics.get("atif_trajectory_count") == 1.0, f"the verified entry did not score on its own: {metrics}"
    assert PRIMARY_METRIC in metrics, f"the verified entry scored without {PRIMARY_METRIC}: {metrics}"
    metadata = _report(tracking.wait_for_artifact("atif-digest-mixed", ARTIFACT))["evaluation_metadata"]
    scored = {item["trajectory_id"] for item in metadata["atif_trajectories"]}
    assert scored == {"t0"}, f"the entry with the wrong digest was scored too: {scored}"
    assert metadata["atif_failed_trajectory_count"] == 1, (
        f"the withheld entry was not counted: {metadata['atif_failed_trajectory_count']}"
    )


@pytest.fixture(scope="module")
def unscored_run(
    tenant_ready: None,
    core_client: client.CoreV1Api,
    batch_client: client.BatchV1Api,
    evalhub_instance_namespace: str,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    judge: str,
    evalhub_mlflow: str,
) -> dict[str, Any]:
    """An export that cannot be scored: every judge call fails against a closed port."""
    export = _publish(
        core_client,
        batch_client,
        evalhub_tenant_namespace,
        "atif-unscored",
        export_files(entries=1, unlisted=False),
    )
    dead_judge = judge.replace(f":{atif.JUDGE_PORT}/", ":9/")
    return _run(
        name="atif-unscored",
        export=export,
        judge_url=judge,
        model_url=dead_judge,
        experiment="atif-unscored",
        evalhub_api_base=evalhub_api_base,
        evalhub_tenant_token=evalhub_tenant_token,
        evalhub_tenant_namespace=evalhub_tenant_namespace,
        parameters={"partial_result_policy": "skip_failed_trajectory"},
    ) | {"export": export}


def test_an_unscored_job_publishes_no_overall_score(unscored_run: dict[str, Any]) -> None:
    """AC4: nothing scored completes without an overall score and reports what it counted."""
    assert unscored_run["status"] == "completed", (
        f"an unscored job ended {unscored_run['status']}: {unscored_run['message']}"
    )
    result = benchmark_result(unscored_run["job"], BENCHMARK_ID)
    metrics = _metrics(result)
    assert PRIMARY_METRIC not in metrics, f"an unscored job published {PRIMARY_METRIC}: {metrics}"
    assert result.get("overall_score") is None or result.get("overall_score") == 0.0, (
        f"an unscored job published overall_score {result.get('overall_score')!r}"
    )
    assert metrics.get("atif_trajectory_count") == 0.0, f"the result counted a scored trajectory: {metrics}"
    test = result.get("test")
    assert not test or not test.get("pass"), f"an unscored job passed its threshold: {test}"


@pytest.fixture(scope="module")
def empty_manifest_run(
    tenant_ready: None,
    core_client: client.CoreV1Api,
    batch_client: client.BatchV1Api,
    evalhub_instance_namespace: str,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    judge: str,
    evalhub_mlflow: str,
) -> dict[str, Any]:
    """An empty manifest: invalid input under the default skip_invalid policy."""
    export = _publish(
        core_client,
        batch_client,
        evalhub_tenant_namespace,
        "atif-empty-manifest",
        export_files(entries=1, empty_manifest=True, unlisted=False),
    )
    return _run(
        name="atif-empty-manifest",
        export=export,
        judge_url=judge,
        experiment="atif-empty-manifest",
        evalhub_api_base=evalhub_api_base,
        evalhub_tenant_token=evalhub_tenant_token,
        evalhub_tenant_namespace=evalhub_tenant_namespace,
        parameters={"invalid_input_policy": "skip_invalid", "partial_result_policy": "fail_fast"},
    ) | {"export": export}


def test_an_empty_manifest_is_invalid_input(empty_manifest_run: dict[str, Any], tracking: TrackingServer) -> None:
    """AC5: the job completes with the invalid input counted, not as a crash and not as a score."""
    assert empty_manifest_run["status"] == "completed", (
        f"job ended {empty_manifest_run['status']}: {empty_manifest_run['message']}"
    )
    result = benchmark_result(empty_manifest_run["job"], BENCHMARK_ID)
    metrics = _metrics(result)
    assert PRIMARY_METRIC not in metrics, f"an empty manifest published {PRIMARY_METRIC}: {metrics}"
    assert metrics.get("atif_manifest_entry_count") in (None, 0.0), f"an empty manifest reported entries: {metrics}"
    report = tracking.wait_for_artifact("atif-empty-manifest", ARTIFACT)
    assert '"invalid_manifest"' in report or '"invalid_input_count": 1' in report, (
        f"the report does not carry the invalid manifest: {report[:1000]}"
    )


@pytest.fixture(scope="module")
def version_runs(
    tenant_ready: None,
    core_client: client.CoreV1Api,
    batch_client: client.BatchV1Api,
    evalhub_instance_namespace: str,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    judge: str,
    evalhub_mlflow: str,
) -> dict[str, dict[str, Any]]:
    """One ATIF-v1.6 export and one ATIF-v1.9 export."""
    runs: dict[str, dict[str, Any]] = {}
    for label, version in (("v16", atif.V16), ("v19", atif.V19)):
        export = _publish(
            core_client,
            batch_client,
            evalhub_tenant_namespace,
            f"atif-{label}",
            export_files(entries=1, schema_version=version, unlisted=False),
        )
        # v1.9 is the out-of-range case: under fail_fast the job must fail naming the
        # supported range, rather than skipping the file as an invalid input.
        policy = "fail_fast" if label == "v19" else "skip_invalid"
        runs[label] = _run(
            name=f"atif-{label}",
            export=export,
            judge_url=judge,
            experiment=f"atif-{label}",
            evalhub_api_base=evalhub_api_base,
            evalhub_tenant_token=evalhub_tenant_token,
            evalhub_tenant_namespace=evalhub_tenant_namespace,
            parameters={
                "partial_result_policy": "fail_fast",
                "invalid_input_policy": policy,
            },
        )
    return runs


def test_atif_v1_6_still_scores(version_runs: dict[str, dict[str, Any]]) -> None:
    """AC7: the version the Harbor-timeout trajectory uses keeps scoring after the range moved."""
    run = version_runs["v16"]
    assert run["status"] == "completed", f"an ATIF-v1.6 export ended {run['status']}: {run['message']}"
    metrics = _metrics(benchmark_result(run["job"], BENCHMARK_ID))
    assert metrics.get("atif_trajectory_count") == 1.0, f"the v1.6 export did not score: {metrics}"
    assert PRIMARY_METRIC in metrics, f"the v1.6 export scored without {PRIMARY_METRIC}: {metrics}"


def test_atif_v1_9_is_rejected_naming_the_supported_range(version_runs: dict[str, dict[str, Any]]) -> None:
    """AC7: a version outside the range fails, and the error names the range rather than a bare code."""
    run = version_runs["v19"]
    assert run["status"] == "failed", f"an ATIF-v1.9 export ended {run['status']}: {run['message']}"
    assert "ATIF-v1.0 through ATIF-v1.8" in run["message"], (
        f"the failure does not name the supported range: {run['message']}"
    )


@pytest.fixture(scope="module")
def undeclared_field_run(
    tenant_ready: None,
    core_client: client.CoreV1Api,
    batch_client: client.BatchV1Api,
    evalhub_instance_namespace: str,
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    judge: str,
    evalhub_mlflow: str,
) -> dict[str, Any]:
    """An export whose root carries a field the ATIF model does not declare."""
    export = _publish(
        core_client,
        batch_client,
        evalhub_tenant_namespace,
        "atif-undeclared",
        export_files(entries=1, undeclared_field="producer_sha256", unlisted=False),
    )
    return _run(
        name="atif-undeclared",
        export=export,
        judge_url=judge,
        experiment="atif-undeclared",
        evalhub_api_base=evalhub_api_base,
        evalhub_tenant_token=evalhub_tenant_token,
        evalhub_tenant_namespace=evalhub_tenant_namespace,
        parameters={"invalid_input_policy": "skip_invalid", "partial_result_policy": "fail_fast"},
    ) | {"export": export}


def test_an_undeclared_root_field_is_invalid_input(
    undeclared_field_run: dict[str, Any], tracking: TrackingServer
) -> None:
    """AC3's policy: producer metadata belongs in `extra`, so a declared-nowhere root field is refused."""
    assert undeclared_field_run["status"] == "completed", (
        f"job ended {undeclared_field_run['status']}: {undeclared_field_run['message']}"
    )
    result = benchmark_result(undeclared_field_run["job"], BENCHMARK_ID)
    metrics = _metrics(result)
    assert PRIMARY_METRIC not in metrics, f"an undeclared root field was scored: {metrics}"
    report = tracking.wait_for_artifact("atif-undeclared", ARTIFACT)
    assert '"invalid_schema"' in report, f"the report does not record the schema rejection: {report[:1000]}"
