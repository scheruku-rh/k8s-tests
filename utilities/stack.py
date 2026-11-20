"""Bring up what `k8s-tests up` promises: the module resource and the workload operator.

`k8s-tests deploy` runs this once it has deployed the module operator, so what `up`
leaves behind is what the checks expect to find: the `TrustyAI` module resource,
reconciled, and the workload operator Deployment the module operator deploys from
it - running the images this run asked for, with the operand CRDs' conversion
webhooks trusting the certificate Kind cannot issue.

There is no manifest behind any of it: the resource is built by
`utilities.trustyai.module_manifest`, the same function the `trustyai_module`
fixture uses when a cluster has no resource for it to adopt. The operands on top -
an `EvalHub` instance, a `TrustyAIService`, the jobs that call a model - are
created by the tests, never here.

`KUBE_CONTEXT` and `NAMESPACE` are read the way the suite reads them.
"""

from __future__ import annotations

import logging
import sys

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient

from utilities.kube import context_from_env, load_clients, namespace_from_env, wait_for_deployment_available
from utilities.trustyai import (
    MODULE_CR_NAME,
    MODULE_KIND,
    WORKLOAD_OPERATOR_DEPLOYMENT,
    apply_module,
    module_api_version,
    point_conversion_webhooks_at_ca,
    wait_for_rendered_workload,
    wait_for_workload_operator,
    webhook_ca_bundle,
)

LOGGER = logging.getLogger(__name__)


def bring_up() -> None:
    """Apply the module resource, then wait until the stack under it is the one this run asked for."""
    context = context_from_env()
    namespace = namespace_from_env()
    LOGGER.info("bringing the TrustyAI module resource up on context %s in namespace %s", context, namespace)
    core_client, apps_client = load_clients(context=context)
    api_client = core_client.api_client
    dynamic_client = DynamicClient(api_client)

    api_version = module_api_version(api_client)
    apply_module(dynamic_client, api_version)
    wait_for_workload_operator(apps_client, namespace)
    wait_for_rendered_workload(apps_client, namespace)
    wait_for_deployment_available(apps_client, WORKLOAD_OPERATOR_DEPLOYMENT, namespace, core_client=core_client)
    point_conversion_webhooks_at_ca(api_client, webhook_ca_bundle(core_client, namespace))
    LOGGER.info(
        "%s/%s and %s/%s are up; operands are created by the tests",
        MODULE_KIND,
        MODULE_CR_NAME,
        namespace,
        WORKLOAD_OPERATOR_DEPLOYMENT,
    )


def main() -> int:
    """Run the bring-up, turning the outcomes the shared helpers raise into an exit status."""
    logging.basicConfig(level=logging.INFO, format="==> %(message)s", stream=sys.stderr)
    try:
        bring_up()
    except (pytest.skip.Exception, pytest.fail.Exception) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ApiException as exc:
        print(f"error: {exc.status} {exc.reason}: {(exc.body or '').strip()}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
