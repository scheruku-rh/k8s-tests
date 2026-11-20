#!/usr/bin/env bash
# Deploy the TrustyAI module operator on Kind, overriding the module operator
# image, the workload operator image it renders, the TrustyAI service and EvalHub
# images the workload operator resolves, and the target namespace.
#
# The operators: the module operator, plus the TrustyAI module resource and the
# workload operator it deploys from it. Nothing here depends on a manifest checked
# into this repository - the resource is built in Python. The operands on top of
# that (EvalHub instances and their namespaces and TLS material, TrustyAIService,
# the jobs that call a model) are created by the tests, the way a user or the
# platform operator would.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_operator_repo
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$here/.." && pwd)

# reports/images.env is what `make images-all` pushed, what `deploy-registry`
# passes in and what the checks read. Two things keep it in step with this
# deployment: a key whose value this deploy does not use is dropped (so the tests
# keep comparing against the references that do describe it), and the header below
# - which is what utilities/trustyai.py requires before it trusts the file - is
# only written once the stack running those images is up (see the end of this
# script). A build that was pushed but never deployed, and a deploy that failed
# half way, therefore record nothing the checks would compare against.
deployed_images_header="Written by hack/deploy.sh: the images this deployment runs"
env_file=$(images_env_path)
images_env_kept=""
if [[ -f "$env_file" ]]; then
	images_env_kept=$(mktemp)
	dropped=""
	while IFS= read -r line; do
		[[ -n "$line" && "$line" != "#"* && "$line" == *=* ]] || continue
		key=${line%%=*}
		value=${line#*=}
		current=$(printenv "$key" 2>/dev/null || true)
		if [[ "$value" != "$current" ]]; then
			dropped="${dropped:+$dropped }$key=$value"
			continue
		fi
		printf '%s\n' "$line" >>"$images_env_kept"
	done <"$env_file"
	if [[ -n "$dropped" ]]; then
		warn "$env_file records images this deploy does not use ($dropped); dropping them"
	fi
	if [[ -s "$images_env_kept" ]]; then
		# Unstamped until the deploy below succeeds: an interrupted deployment
		# must not leave a file the checks treat as a record of what is running.
		cp "$images_env_kept" "$env_file"
	else
		warn "removing $env_file: it records no image this deploy uses"
		rm -f "$env_file"
		rm -f "$images_env_kept"
		images_env_kept=""
	fi
fi

module_config="$OPERATOR_REPO/trustyai-operator-module/config"
[[ -d "$module_config/default" ]] || die "missing $module_config/default"
crd_dir="$OPERATOR_REPO/tests/crds"
[[ -d "$crd_dir" ]] || die "missing $crd_dir (external CRD stand-ins)"

# External CRDs the rendered workload manifests reference. Kind has neither
# OpenShift Routes, the Prometheus Operator, nor KServe, but the operators still
# need the API types to exist so their watchers can start.
external_crds=(
	route_crd.yaml
	monitoring.coreos.com_prometheuses.yaml
	monitoring.coreos.com_servicemonitors.yaml
	serving.kserve.io_inferenceservices.yaml
)

info "creating namespace $NAMESPACE"
kube create namespace "$NAMESPACE" --dry-run=client -o yaml | kube apply -f -

info "applying external CRDs from $crd_dir"
for crd in "${external_crds[@]}"; do
	[[ -f "$crd_dir/$crd" ]] || die "missing $crd_dir/$crd"
	kube apply -f "$crd_dir/$crd" >/dev/null
done
for crd in routes.route.openshift.io prometheuses.monitoring.coreos.com \
	servicemonitors.monitoring.coreos.com inferenceservices.serving.kserve.io; do
	kube wait --for=condition=Established "crd/$crd" --timeout "$TIMEOUT" >/dev/null
done

# Render the module operator from a copy of the checkout's config tree so the
# image, the workload image it renders and the namespace can all be overridden
# without editing the operator checkout.
split_image "$MODULE_IMG"
module_name=$IMG_NAME module_tag=$IMG_TAG
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"; [[ -z "${images_env_kept:-}" ]] || rm -f "$images_env_kept"' EXIT
cp -a "$module_config" "$workdir/config"
# EVALHUB_IMG overrides the operand image the module operator passes to the
# workload operator through params.env.
module_env_patch=$(cat <<EOF
      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE
          value: $WORKLOAD_IMG
EOF
)
if [[ -n "${TRUSTYAI_SERVICE_IMG:-}" ]]; then
	module_env_patch+=$(cat <<EOF

      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE
          value: $TRUSTYAI_SERVICE_IMG
EOF
)
fi
if [[ -n "${EVALHUB_IMG:-}" ]]; then
	module_env_patch+=$(cat <<EOF

      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_EVAL_HUB_IMAGE
          value: $EVALHUB_IMG
EOF
)
fi
if [[ -n "${KUBE_RBAC_PROXY_IMG:-}" ]]; then
	module_env_patch+=$(cat <<EOF

      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_KUBE_RBAC_PROXY_IMAGE
          value: $KUBE_RBAC_PROXY_IMG
EOF
)
fi
if [[ -n "${LMES_IMG:-}" ]]; then
	module_env_patch+=$(cat <<EOF

      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_TA_LMES_JOB_IMAGE
          value: $LMES_IMG
EOF
)
fi
if [[ -n "${INSPECT_IMG:-}" ]]; then
	module_env_patch+=$(cat <<EOF

      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_EVAL_HUB_INSPECTAI_IMAGE
          value: $INSPECT_IMG
EOF
)
fi
if [[ -n "${LIGHTEVAL_IMG:-}" ]]; then
	module_env_patch+=$(cat <<EOF

      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_EVAL_HUB_LIGHTEVAL_IMAGE
          value: $LIGHTEVAL_IMG
EOF
)
fi

cat >"$workdir/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NAMESPACE
resources:
  - ./config/default
images:
  - name: trustyai-operator-module-controller
    newName: $module_name
    newTag: $module_tag
patches:
  - target:
      group: apps
      version: v1
      kind: Deployment
      name: trustyai-operator-module-controller-manager
    patch: |-
$module_env_patch
EOF

info "deploying the module operator ($MODULE_IMG) with workload image $WORKLOAD_IMG"
if [[ -n "${EVALHUB_IMG:-}" ]]; then
	info "passing EvalHub image $EVALHUB_IMG to the workload operator"
fi
if [[ -n "${TRUSTYAI_SERVICE_IMG:-}" ]]; then
	info "passing TrustyAI service image $TRUSTYAI_SERVICE_IMG to the workload operator"
fi
if [[ -n "${KUBE_RBAC_PROXY_IMG:-}" ]]; then
	info "passing kube-rbac-proxy image $KUBE_RBAC_PROXY_IMG to the workload operator"
fi
if [[ -n "${LMES_IMG:-}" ]]; then
	info "passing lm_evaluation_harness adapter image $LMES_IMG to the workload operator"
fi
if [[ -n "${INSPECT_IMG:-}" ]]; then
	info "passing Inspect AI adapter image $INSPECT_IMG to the workload operator"
fi
if [[ -n "${LIGHTEVAL_IMG:-}" ]]; then
	info "passing LightEval adapter image $LIGHTEVAL_IMG to the workload operator"
fi
kube apply -k "$workdir" --server-side=true

# rollout status, not just Available: an existing pod set satisfies Available
# while a new image is still rolling out, and the next step reads what the new
# module operator renders.
info "waiting for the module operator deployment"
kube -n "$NAMESPACE" rollout status deployment/trustyai-operator-module-controller-manager \
	--timeout "$TIMEOUT"

info "seeding the Prometheus instance the module operator expects"
cat <<EOF | kube apply -f -
apiVersion: monitoring.coreos.com/v1
kind: Prometheus
metadata:
  name: e2e-prometheus
  namespace: $NAMESPACE
EOF

# On OpenShift the service CA creates these from the serving-cert annotations;
# on Kind they have to exist before the operator pods can start. They are only
# created once so repeat deploys keep the same certificate.
create_tls_secrets() {
	local dir="$workdir/certs"
	python3 "$here/gen-certs.py" --namespace "$NAMESPACE" --out-dir "$dir"
	for secret in webhook-server-cert trustyai-service-operator-metrics-tls; do
		kube -n "$NAMESPACE" create secret tls "$secret" \
			--cert="$dir/tls.crt" --key="$dir/tls.key"
	done
}

if ! kube -n "$NAMESPACE" get secret webhook-server-cert >/dev/null 2>&1; then
	info "creating the webhook and metrics TLS secrets"
	create_tls_secrets
fi

# What `make up` promises also includes the stack under the module operator: the
# module resource, and the workload operator the module operator deploys from it.
# Both come from Python (utilities/stack.py, which builds the resource with the
# helpers the tests use and then waits for the Deployment, the images it renders
# and the conversion webhooks), so there is no manifest to keep in step with the
# operator. The operands on top of that - EvalHub instances, TrustyAIService, the
# jobs that call a model - stay the tests' business.
command -v uv >/dev/null 2>&1 || die "uv is required to create the TrustyAI module resource; install uv"
info "applying the TrustyAI module resource and waiting for the workload operator"
(
	cd "$repo_root"
	KUBE_CONTEXT="$KUBE_CONTEXT" NAMESPACE="$NAMESPACE" uv run --quiet python -m utilities.stack
)

# The module resource, the workload operator and the images it renders are up, so
# the record can now say what this deployment runs. utilities/trustyai.py only
# trusts a file carrying this header, and the checks compare the running images
# against what it records.
if [[ -n "$images_env_kept" ]]; then
	printf '# %s\n' "$deployed_images_header" >"$env_file"
	cat "$images_env_kept" >>"$env_file"
	rm -f "$images_env_kept"
	images_env_kept=""
	info "recorded the deployed images in $env_file"
fi

info "TrustyAI is deployed in namespace $NAMESPACE"
info "  the module operator and the workload operator it deploys are running"
info "  its operands are created by the tests: make test"
