#!/usr/bin/env bash
# Build every image the harness can build from source and push it to a local
# registry container that the Kind nodes pull from.
#
# The registry listens on 127.0.0.1:${REGISTRY_PORT} and is joined to the kind
# container network under an alias. hack/lib.sh writes a containerd hosts.toml
# on every node, so pods that reference localhost:${REGISTRY_PORT}/... resolve
# to the registry. The cluster must have been created with the certs.d config
# path in hack/kind/cluster.yaml (recreate an older cluster with make down).
#
# Repositories (defaults are siblings of this checkout):
#   OPERATOR_REPO         trustyai-service-operator   (required)
#   EVALHUB_REPO          eval-hub                    EvalHub service image
#   EVALHUB_CONTRIB_REPO  eval-hub-contrib            adapter images
#   LMES_REPO             lm-evaluation-harness       lm_evaluation_harness adapter
#
# ADAPTERS selects the adapters: "all" (the default), "lmes" for only the
# adapter the adapter checks use, or a comma/space separated list of
# eval-hub-contrib adapter directory names.
#
# The image references are written to reports/images.env ($IMAGES_ENV_FILE) and
# printed together with the `make deploy` invocation that uses them.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_operator_repo

# The engine only has to be resolved once - the lookup probes podman and docker -
# and the per-image helpers below run in subshells, so export the answer.
CONTAINER_ENGINE=$(container_engine)
export CONTAINER_ENGINE

cluster=${KIND_CLUSTER:-trustyai}
tag=${TAG:-dev}
registry_port=${REGISTRY_PORT:-5005}
registry="localhost:${registry_port}"
platform=${PLATFORM:-$(host_platform)}
adapters=${ADAPTERS:-all}
skip_build=${SKIP_BUILD:-false}
remove_after_push=${REMOVE_AFTER_PUSH:-false}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$here/.." && pwd)

if [[ "$skip_build" == true && "$remove_after_push" == true ]]; then
	die "SKIP_BUILD=true reuses images in the local store, so it cannot be combined with REMOVE_AFTER_PUSH=true"
fi

evalhub_repo=${EVALHUB_REPO:-$repo_root/../eval-hub}
contrib_repo=${EVALHUB_CONTRIB_REPO:-$repo_root/../eval-hub-contrib}
lmes_repo=${LMES_REPO:-$repo_root/../lm-evaluation-harness}

CONTRIB_ADAPTERS=(
	atif clear databench deepeval evalhub-post-processor followbench guidellm
	ifbench inspect lighteval lm-eval mteb nemo-guardrails promptfoo ragas ruler
	swebench tooleval toolbench-server wildguard
)

adapter_selected() {
	local wanted=$1 item
	for item in ${adapters//,/ }; do
		[[ "$item" == all || "$item" == "$wanted" ]] && return 0
	done
	return 1
}

# validate_adapters rejects an ADAPTERS entry that matches nothing: a typo would
# otherwise build no adapter, write an images.env without the images the caller
# asked for, and leave those providers on the operator's own pins.
validate_adapters() {
	local item candidate known
	for item in ${adapters//,/ }; do
		[[ "$item" == all || "$item" == lmes ]] && continue
		known=false
		for candidate in "${CONTRIB_ADAPTERS[@]}"; do
			[[ "$item" == "$candidate" ]] && known=true && break
		done
		[[ "$known" == true ]] ||
			die "unknown ADAPTERS entry '$item'; use all, lmes, or one of: ${CONTRIB_ADAPTERS[*]}"
	done
}

validate_adapters

kind_cmd get clusters | grep -qx "$cluster" ||
	die "Kind cluster '$cluster' not found; create it with make cluster (or set KIND_PROVIDER to the engine that created it)"

export KIND_CLUSTER="$cluster" REGISTRY_PORT="$registry_port" PLATFORM="$platform"
ensure_registry
configure_registry_mirrors
info "local registry ready on 127.0.0.1:${registry_port}"

adapter_image_name() {
	case "$1" in
	clear) printf 'community-ibm-clear' ;;
	evalhub-post-processor) printf 'evalhub-post-processor' ;;
	*) printf 'community-%s' "$1" ;;
	esac
}

adapter_context() {
	case "$1" in
	toolbench-server) printf 'adapters/tooleval/tool-server' ;;
	*) printf 'adapters/%s' "$1" ;;
	esac
}

image_present() {
	local engine
	engine=$(container_engine)
	"$engine" image inspect "$1" >/dev/null 2>&1
}

# push_and_maybe_remove pushes the image and, when REMOVE_AFTER_PUSH=true, drops
# the local copy: the registry has it and the adapter images are large, so a
# small podman machine should not keep them.
push_and_maybe_remove() {
	push_image "$1"
	if [[ "$remove_after_push" == true ]]; then
		"$(container_engine)" rmi "$1" >/dev/null 2>&1 ||
			warn "could not remove $1 from the local store after pushing"
	fi
}

built=()
built_lmes=false
inspect_img=""
lighteval_img=""

# Operators: the workload operator and the module operator that deploys it.
workload_img="$registry/trustyai-service-operator:$tag"
module_img="$registry/trustyai-operator-module:$tag"
if [[ "$skip_build" == true ]]; then
	image_present "$workload_img" || die "SKIP_BUILD=true but $workload_img is not present"
	image_present "$module_img" || die "SKIP_BUILD=true but $module_img is not present"
else
	WORKLOAD_IMG="$workload_img" MODULE_IMG="$module_img" PLATFORM="$platform" TAG="$tag" "$here/build-images.sh"
fi
push_and_maybe_remove "$workload_img"
push_and_maybe_remove "$module_img"
built+=("$workload_img" "$module_img")

# EvalHub service image.
evalhub_img="$registry/evalhub/evalhub:$tag"
if [[ "$skip_build" == true ]]; then
	image_present "$evalhub_img" || die "SKIP_BUILD=true but $evalhub_img is not present"
else
	[[ -f "$evalhub_repo/Containerfile" ]] ||
		die "missing $evalhub_repo/Containerfile; set EVALHUB_REPO"
	evalhub_args=(--build-arg "BUILD_NUMBER=k8s-tests")
	evalhub_args+=(--build-arg "BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)")
	if [[ -d "$evalhub_repo/.git" ]]; then
		evalhub_args+=(--build-arg "GIT_HASH=$(git -C "$evalhub_repo" rev-parse --short HEAD)")
	fi
	build_image "$evalhub_img" "$evalhub_repo" "$evalhub_repo/Containerfile" "${evalhub_args[@]}"
fi
push_and_maybe_remove "$evalhub_img"
built+=("$evalhub_img")

# eval-hub-contrib adapters.
for adapter in "${CONTRIB_ADAPTERS[@]}"; do
	adapter_selected "$adapter" || continue
	context="$contrib_repo/$(adapter_context "$adapter")"
	image="$registry/evalhub/$(adapter_image_name "$adapter"):$tag"
	if [[ "$skip_build" == true ]]; then
		image_present "$image" || die "SKIP_BUILD=true but $image is not present"
	else
		[[ -f "$context/Containerfile" ]] ||
			die "missing $context/Containerfile; set EVALHUB_CONTRIB_REPO"
		case "$adapter" in
		toolbench-server)
			build_image "$image" "$context" "$context/Containerfile" --build-arg FETCH_FULL_CACHE=0
			;;
		promptfoo)
			if [[ -n "${BASE_IMAGE:-}" ]]; then
				build_image "$image" "$context" "$context/Containerfile" --build-arg "BASE_IMAGE=${BASE_IMAGE}"
			else
				build_image "$image" "$context" "$context/Containerfile"
			fi
			;;
		*) build_image "$image" "$context" "$context/Containerfile" ;;
		esac
	fi
	push_and_maybe_remove "$image"
	built+=("$image")
	case "$adapter" in
	inspect) inspect_img=$image ;;
	lighteval) lighteval_img=$image ;;
	esac
done

# The lm_evaluation_harness provider's pod image is the fork's Dockerfile.lmes-job,
# not the eval-hub-contrib lm-eval adapter.
lmes_img=""
if adapter_selected lmes; then
	lmes_img="$registry/evalhub/community-lmes:$tag"
	if [[ "$skip_build" == true ]]; then
		image_present "$lmes_img" || die "SKIP_BUILD=true but $lmes_img is not present"
	else
		[[ -f "$lmes_repo/Dockerfile.lmes-job" ]] ||
			die "missing $lmes_repo/Dockerfile.lmes-job; set LMES_REPO"
		build_image "$lmes_img" "$lmes_repo" "$lmes_repo/Dockerfile.lmes-job" \
			--build-arg "TARGETARCH=${platform##*/}"
	fi
	push_and_maybe_remove "$lmes_img"
	built+=("$lmes_img")
	built_lmes=true
fi

env_file=$(images_env_path)
mkdir -p "$(dirname "$env_file")"
{
	printf '# Generated by hack/build-all-images.sh: images pushed to %s\n' "$registry"
	printf 'WORKLOAD_IMG=%s\n' "$workload_img"
	printf 'MODULE_IMG=%s\n' "$module_img"
	printf 'EVALHUB_IMG=%s\n' "$evalhub_img"
	if [[ "$built_lmes" == true ]]; then
		printf 'LMES_IMG=%s\n' "$lmes_img"
	fi
	if [[ -n "$inspect_img" ]]; then
		printf 'INSPECT_IMG=%s\n' "$inspect_img"
	fi
	if [[ -n "$lighteval_img" ]]; then
		printf 'LIGHTEVAL_IMG=%s\n' "$lighteval_img"
	fi
} >"$env_file"

info "pushed ${#built[@]} images to $registry"
info "image references written to $env_file"
info "deploy the stack with the local images:"
printf '  make deploy OPERATOR_REPO=%s WORKLOAD_IMG=%s MODULE_IMG=%s EVALHUB_IMG=%s' \
	"$OPERATOR_REPO" "$workload_img" "$module_img" "$evalhub_img"
[[ "$built_lmes" == true ]] && printf ' LMES_IMG=%s' "$lmes_img"
[[ -n "$inspect_img" ]] && printf ' INSPECT_IMG=%s' "$inspect_img"
[[ -n "$lighteval_img" ]] && printf ' LIGHTEVAL_IMG=%s' "$lighteval_img"
printf '\n'

