"""Checks that payload capture is written into a KServe InferenceService.

This is the other half of the install with no serving API: where the API exists, the workload
operator adopts it and configures the predictor's logger, and where it does not, nothing is
written and nothing fails. It runs *after* those checks, because it installs the CRD they
require to be absent:

- `tests/test_trustyai_serving_optional.py` (marker `isvc_free`) asserts the stack works
  with no serving API, and skips if one is installed;
- this module (marker `tas_payload`) installs the checkout's stand-in, writes a mock
  InferenceService - a predictor KServe's webhook admits, which nothing serves - and takes
  the CRD back off the cluster when it is done.

The operator registers no source for `serving.kserve.io` on purpose - a watch on a kind
nothing serves would keep it from starting - so it learns about an InferenceService on its
next pass, at most `defaultRequeueDelay` (30s) after the object appears. The fixture
therefore creates the object before the service, so the pass the service creation triggers
is the one that writes the logger.

What is written is `spec.predictor.logger = {mode: all, url:
https://<service>.<namespace>.svc.cluster.local}`: the https sink for a Raw/Standard
deployment, whose predictor sidecar presents the service certificate. Removing the service
takes the logger back, along with the annotations KServe writes beside it.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator

import pytest
from kubernetes import client
from kubernetes.dynamic import DynamicClient

from utilities.kube import keep_resources
from utilities.release import release
from utilities.serving import (
    LOGGER_ANNOTATIONS,
    MODULE_CONDITION,
    REASON_AVAILABLE,
    REASON_INFERENCE_SERVICES_FOUND,
    SERVICE_CONDITION,
    annotations_of,
    create_mock_inference_service,
    describe,
    install_serving_api,
    logger_of,
    logger_url,
    read_inference_service,
    remove_serving_api,
    wait_for_logger,
    wait_for_logger_removed,
)
from utilities.trustyai import (
    MODULE_CR_NAME,
    assert_module_ready,
    module_api_version,
    module_conditions,
    read_module,
)
from utilities.trustyai_service import API_GROUP, PLURAL, STORAGE_VERSION, ServiceAddress, condition, read_service

LOGGER = logging.getLogger(__name__)

# The first release whose workload operator adopts the serving API by reading the CRD, and
# whose module operator reports it as an observation (#1136, #1137).
MIN_RELEASE = "3.7"

# The object the checks write: a mock InferenceService, never a working predictor.
MODEL_NAME = "mock-model"

# The workload operator reconciles a TrustyAIService every 30s and this waits well past
# that, so a pass that has to notice something new always fits.
SERVING_TIMEOUT = 180

# Two reconciles, so a logger the operator keeps rewriting shows up as a new resourceVersion.
IDEMPOTENCE_WINDOW = 65

pytestmark = [
    pytest.mark.tas_payload,
    release(min=MIN_RELEASE),
    pytest.mark.usefixtures("modular_trustyai_architecture", "trustyai_module"),
]


@pytest.fixture(scope="module")
def serving_api(api_client: client.ApiClient) -> Iterator[bool]:
    """Install the InferenceService stand-in for this module, and take it back off after.

    It comes from `OPERATOR_REPO/tests/crds`, the same file `k8s-tests deploy` applies for
    a run whose operators watch the kind, so the schema exercised here is the one the
    operator's own tests check the write path against. A cluster that already serves the
    kind keeps its CRD: this module only removes one it installed itself.
    """
    operator_repo = os.environ.get("OPERATOR_REPO", "").strip()
    if not operator_repo:
        pytest.skip("OPERATOR_REPO is not set, so the InferenceService stand-in to install cannot be found")
    created = install_serving_api(api_client, operator_repo)
    try:
        yield created
    finally:
        if created and keep_resources():
            LOGGER.warning("KEEP_RESOURCES is set: leaving the InferenceService CRD this module installed")
        elif created:
            remove_serving_api(api_client)


@pytest.fixture(scope="module")
def captured(
    request: pytest.FixtureRequest,
    serving_api: bool,
    custom_api: client.CustomObjectsApi,
    trustyai_services: Callable[..., ServiceAddress],
    workload_namespaces: Callable[..., str],
) -> Iterator[dict[str, object]]:
    """A mock InferenceService and the service that adopts it, with the logger already written.

    The object goes first and carries the annotations KServe writes beside a logger, so the
    removal below has something to take back. The service is created into the same
    namespace, which is what makes the operator list it at all: it looks for
    InferenceServices in the instance's own namespace, on the pass the creation triggers.
    """
    namespace = workload_namespaces(request, prefix="trustyai-payload")
    create_mock_inference_service(
        custom_api, namespace, MODEL_NAME, annotations=dict.fromkeys(LOGGER_ANNOTATIONS, "true")
    )
    service = trustyai_services(request, namespace=namespace)
    resource = wait_for_logger(
        custom_api, namespace, MODEL_NAME, logger_url(service.name, namespace), timeout=SERVING_TIMEOUT
    )
    yield {"service": service, "namespace": namespace, "resource": resource}


def test_the_inference_service_gets_the_payload_capture_logger(captured: dict[str, object], custom_api) -> None:
    """The predictor's logger points at this service, in the mode that captures both halves."""
    service: ServiceAddress = captured["service"]  # type: ignore[assignment]
    namespace = str(captured["namespace"])
    resource = read_inference_service(custom_api, namespace, MODEL_NAME)
    logger = logger_of(resource)
    LOGGER.info("%s/%s logger: %s", namespace, MODEL_NAME, logger)

    assert logger is not None, (
        f"{MODEL_NAME} in {namespace} carries no logger; the operator has adopted the serving API "
        "and should have written spec.predictor.logger"
    )
    assert logger.get("mode") == "all", f"the logger does not capture both halves: {logger}"
    assert logger.get("url") == logger_url(service.name, namespace), (
        f"the logger points at {logger.get('url')!r}, expected the in-cluster https sink "
        f"{logger_url(service.name, namespace)!r}"
    )


def test_the_service_condition_reports_the_serving_api_present(
    captured: dict[str, object], custom_api: client.CustomObjectsApi
) -> None:
    """The same condition that says NotInstalled without the API says Found with it."""
    service: ServiceAddress = captured["service"]  # type: ignore[assignment]
    resource = read_service(custom_api, service.namespace, service.name)
    reported = condition(resource, SERVICE_CONDITION)
    LOGGER.info("%s/%s %s: %s", service.namespace, service.name, SERVICE_CONDITION, reported)

    assert reported is not None, (
        f"{service.namespace}/{service.name} reports no {SERVICE_CONDITION} condition; "
        "the operator adopts the API on the same pass that writes the logger"
    )
    assert reported.get("status") == "True", (
        f"{SERVICE_CONDITION} is {reported.get('status')} with the API installed and an "
        f"InferenceService to configure: {reported}"
    )
    assert reported.get("reason") == REASON_INFERENCE_SERVICES_FOUND, (
        f"{SERVICE_CONDITION} reason is {reported.get('reason')!r}, expected "
        f"{REASON_INFERENCE_SERVICES_FOUND!r}: {reported}"
    )


def test_the_module_notices_the_api_appearing(
    serving_api: bool, dynamic_client: DynamicClient, api_client: client.ApiClient
) -> None:
    """The module operator re-reads the API every pass, so installing it needs no restart."""
    LOGGER.info("this module installed the serving API: %s", serving_api)
    api_version = module_api_version(api_client)
    deadline = time.monotonic() + SERVING_TIMEOUT
    while True:
        module = read_module(dynamic_client, api_version)
        reported = module_conditions(module).get(MODULE_CONDITION)
        if reported is not None and reported.get("status") == "True":
            break
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{MODULE_CR_NAME} still reports {describe(reported)} {SERVING_TIMEOUT}s after the serving API "
                "was installed; the module operator re-derives it on every pass and needs no restart",
                pytrace=False,
            )
        LOGGER.info("waiting for %s to report the installed API: %s", MODULE_CR_NAME, describe(reported))
        time.sleep(10)

    assert reported.get("reason") == REASON_AVAILABLE, (
        f"{MODULE_CONDITION} reason is {reported.get('reason')!r}, expected {REASON_AVAILABLE!r}: {describe(reported)}"
    )
    assert_module_ready(dynamic_client, api_version)


def test_the_logger_is_not_rewritten(captured: dict[str, object], custom_api: client.CustomObjectsApi) -> None:
    """A reconcile that finds the desired logger writes nothing, so the object stops moving.

    The operator rewrote the field whenever a pass ran before the mode was compared as well
    as the sink; two reconciles is what tells the two behaviours apart.
    """
    namespace = str(captured["namespace"])
    before = read_inference_service(custom_api, namespace, MODEL_NAME)
    LOGGER.info("holding %s/%s for %ds", namespace, MODEL_NAME, IDEMPOTENCE_WINDOW)
    time.sleep(IDEMPOTENCE_WINDOW)
    after = read_inference_service(custom_api, namespace, MODEL_NAME)

    assert after["metadata"]["resourceVersion"] == before["metadata"]["resourceVersion"], (
        f"{MODEL_NAME} in {namespace} was written again by a pass that found the logger already correct: "
        f"resourceVersion {before['metadata']['resourceVersion']} then {after['metadata']['resourceVersion']}"
    )
    assert logger_of(after) == logger_of(before), f"the logger changed without a write: {logger_of(after)}"


def test_removing_the_service_takes_the_logger_back(captured: dict[str, object], custom_api) -> None:
    """The finalizer clears what it wrote and the annotations KServe wrote beside it.

    This is last in the file because it deletes the service the checks above read.
    """
    service: ServiceAddress = captured["service"]  # type: ignore[assignment]
    namespace = str(captured["namespace"])
    LOGGER.info("deleting %s/%s", service.namespace, service.name)
    custom_api.delete_namespaced_custom_object(
        group=API_GROUP,
        version=STORAGE_VERSION,
        namespace=service.namespace,
        plural=PLURAL,
        name=service.name,
    )

    resource = wait_for_logger_removed(custom_api, namespace, MODEL_NAME, timeout=SERVING_TIMEOUT)
    remaining = sorted(annotations_of(resource))
    LOGGER.info("%s/%s annotations after removal: %s", namespace, MODEL_NAME, remaining)
    assert logger_of(resource) is None, f"the logger is still there after removal: {logger_of(resource)}"
    assert not set(remaining) & set(LOGGER_ANNOTATIONS), (
        f"the annotations recording this operator's sink were left behind: {remaining}"
    )
