"""Fixtures shared by the TrustyAI operator test modules."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any

import pytest
from kubernetes import client
from kubernetes.dynamic import DynamicClient

from utilities.emulator import (
    FORWARDED_PORT,
    deploy_emulator,
    emulator_name,
    emulator_port,
    model_url,
    read_credential,
    remove_emulator,
)
from utilities.emulator import emulator_namespace as emulator_namespace_from_env
from utilities.evalhub import (
    CONDITION_READY,
    EVALHUB_NAME,
    EVALHUB_SERVICE_PORT,
    EVALHUB_TENANT_LABEL_KEY,
    EVALHUB_TENANT_LABEL_VALUE,
    TENANT_IDENTITY_NAME,
    create_evalhub_instance,
    create_namespace,
    create_tenant_identity,
    evalhub_api_version,
    grant_tenant_job_access,
    instance_tls_cert,
    release_namespace,
    seed_instance_tls,
    seed_tenant_ca,
    wait_for_evalhub_condition,
    wait_for_tenant_rbac,
)
from utilities.kube import keep_resources, mint_token, wait_for_deployment_available
from utilities.model import (
    MODEL_CREDENTIALS_SECRET,
    ModelEndpoint,
    ensure_model_credentials,
    external_model_key,
    external_model_url,
    model_name,
)
from utilities.portforward import port_forward
from utilities.trustyai import (
    MODULE_CR_NAME,
    MODULE_CRD_NAME,
    MODULE_KIND,
    MODULE_OPERATOR_DEPLOYMENT,
    WORKLOAD_OPERATOR_DEPLOYMENT,
    crd_exists,
    delete_module,
    deployment_exists,
    ensure_module,
    module_api_version,
    point_conversion_webhooks_at_ca,
    wait_for_rendered_workload,
    wait_for_workload_operator,
    webhook_ca_bundle,
)
from utilities.trustyai_service import SERVICE_NAME, ServiceAddress, service_environment

LOGGER = logging.getLogger(__name__)

# Set by `k8s-tests test --trustyai-service-img ...`: the operand image the operator was
# told to deploy, which the checks then confirm the Deployment actually runs.
SERVICE_IMG_VAR = "TRUSTYAI_SERVICE_IMG"

# The Secret a job's `model.auth.secret_ref` names when the checks use the
# emulator's credential rather than MODEL_KEY.
EMULATOR_TENANT_SECRET = "emulator-credentials"


@pytest.fixture(scope="session")
def modular_trustyai_architecture(api_client: client.ApiClient, apps_client: client.AppsV1Api, namespace: str) -> None:
    """Skip the module checks when the cluster has not adopted the modular operator path.

    On the Kind harness this always holds, because the harness deploys the module
    operator itself. The fixture is what lets the same suite point at a platform
    where TrustyAI is still served by the single operator.
    """
    if not crd_exists(api_client, MODULE_CRD_NAME):
        pytest.skip(
            f"TrustyAI module CRD {MODULE_CRD_NAME} is not installed; "
            "modular architecture tests require the platform module handler"
        )
    if not deployment_exists(apps_client, MODULE_OPERATOR_DEPLOYMENT, namespace):
        pytest.skip(
            f"deployment {namespace}/{MODULE_OPERATOR_DEPLOYMENT} is not present; modular architecture is not enabled"
        )


@pytest.fixture(scope="session")
def trustyai_module(
    api_client: client.ApiClient,
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    dynamic_client: DynamicClient,
    namespace: str,
) -> Iterator[dict[str, Any] | None]:
    """Ensure the module resource the module operator reconciles is there, and wait for the stack under it.

    The harness ships no manifest for it: `k8s-tests deploy` applies the resource through
    `utilities.stack`, and a cluster brought up another way gets it created here,
    the way a user would, and removed again unless the run keeps its resources. A
    resource that is already there - the one `k8s-tests deploy` created, a
    platform-managed install, or a Kind cluster a KEEP_RESOURCES run left populated -
    is never rewritten and never deleted by this session.

    Creating the resource is only the first step: the workload operator it makes
    the module operator deploy has to exist, and it has to be running and resolving
    the images this run asked for before a check creates an operand, and the
    conversion webhooks the checks go through have to trust the certificate Kind
    cannot issue.

    A cluster without the module operator has nothing to reconcile the resource and
    already runs whatever stack the platform deployed, so the fixture leaves it
    alone and yields None: the checks that need the module resource skip on their
    own, and the ones that only need the workload operator still run.
    """
    if not crd_exists(api_client, MODULE_CRD_NAME) or not deployment_exists(
        apps_client, MODULE_OPERATOR_DEPLOYMENT, namespace
    ):
        LOGGER.info(
            "no module operator (%s/%s with CRD %s); leaving the stack as the platform runs it",
            namespace,
            MODULE_OPERATOR_DEPLOYMENT,
            MODULE_CRD_NAME,
        )
        yield None
        return
    api_version = module_api_version(api_client)
    module, created = ensure_module(dynamic_client, api_version)
    if not created:
        LOGGER.info(
            "%s/%s was already on the cluster; leaving its spec alone and not deleting it afterwards",
            MODULE_KIND,
            MODULE_CR_NAME,
        )
    wait_for_workload_operator(apps_client, namespace)
    wait_for_rendered_workload(apps_client, namespace)
    wait_for_deployment_available(apps_client, WORKLOAD_OPERATOR_DEPLOYMENT, namespace, core_client=core_client)
    point_conversion_webhooks_at_ca(api_client, webhook_ca_bundle(core_client, namespace))
    try:
        yield module
    finally:
        if created:
            if keep_resources():
                LOGGER.warning(
                    "KEEP_RESOURCES is set: leaving %s/%s on the cluster, and the workload operator and operands "
                    "below it",
                    MODULE_KIND,
                    MODULE_CR_NAME,
                )
            else:
                delete_module(dynamic_client, api_version)


@pytest.fixture(scope="session")
def evalhub_instance_namespace(core_client: client.CoreV1Api) -> Iterator[str]:
    """Namespace holding the EvalHub instance the tests create, removed afterwards."""
    name = f"trustyai-evalhub-{uuid.uuid4().hex[:6]}"
    create_namespace(core_client, name)
    try:
        yield name
    finally:
        release_namespace(core_client, name)


@pytest.fixture(scope="session")
def evalhub_instances_in_cluster(dynamic_client: DynamicClient) -> int:
    """Warn when the cluster holds more than one EvalHub instance.

    Every instance is named `evalhub` and reconciles every tenant-labelled
    namespace, so a leftover instance, or one a `KEEP_RESOURCES` run left behind,
    keeps writing to namespaces it did not create. The run grants its own job
    access, so it does not depend on which instance reconciled last, but the
    leftovers still consume the cluster and are worth naming here.
    """
    plural = dynamic_client.resources.get(api_version="trustyai.opendatahub.io/v1", kind="EvalHub")
    instances = plural.get().items
    if len(instances) > 1:
        namespaces = sorted(instance.metadata.namespace for instance in instances)
        LOGGER.warning(
            "the cluster holds %d EvalHub instances (%s); a leftover instance reconciles every tenant-labelled "
            "namespace, including ones it did not create. Delete the namespaces you do not need.",
            len(instances),
            ", ".join(namespaces),
        )
    return len(instances)


@pytest.fixture(scope="session")
def evalhub_instance(
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    dynamic_client: DynamicClient,
    api_client: client.ApiClient,
    evalhub_instance_namespace: str,
    trustyai_module: dict[str, Any] | None,
) -> dict[str, Any]:
    """Create an EvalHub instance from Python and wait until it serves.

    The tests own the instance, its namespace and, because Kind has no service
    CA, its TLS material; the operator owns everything below that - which is why
    the instance is only created once the workload operator is up, behind
    `trustyai_module`.
    """
    seed_instance_tls(core_client, EVALHUB_NAME, evalhub_instance_namespace)
    api_version = evalhub_api_version(api_client)
    create_evalhub_instance(dynamic_client, api_version, EVALHUB_NAME, evalhub_instance_namespace)
    instance = wait_for_evalhub_condition(
        dynamic_client,
        api_version,
        EVALHUB_NAME,
        evalhub_instance_namespace,
        CONDITION_READY,
    )
    wait_for_deployment_available(apps_client, EVALHUB_NAME, evalhub_instance_namespace, core_client=core_client)
    return instance


@pytest.fixture(scope="session")
def evalhub_tenant_namespace(core_client: client.CoreV1Api, evalhub_instance: dict[str, Any]) -> Iterator[str]:
    """A namespace labelled as an EvalHub tenant, which the operator seeds."""
    name = f"trustyai-evalhub-tenant-{uuid.uuid4().hex[:6]}"
    create_namespace(core_client, name, labels={EVALHUB_TENANT_LABEL_KEY: EVALHUB_TENANT_LABEL_VALUE})
    try:
        yield name
    finally:
        release_namespace(core_client, name)


@pytest.fixture(scope="session")
def emulator_namespace() -> str:
    """Namespace the harness deployed the LLM emulator into."""
    return emulator_namespace_from_env()


@pytest.fixture(scope="session")
def emulator(
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    emulator_namespace: str,
) -> Iterator[client.V1Deployment]:
    """Deploy the LLM emulator and remove it when the session ends.

    The checks own it, the way they own the EvalHub instance: the namespace, image,
    port and token are parameters, and nothing is left behind in the cluster.
    When MODEL_URL points the checks at a real endpoint there is nothing to
    deploy, so the emulator checks skip instead.
    """
    if external_model_url():
        pytest.skip("MODEL_URL is set, so the checks call that endpoint; the LLM emulator is not deployed")
    deployment = deploy_emulator(core_client, apps_client, emulator_namespace)
    try:
        yield deployment
    finally:
        if keep_resources():
            LOGGER.warning(
                "keeping the LLM emulator in namespace %s: kubectl -n %s get all,cm,secret",
                emulator_namespace,
                emulator_namespace,
            )
        else:
            remove_emulator(core_client, apps_client, emulator_namespace)


@pytest.fixture(scope="session")
def emulator_credentials(
    core_client: client.CoreV1Api,
    emulator_namespace: str,
    emulator: client.V1Deployment,
) -> str:
    """The token the emulator requires, from the Secret the emulator fixture wrote."""
    return read_credential(core_client, emulator_namespace)


@pytest.fixture(scope="session")
def emulator_url(kube_context: str, emulator_namespace: str, emulator: client.V1Deployment) -> Iterator[str]:
    """HTTP base URL of the emulator, reached through a port-forward to its Service."""
    with port_forward(
        kube_context,
        emulator_namespace,
        f"service/{emulator_name()}",
        FORWARDED_PORT,
        emulator_port(),
    ) as url:
        yield url


@pytest.fixture(scope="session")
def model_endpoint(request: pytest.FixtureRequest) -> ModelEndpoint:
    """The endpoint the benchmark jobs call, and the credential it needs.

    MODEL_URL selects a real endpoint, in which case the fixtures deploy no
    emulator. Without it the jobs call the emulator, whose token is the
    credential and whose `/__auth/state` endpoint can prove the job reached it.
    MODEL_KEY, when set, is the credential in either case.
    """
    url = external_model_url()
    key = external_model_key()
    if url:
        LOGGER.info("using the real model endpoint %s", url)
        return ModelEndpoint(
            url=url,
            name=model_name(),
            auth_secret_name=MODEL_CREDENTIALS_SECRET if key else None,
            credential=key,
        )
    credentials = request.getfixturevalue("emulator_credentials")
    state_url = request.getfixturevalue("emulator_url")
    secret_name = MODEL_CREDENTIALS_SECRET if key else EMULATOR_TENANT_SECRET
    return ModelEndpoint(
        url=model_url(emulator_namespace_from_env()),
        name=model_name(),
        auth_secret_name=secret_name,
        credential=credentials,
        observes_requests=True,
        state_url=state_url,
    )


@pytest.fixture(scope="session")
def evalhub_api_base(
    kube_context: str,
    evalhub_instance: dict[str, Any],
    evalhub_instance_namespace: str,
) -> Iterator[str]:
    """HTTPS base URL of the instance's API, through a port-forward to its Service."""
    with port_forward(
        context=kube_context,
        namespace=evalhub_instance_namespace,
        target=f"service/{EVALHUB_NAME}",
        local_port=FORWARDED_PORT + 1,
        remote_port=EVALHUB_SERVICE_PORT,
        scheme="https",
    ) as base_url:
        yield base_url


@pytest.fixture(scope="session")
def evalhub_tenant_identity(
    core_client: client.CoreV1Api,
    api_client: client.ApiClient,
    evalhub_instance: dict[str, Any],
    evalhub_tenant_namespace: str,
) -> str:
    """A ServiceAccount in the tenant namespace, granted the EvalHub API."""
    return create_tenant_identity(
        core_client,
        client.RbacAuthorizationV1Api(api_client),
        evalhub_tenant_namespace,
    )


@pytest.fixture(scope="session")
def evalhub_tenant_token(kube_context: str, evalhub_tenant_identity: str, evalhub_tenant_namespace: str) -> str:
    """A bearer token for the tenant identity, which the proxy authorizes by SAR."""
    return mint_token(kube_context, evalhub_tenant_namespace, TENANT_IDENTITY_NAME)


@pytest.fixture(scope="session")
def evalhub_tenant_ca(
    core_client: client.CoreV1Api,
    evalhub_instance: dict[str, Any],
    evalhub_instance_namespace: str,
    evalhub_tenant_namespace: str,
) -> None:
    """Wait for the tenant to be provisioned, then give it the instance certificate.

    The evaluation sidecar reads `service-ca.crt` from the tenant's ConfigMap to
    verify the instance it reports results back to; without this every job runs to
    completion and then loses its results on a TLS failure.
    """
    rbac_client = client.RbacAuthorizationV1Api(core_client.api_client)
    wait_for_tenant_rbac(rbac_client, evalhub_tenant_namespace, evalhub_instance_namespace)
    # Belt and braces: the wait above can only observe the operator's binding, and
    # another instance in the cluster may rewrite it a moment later.
    grant_tenant_job_access(rbac_client, evalhub_tenant_namespace, evalhub_instance_namespace)
    cert_pem = instance_tls_cert(core_client, evalhub_instance_namespace)
    seed_tenant_ca(core_client, evalhub_tenant_namespace, cert_pem)


@pytest.fixture(scope="session")
def evalhub_model_credentials(
    core_client: client.CoreV1Api,
    evalhub_tenant_namespace: str,
    evalhub_tenant_ca: None,
    model_endpoint: ModelEndpoint,
) -> str | None:
    """The model credential Secret a job's `model.auth.secret_ref` points at.

    `api-key` is the key EvalHub resolves, so the value never enters the job
    payload. With MODEL_KEY set the Secret has the known name
    `model-credentials`; against the emulator without MODEL_KEY it carries the
    emulator's token. An endpoint that needs no credential gets no Secret, and
    the job carries no `model.auth` block.
    """
    if not model_endpoint.auth_secret_name or not model_endpoint.credential:
        LOGGER.info("the model endpoint needs no credential; the job will omit model.auth")
        return None
    return ensure_model_credentials(
        core_client,
        evalhub_tenant_namespace,
        model_endpoint.auth_secret_name,
        model_endpoint.credential,
    )


# --- the TrustyAI service --------------------------------------------------


@pytest.fixture(scope="session")
def workload_namespaces(core_client: client.CoreV1Api) -> Iterator[Callable[..., str]]:
    """Create namespaces for workloads a check brings itself, removed when the check ends.

    The operators are the session's; a workload is the check's. Anything a check
    needs its own copy of - a `TrustyAIService`, an `LMEvalJob`, a guardrails server -
    gets a namespace of its own from here, and the namespace takes the workload with
    it when the check finishes, the way `opendatahub-tests` does it.
    """

    def create(request: pytest.FixtureRequest, prefix: str = "trustyai-workload") -> str:
        name = f"{prefix}-{uuid.uuid4().hex[:6]}"
        create_namespace(core_client, name)
        request.addfinalizer(lambda: release_namespace(core_client, name))
        LOGGER.info("created workload namespace %s", name)
        return name

    yield create


@pytest.fixture(scope="session")
def trustyai_services(
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    rbac_client: client.RbacAuthorizationV1Api,
    custom_api: client.CustomObjectsApi,
    kube_context: str,
    workload_namespaces: Callable[..., str],
    trustyai_module: dict[str, Any] | None,
) -> Iterator[Callable[..., ServiceAddress]]:
    """Create a `TrustyAIService` workload and point the checks at its API.

    `trustyai_services(request)` gives it a namespace of its own;
    `trustyai_services(request, namespace=ns)` uses one the check already has, which
    is how two services can share a namespace. Either way the service, its operand
    and its port-forwards go away when the check ends.

    It comes after `trustyai_module` for the same reason the other operand fixtures
    do: there is no point creating a resource before the workload operator that
    reconciles it is up.
    """

    def create(
        request: pytest.FixtureRequest,
        namespace: str | None = None,
        name: str = SERVICE_NAME,
    ) -> ServiceAddress:
        if namespace is None:
            namespace = workload_namespaces(request, prefix="trustyai-service")
        stack = ExitStack()
        request.addfinalizer(stack.close)
        return stack.enter_context(
            service_environment(
                core_client=core_client,
                apps_client=apps_client,
                rbac_client=rbac_client,
                custom_api=custom_api,
                kube_context=kube_context,
                namespace=namespace,
                name=name,
            )
        )

    yield create


@pytest.fixture(scope="session")
def evalhub_tenants(
    core_client: client.CoreV1Api,
    evalhub_instance: dict[str, Any],
) -> Iterator[Callable[..., str]]:
    """Label a namespace as an EvalHub tenant, removed when the check ends.

    An instance reconciles every tenant-labelled namespace, so a check that needs its
    own evaluation target asks for its own tenant namespace rather than its own
    instance.
    """

    def create(request: pytest.FixtureRequest, prefix: str = "trustyai-evalhub-tenant") -> str:
        name = f"{prefix}-{uuid.uuid4().hex[:6]}"
        create_namespace(core_client, name, labels={EVALHUB_TENANT_LABEL_KEY: EVALHUB_TENANT_LABEL_VALUE})
        request.addfinalizer(lambda: release_namespace(core_client, name))
        LOGGER.info("created tenant namespace %s", name)
        return name

    yield create


@pytest.fixture(scope="session")
def trustyai_service_image() -> str:
    """The operand image the workload operator was told to deploy, if the run named one."""
    return os.environ.get(SERVICE_IMG_VAR, "")
