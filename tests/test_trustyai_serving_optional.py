"""Checks for a cluster the serving API was never installed on.

KServe InferenceServices are an integration, not a dependency: the stack registers models
on a model server and captures their payloads when serving is there, and runs without it
when it is not. Up to 3.6 the module operator reported the absence through a precondition,
as `KServeAvailable=False` with reason `PreConditionFailed`, which reads as an unmet
requirement; from 3.7 it states the same fact as an observation
(trustyai-service-operator#1137) and the workload operator keeps `Ready` where it was
(#1136). Neither operator watches the kind any more, which is why the harness stopped
installing the CRD for a release from 3.7 on: an informer for a kind nothing serves never
syncs, so with the watch gone there is nothing left that needs it.

These checks describe that install, and every one of them needs it: the premise is a
cluster with no `inferenceservices.serving.kserve.io`, and the fixture skips the module
when the CRD is there rather than asserting against the wrong cluster. `--section isvc-free`
runs them alone, and `SERVING_CRD=never` (or a `--release 3.7` run) is what keeps the CRD
off a cluster `k8s-tests up` built.

The last check is the one that costs time: it restarts both operators, because starting
with an empty informer set is the failure a watch on an absent kind produces, and it only
appears when the process starts.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient

from utilities.kube import assert_deployment_healthy, restart_deployment
from utilities.release import release
from utilities.serving import (
    MODULE_CONDITION,
    REASON_NOT_INSTALLED,
    REASON_PRECONDITION_FAILED,
    REASON_SERVING_CRD_NOT_FOUND,
    SERVICE_CONDITION,
    SERVING_CRD_NAME,
    WATCH,
    describe,
    describe_grants,
    grants,
    require_serving_api_absent,
    rules_for_deployment,
)
from utilities.trustyai import (
    MODULE_CR_NAME,
    MODULE_KIND,
    MODULE_OPERATOR_DEPLOYMENT,
    WORKLOAD_OPERATOR_DEPLOYMENT,
    assert_module_observed_generation,
    assert_module_provisioning_succeeded,
    assert_module_ready,
    module_api_version,
    module_condition,
    module_conditions,
    read_module,
    wait_for_module_condition,
)
from utilities.trustyai_service import (
    API_GROUP,
    PLURAL,
    SERVICE_NAME,
    STORAGE_VERSION,
    ServiceAddress,
    condition,
    describe_conditions,
    read_service,
)

LOGGER = logging.getLogger(__name__)

# The first release whose module operator reports an absent serving API as NotInstalled,
# and whose workload operator reports the missing CRD without touching Ready.
MIN_RELEASE = "3.7"

# How long the finalizer the operator adds may take to appear, and to finish once the
# resource is deleted. Both are redirects of the same reconcile, not operand rollouts.
FINALIZER_TIMEOUT = 180
FINALIZER_INTERVAL = 3

# The two conditions every check here reads. `trustyai_module` waits for the workload
# operator, not for the module operator to have written its status, so the first read can
# be a reconcile early; the module operator requeues every 60s, so this is well past one.
CONDITIONS_TIMEOUT = 180
CONDITIONS_INTERVAL = 5

# What the module operator reports the required and the optional dependency under.
DEPENDENCIES_CONDITION = "DependenciesAvailable"

pytestmark = [
    pytest.mark.isvc_free,
    release(min=MIN_RELEASE),
    pytest.mark.usefixtures("modular_trustyai_architecture", "trustyai_module"),
]


@pytest.fixture(scope="module", autouse=True)
def serving_absent(api_client: client.ApiClient) -> None:
    """The premise of every check here: this cluster has no serving API.

    It is a fixture rather than a check so the skip names the reason and the fix, and so
    no check in the module runs against a cluster that has KServe installed.
    """
    require_serving_api_absent(api_client)


@pytest.fixture(scope="module")
def module(dynamic_client: DynamicClient, api_client: client.ApiClient) -> dict:
    """The module resource, read once both conditions this module reads are on it.

    It is read once because it is the same object every check here reports on, and it is
    read *after* the conditions appear because nothing before this point waits for the
    module operator to have written its status: `trustyai_module` waits for the workload
    operator it deploys. A snapshot taken a reconcile early has no conditions to assert on.
    """
    api_version = module_api_version(api_client)
    deadline = time.monotonic() + CONDITIONS_TIMEOUT
    while True:
        observed = read_module(dynamic_client, api_version)
        conditions = module_conditions(observed)
        if MODULE_CONDITION in conditions and DEPENDENCIES_CONDITION in conditions:
            LOGGER.info("%s/%s reports %s", MODULE_KIND, MODULE_CR_NAME, sorted(conditions))
            return observed
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{MODULE_KIND}/{MODULE_CR_NAME} did not report {MODULE_CONDITION} and {DEPENDENCIES_CONDITION} "
                f"within {CONDITIONS_TIMEOUT}s; conditions: {', '.join(sorted(conditions)) or 'none'}",
                pytrace=False,
            )
        LOGGER.info(
            "waiting for %s/%s to report its conditions; it has %s",
            MODULE_KIND,
            MODULE_CR_NAME,
            ", ".join(sorted(conditions)) or "none",
        )
        time.sleep(CONDITIONS_INTERVAL)


@pytest.fixture(scope="module")
def service(
    request: pytest.FixtureRequest,
    trustyai_services: Callable[..., ServiceAddress],
) -> Iterator[ServiceAddress]:
    """A `TrustyAIService` workload, created the way any check creates one, and removed after.

    It is the only way to see the workload operator's half of the removal: the condition
    it reports about payload capture only exists on a service it reconciles.
    """
    yield trustyai_services(request)


def test_module_reports_serving_as_not_installed(module: dict) -> None:
    """An absent API is stated as a fact, with the reason and severity that say so."""
    reported = module_conditions(module).get(MODULE_CONDITION)
    LOGGER.info("%s/%s %s: %s", MODULE_KIND, MODULE_CR_NAME, MODULE_CONDITION, describe(reported))

    assert reported is not None, (
        f"{MODULE_KIND}/{MODULE_CR_NAME} reports no {MODULE_CONDITION} condition; "
        f"conditions: {', '.join(sorted(module_conditions(module)))}"
    )
    assert reported.get("status") == "False", (
        f"{MODULE_CONDITION} is {reported.get('status')}, expected False on a cluster with no "
        f"serving API: {describe(reported)}"
    )
    assert reported.get("reason") == REASON_NOT_INSTALLED, (
        f"{MODULE_CONDITION} reason is {reported.get('reason')!r}, expected {REASON_NOT_INSTALLED!r}: "
        f"{describe(reported)}"
    )
    assert reported.get("severity") == "Info", (
        f"{MODULE_CONDITION} severity is {reported.get('severity')!r}, expected Info: an API the stack "
        f"works without is not a fault: {describe(reported)}"
    )
    message = reported.get("message") or ""
    assert "InferenceService.serving.kserve.io" in message, (
        f"{MODULE_CONDITION} message does not name the API it is about: {message!r}"
    )
    assert_module_observed_generation(module)


def test_module_does_not_report_a_failed_precondition_for_serving(module: dict) -> None:
    """The wording #1137 removed stays removed: no condition calls this an unmet requirement."""
    reasons = {condition_type: reported.get("reason") for condition_type, reported in module_conditions(module).items()}
    LOGGER.info("%s/%s condition reasons: %s", MODULE_KIND, MODULE_CR_NAME, reasons)

    failed = sorted(name for name, reason in reasons.items() if reason == REASON_PRECONDITION_FAILED)
    assert not failed, (
        f"{MODULE_KIND}/{MODULE_CR_NAME} reports {', '.join(failed)} as {REASON_PRECONDITION_FAILED}; "
        f"an optional API is not a failed precondition. conditions: "
        f"{', '.join(f'{name}={reason}' for name, reason in sorted(reasons.items()))}"
    )
    assert reasons.get(MODULE_CONDITION) == REASON_NOT_INSTALLED, (
        f"{MODULE_CONDITION} reason is {reasons.get(MODULE_CONDITION)!r}, expected {REASON_NOT_INSTALLED!r}"
    )


def test_module_is_ready_without_serving(dynamic_client: DynamicClient, api_client: client.ApiClient) -> None:
    """Nothing about the optional API gates readiness, so the module is ready all the same.

    `Ready` aggregates the health of every enabled service's operand instances, so this
    check has to run before the module creates one: a `TrustyAIService` on Kind is never
    ready - it wants a Route an OpenShift router would admit - and the module would then be
    `Not Ready` for a reason that has nothing to do with serving. That is the order the
    checks in this file keep, and the order the report runs them in.
    """
    module = assert_module_ready(dynamic_client, module_api_version(api_client))
    assert_module_provisioning_succeeded(module)

    degraded = module_condition(module, "Degraded")
    LOGGER.info(
        "%s/%s Degraded=%s; conditions: %s", MODULE_KIND, MODULE_CR_NAME, degraded, sorted(module_conditions(module))
    )
    assert degraded == "False", (
        f"{MODULE_KIND}/{MODULE_CR_NAME} reports Degraded={degraded} on a cluster with no serving API; "
        f"conditions: {', '.join(sorted(module_conditions(module)))}"
    )


def test_the_required_dependency_is_reported_separately(module: dict) -> None:
    """Prometheus still decides readiness, and the optional API is not folded into it."""
    conditions = module_conditions(module)
    LOGGER.info("%s/%s conditions: %s", MODULE_KIND, MODULE_CR_NAME, sorted(conditions))

    assert DEPENDENCIES_CONDITION in conditions, (
        f"{MODULE_KIND}/{MODULE_CR_NAME} reports no {DEPENDENCIES_CONDITION} condition; "
        f"conditions: {', '.join(sorted(conditions))}"
    )
    assert conditions[DEPENDENCIES_CONDITION].get("status") == "True", (
        f"the required dependency is not satisfied with the harness' Prometheus stand-in: "
        f"{describe(conditions[DEPENDENCIES_CONDITION])}"
    )
    assert conditions[MODULE_CONDITION].get("status") == "False", (
        f"the optional {MODULE_CONDITION} is reported as the required dependency: "
        f"{describe(conditions[MODULE_CONDITION])}"
    )


def test_module_operator_grants_no_watch_on_serving(
    apps_client: client.AppsV1Api,
    rbac_client: client.RbacAuthorizationV1Api,
    namespace: str,
) -> None:
    """No `watch` on the kind is granted, because a cache informer for it could not start.

    The verb is the one that would make an absent CRD fatal: controller-runtime lists and
    then watches, and a watch on a kind nothing serves never syncs, so the operator never
    becomes ready. #1136 removed the grant with the watch; this is the declarative half of
    that removal, and it fails if either comes back.
    """
    rules = rules_for_deployment(apps_client, rbac_client, MODULE_OPERATOR_DEPLOYMENT, namespace)
    if not rules:
        pytest.skip(f"no RBAC rule is bound to the ServiceAccount of {namespace}/{MODULE_OPERATOR_DEPLOYMENT}")
    granted = grants(rules)
    LOGGER.info("%s/%s serving grants: %s", namespace, MODULE_OPERATOR_DEPLOYMENT, describe_grants(granted))

    verbs = granted.get("inferenceservices", set())
    assert WATCH not in verbs, (
        f"{namespace}/{MODULE_OPERATOR_DEPLOYMENT} is granted {WATCH} on inferenceservices, so its cache "
        f"asks the API server for a kind this cluster does not serve and the operator cannot become ready; "
        f"serving grants: {describe_grants(granted)}"
    )


def test_both_operators_start_without_serving(
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    namespace: str,
    dynamic_client: DynamicClient,
    api_client: client.ApiClient,
) -> None:
    """Both processes start with an empty informer set, which is where a watch would fail.

    A running operator proves little on its own: the watch that breaks without the CRD is
    registered at startup, so the pods are replaced and the module is read again once the
    replacements are serving. The module operator goes first because it is what deploys the
    workload operator underneath it.
    """
    for deployment in (MODULE_OPERATOR_DEPLOYMENT, WORKLOAD_OPERATOR_DEPLOYMENT):
        restart_deployment(apps_client, deployment, namespace, core_client)
        assert_deployment_healthy(apps_client, core_client, name=deployment, namespace=namespace)

    module = wait_for_module_condition(
        dynamic_client=dynamic_client,
        api_version=module_api_version(api_client),
        condition_type=MODULE_CONDITION,
        status="False",
    )
    reported = module_conditions(module).get(MODULE_CONDITION)
    assert reported is not None and reported.get("reason") == REASON_NOT_INSTALLED, (
        f"after restarting both operators {MODULE_CONDITION} is {describe(reported)}, "
        f"expected reason {REASON_NOT_INSTALLED}"
    )
    assert module_condition(module, "Ready") == "True", (
        f"{MODULE_KIND}/{MODULE_CR_NAME} is not Ready after both operators restarted: "
        f"conditions: {', '.join(sorted(module_conditions(module)))}"
    )


def test_service_reports_payload_capture_unavailable(
    service: ServiceAddress, custom_api: client.CustomObjectsApi
) -> None:
    """The workload operator names the missing CRD, so payload capture is off for a reason."""
    resource = read_service(custom_api, service.namespace, service.name)
    LOGGER.info("%s/%s conditions: %s", service.namespace, service.name, describe_conditions(resource))

    reported = condition(resource, SERVICE_CONDITION)
    assert reported is not None, (
        f"{service.namespace}/{service.name} reports no {SERVICE_CONDITION} condition; "
        f"conditions: {describe_conditions(resource)}"
    )
    assert reported.get("status") == "False", (
        f"{SERVICE_CONDITION} is {reported.get('status')}, expected False with no serving API: {reported}"
    )
    assert reported.get("reason") == REASON_SERVING_CRD_NOT_FOUND, (
        f"{SERVICE_CONDITION} reason is {reported.get('reason')!r}, expected "
        f"{REASON_SERVING_CRD_NOT_FOUND!r}: {reported}"
    )
    assert SERVING_CRD_NAME in (reported.get("message") or ""), (
        f"{SERVICE_CONDITION} message does not name {SERVING_CRD_NAME}: {reported.get('message')!r}"
    )


def test_service_operand_runs_without_payload_capture(
    service: ServiceAddress,
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    custom_api: client.CustomObjectsApi,
) -> None:
    """Payload capture being unavailable does not stop the service the operator deploys."""
    resource = read_service(custom_api, service.namespace, service.name)
    LOGGER.info("%s/%s conditions: %s", service.namespace, service.name, describe_conditions(resource))

    assert_deployment_healthy(apps_client, core_client, name=SERVICE_NAME, namespace=service.namespace)

    degraded = condition(resource, "Degraded")
    if degraded is not None and degraded.get("status") == "True":
        message = f"{degraded.get('reason')} {degraded.get('message')}".lower()
        assert "serving" not in message and "inferenceservice" not in message, (
            f"{service.namespace}/{service.name} is degraded because serving is missing: {degraded}"
        )


def test_service_removal_does_not_need_the_serving_api(
    service: ServiceAddress, custom_api: client.CustomObjectsApi
) -> None:
    """Removal reads the serving state the way a reconcile does, and must still finish.

    #1136 made the finalizer read the serving contract before taking back what the operator
    wrote. That read cannot be allowed to fail the removal: a stuck finalizer blocks the
    deletion of the very resource it exists to clean up after. The module's service is the
    subject because its finalizer has certainly been added, and this check is last in the
    file because it deletes it.
    """
    deadline = time.monotonic() + FINALIZER_TIMEOUT
    while not (read_service(custom_api, service.namespace, service.name).get("metadata") or {}).get("finalizers"):
        if time.monotonic() >= deadline:
            pytest.fail(
                f"TrustyAIService/{service.name} in {service.namespace} has no finalizer after "
                f"{FINALIZER_TIMEOUT}s, so the removal path below is not the one under test",
                pytrace=False,
            )
        time.sleep(FINALIZER_INTERVAL)

    LOGGER.info("deleting %s/%s", service.namespace, service.name)
    custom_api.delete_namespaced_custom_object(
        group=API_GROUP,
        version=STORAGE_VERSION,
        namespace=service.namespace,
        plural=PLURAL,
        name=service.name,
    )

    deadline = time.monotonic() + FINALIZER_TIMEOUT
    while True:
        try:
            resource = custom_api.get_namespaced_custom_object(
                group=API_GROUP,
                version=STORAGE_VERSION,
                namespace=service.namespace,
                plural=PLURAL,
                name=service.name,
            )
        except ApiException as exc:
            if exc.status == 404:
                LOGGER.info("%s/%s is gone", service.namespace, service.name)
                return
            raise
        finalizers = (resource.get("metadata") or {}).get("finalizers") or []
        if time.monotonic() >= deadline:
            pytest.fail(
                f"TrustyAIService/{service.name} in {service.namespace} is still there {FINALIZER_TIMEOUT}s after "
                f"deletion, with finalizers {finalizers}: the removal path did not finish on a cluster with no "
                "serving API",
                pytrace=False,
            )
        LOGGER.info("waiting for the finalizer of %s/%s: %s", service.namespace, service.name, finalizers)
        time.sleep(FINALIZER_INTERVAL)
