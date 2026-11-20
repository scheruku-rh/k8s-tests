#!/usr/bin/env bash
# Build the TrustyAI workload and module operator images from OPERATOR_REPO.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_operator_repo
engine=$(container_engine)
platform=${PLATFORM:-$(host_platform)}
TAG=${TAG:-dev}

# Building the load-path images invalidates the references `make images-all`
# records for the registry flow; tests read that file to know what was built.
rm -f "$(images_env_path)"

# `docker build` is buildx, and when the active builder uses the docker-container
# driver the result is only kept in the builder's cache unless it is explicitly
# loaded into the engine's image store. Without --load the build reports success
# but `docker save` cannot find the image, which is what `hack/load-images.sh`
# needs to hand Kind an archive. `podman build` writes to the local store already
# and has no --load. The platform is explicit so a pinned builder or
# DOCKER_DEFAULT_PLATFORM cannot produce an image the Kind node cannot run.
build=(build)
if [[ "$engine" == docker ]] && "$engine" buildx version >/dev/null 2>&1; then
	build+=(--load)
fi

module_dir="$OPERATOR_REPO/trustyai-operator-module"
[[ -f "$module_dir/Dockerfile" ]] || die "missing $module_dir/Dockerfile"

# The module image bakes the workload operator manifests at build time, so a
# stale template silently deploys the previous workload manifests.
if ! diff -rq "$OPERATOR_REPO/config" "$module_dir/config/manifests-template" >/dev/null 2>&1; then
	warn "config/ and trustyai-operator-module/config/manifests-template/ differ;"
	warn "run 'make -C $OPERATOR_REPO sync-trustyai-module-manifests' to refresh the template"
fi

info "building workload operator image $WORKLOAD_IMG for $platform with $engine"
"$engine" "${build[@]}" \
	--platform "$platform" \
	--build-arg "CI_CONTAINER_VERSION=$TAG" \
	-t "$WORKLOAD_IMG" \
	-f "$OPERATOR_REPO/Dockerfile" \
	"$OPERATOR_REPO"

info "building module operator image $MODULE_IMG for $platform with $engine"
"$engine" "${build[@]}" \
	--platform "$platform" \
	--build-arg "VERSION=$TAG" \
	-t "$MODULE_IMG" \
	-f "$module_dir/Dockerfile" \
	"$module_dir"

info "built $WORKLOAD_IMG and $MODULE_IMG"
