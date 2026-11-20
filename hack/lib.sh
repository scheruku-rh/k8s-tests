#!/usr/bin/env bash
# Shared helpers for the Kind harness. Sourced by the hack/*.sh scripts.

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() {
	printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
	exit 1
}

# cluster_owner_engine prints the engine that has this Kind cluster's node
# containers, or nothing when neither podman nor docker can see one. The
# cluster's provider - not which engine happens to be installed - decides where
# the registry has to live: it joins that engine's `kind` network and the
# scripts exec into the node containers that engine created.
cluster_owner_engine() {
	local cluster=${KIND_CLUSTER:-trustyai} engine
	for engine in podman docker; do
		command -v "$engine" >/dev/null 2>&1 || continue
		"$engine" info >/dev/null 2>&1 || continue
		if [[ -n "$("$engine" ps --all --quiet --filter "label=io.x-k8s.kind.cluster=$cluster" 2>/dev/null)" ]]; then
			printf '%s' "$engine"
			return
		fi
	done
}

# container_engine prints the container engine to build images with.
# CONTAINER_ENGINE is the answer a script exports once it has resolved the
# engine, so the per-image helpers below do not probe podman and docker again.
# Otherwise ENGINE wins when it is set, then KIND_PROVIDER, then the engine that
# owns the cluster's nodes. With no cluster yet podman is preferred (it stores
# images locally and pushes to the plain-HTTP registry without daemon
# configuration) and docker is the fallback.
container_engine() {
	if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
		printf '%s' "$CONTAINER_ENGINE"
		return
	fi
	if [[ -n "${ENGINE:-}" ]]; then
		command -v "$ENGINE" >/dev/null 2>&1 || die "ENGINE=$ENGINE is not installed"
		printf '%s' "$ENGINE"
		return
	fi
	if [[ -n "${KIND_PROVIDER:-}" ]]; then
		command -v "$KIND_PROVIDER" >/dev/null 2>&1 || die "KIND_PROVIDER=$KIND_PROVIDER is not installed"
		printf '%s' "$KIND_PROVIDER"
		return
	fi
	local engine owner
	owner=$(cluster_owner_engine)
	if [[ -n "$owner" ]]; then
		printf '%s' "$owner"
		return
	fi
	for engine in podman docker; do
		if command -v "$engine" >/dev/null 2>&1 && "$engine" info >/dev/null 2>&1; then
			printf '%s' "$engine"
			return
		fi
	done
	if command -v podman >/dev/null 2>&1 || command -v docker >/dev/null 2>&1; then
		die "podman/docker is installed but not reachable; start the podman machine (podman machine start) or Docker Desktop"
	fi
	die "neither podman nor docker is installed"
}

# kind_cmd runs the Kind CLI with the provider the cluster was created with.
# KIND_PROVIDER wins; without it Kind asks the default (docker) provider and
# reports a podman cluster as missing, so the engine that owns the cluster's node
# containers decides instead. `make cluster` exports KIND_EXPERIMENTAL_PROVIDER
# for its own kind calls, and a machine with no cluster yet keeps Kind's default.
kind_cmd() {
	local provider=${KIND_PROVIDER:-}
	if [[ -z "$provider" ]]; then
		provider=$(cluster_owner_engine)
	fi
	if [[ -n "$provider" ]]; then
		KIND_EXPERIMENTAL_PROVIDER="$provider" "${KIND:-kind}" "$@"
	else
		"${KIND:-kind}" "$@"
	fi
}

# reachable_container_engine prints container_engine only when that engine answers.
# `make clean` deletes reports and images, and only the images need an engine: with
# the podman machine stopped it skips them instead of failing the whole clean.
reachable_container_engine() {
	local engine
	engine=$(container_engine 2>/dev/null) || return 1
	"$engine" info >/dev/null 2>&1 || return 1
	printf '%s' "$engine"
}

# kube is the kubectl invocation every script uses, pinned to the Kind context.
kube() {
	"${KUBECTL:-kubectl}" --context "${KUBE_CONTEXT:-kind-kind}" "$@"
}

# images_env_path prints where the image references live: IMAGES_ENV_FILE when it
# is set, resolved against this checkout the way make resolves it, and
# reports/images.env otherwise. hack/build-images.sh, hack/build-all-images.sh,
# hack/deploy.sh, hack/clean.sh and utilities/trustyai.py all read or write that
# one file, so they have to agree on the path even when a script is invoked from
# another directory.
images_env_path() {
	local path=${IMAGES_ENV_FILE:-reports/images.env} root
	if [[ "$path" == /* ]]; then
		printf '%s' "$path"
		return
	fi
	root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
	printf '%s/%s' "$root" "$path"
}

# split_image sets IMG_NAME and IMG_TAG for "registry:port/name:tag" style refs.
split_image() {
	local ref=$1
	if [[ ${ref##*/} == *:* ]]; then
		IMG_TAG=${ref##*:}
		IMG_NAME=${ref%:*}
	else
		IMG_NAME=$ref
		IMG_TAG=latest
	fi
}

require_operator_repo() {
	[[ -n "${OPERATOR_REPO:-}" ]] ||
		die "OPERATOR_REPO must point at a trustyai-service-operator checkout"
	[[ -d "$OPERATOR_REPO" ]] || die "OPERATOR_REPO=$OPERATOR_REPO does not exist"
}

# normalize_arch maps the names the kernel and the engines report onto the GOARCH
# names the build tools use. `uname -m` and moby's Info.Architecture (the running
# platform, so x86_64 on amd64 hardware and aarch64 on arm64) say x86_64/aarch64,
# while `--platform` and BuildKit's automatic TARGETARCH say amd64/arm64: passing
# a non-GOARCH value as --build-arg TARGETARCH overrides the one BuildKit fills in
# with something no Dockerfile switches on.
normalize_arch() {
	case "$1" in
	x86_64 | x86-64) printf 'amd64' ;;
	aarch64) printf 'arm64' ;;
	*) printf '%s' "$1" ;;
	esac
}

# host_platform prints the platform images should be built for: the engine's own
# OS/arch, which is linux/arm64 on Apple Silicon and linux/amd64 on Intel/Linux,
# named the way --platform and TARGETARCH expect it.
host_platform() {
	local engine platform machine
	engine=$(container_engine)
	if [[ "$engine" == docker ]]; then
		platform=$(docker info --format '{{.OSType}}/{{.Architecture}}' 2>/dev/null || true)
		if [[ -n "$platform" ]]; then
			printf '%s/%s' "${platform%%/*}" "$(normalize_arch "${platform##*/}")"
			return
		fi
	fi
	machine=$(uname -m)
	case "$machine" in
	arm64 | aarch64 | x86_64 | amd64) printf 'linux/%s' "$(normalize_arch "$machine")" ;;
	*) die "unsupported host architecture: $machine" ;;
	esac
}

# build_image builds one image for PLATFORM (host platform when unset) and loads
# it into the engine's image store. `docker build` is buildx, and with the
# docker-container driver the image stays in the builder cache unless --load is
# passed.
build_image() {
	local image=$1 context=$2 dockerfile=$3
	shift 3
	local engine platform
	engine=$(container_engine)
	platform=${PLATFORM:-$(host_platform)}
	local -a cmd
	if [[ "$engine" == docker ]] && docker buildx version >/dev/null 2>&1; then
		cmd=(docker buildx build --load)
	else
		cmd=("$engine" build)
	fi
	info "building $image for $platform"
	"${cmd[@]}" --platform "$platform" -t "$image" -f "$dockerfile" "$@" "$context"
}

# push_image pushes a locally built image. The registry listens on localhost over
# plain HTTP, which podman has to be told explicitly.
push_image() {
	local image=$1 engine
	engine=$(container_engine)
	info "pushing $image"
	if [[ "$engine" == podman ]]; then
		"$engine" push --tls-verify=false "$image"
	else
		"$engine" push "$image"
	fi
}

# registry_name is the registry container this Kind cluster's images go to.
registry_name() { printf 'k8s-tests-registry-%s' "${KIND_CLUSTER:-trustyai}"; }

# ensure_registry starts (or reuses) a registry:2 container on the kind network,
# published on 127.0.0.1:${REGISTRY_PORT}, and sets REGISTRY_ALIAS to the name
# the nodes resolve it by.
ensure_registry() {
	local engine name alias networks mapping status
	engine=$(container_engine)
	name=$(registry_name)
	REGISTRY_PORT=${REGISTRY_PORT:-5005}
	command -v curl >/dev/null 2>&1 ||
		die "curl is required to wait for the local registry on 127.0.0.1:${REGISTRY_PORT}"
	alias="k8s-tests-reg-$(printf '%s' "${KIND_CLUSTER:-trustyai}" | cksum | awk '{print $1}')"
	REGISTRY_ALIAS=$alias
	"$engine" network inspect kind >/dev/null 2>&1 ||
		die "container network 'kind' does not exist for $engine; create the cluster with make cluster, or set KIND_PROVIDER to the engine that created it"

	if "$engine" container inspect "$name" >/dev/null 2>&1; then
		status=$("$engine" container inspect --format '{{.State.Status}}' "$name")
		[[ "$status" == running ]] || "$engine" container start "$name" >/dev/null
	else
		"$engine" image inspect docker.io/library/registry:2 >/dev/null 2>&1 ||
			"$engine" image pull docker.io/library/registry:2
		info "starting local registry $name on 127.0.0.1:${REGISTRY_PORT}"
		"$engine" run --detach --name "$name" \
			--label "io.k8s-tests.cluster=${KIND_CLUSTER:-trustyai}" \
			--publish "127.0.0.1:${REGISTRY_PORT}:5000" docker.io/library/registry:2 >/dev/null
	fi

	mapping=$("$engine" port "$name" 5000/tcp 2>/dev/null | head -n 1)
	[[ "$mapping" == "127.0.0.1:${REGISTRY_PORT}" ]] ||
		die "registry $name is published on ${mapping:-nothing}, not 127.0.0.1:${REGISTRY_PORT}; remove it or set REGISTRY_PORT"

	networks=$("$engine" container inspect --format '{{json .NetworkSettings.Networks}}' "$name")
	if [[ "$networks" != *'"kind":'* ]]; then
		"$engine" network connect --alias "$alias" kind "$name" >/dev/null
	elif [[ "$networks" != *"$alias"* ]]; then
		"$engine" network disconnect kind "$name" >/dev/null 2>&1 || true
		"$engine" network connect --alias "$alias" kind "$name" >/dev/null
	fi

	for _ in {1..30}; do
		curl --silent --fail "http://127.0.0.1:${REGISTRY_PORT}/v2/" >/dev/null 2>&1 && return 0
		sleep 1
	done
	die "local registry $name did not become reachable on 127.0.0.1:${REGISTRY_PORT}"
}

# configure_registry_mirrors points each Kind node's containerd at the registry
# for localhost:${REGISTRY_PORT}. New clusters get the certs.d config path from
# hack/kind/cluster.yaml; a cluster created before that patch has it enabled in
# place (set PATCH_CONTAINERD=false to require a recreated cluster instead).
configure_registry_mirrors() {
	local engine node hosts nodes state deadline
	engine=$(container_engine)
	hosts=$(mktemp)
	printf '[host."http://%s:5000"]\n  capabilities = ["pull", "resolve"]\n' "$REGISTRY_ALIAS" >"$hosts"
	chmod 644 "$hosts"
	nodes=$(kind_cmd get nodes --name "${KIND_CLUSTER:-trustyai}" 2>/dev/null || true)
	if [[ -z "$nodes" ]]; then
		rm -f "$hosts"
		die "Kind cluster ${KIND_CLUSTER:-trustyai} has no nodes visible to kind; set KIND_PROVIDER when the cluster runs on podman"
	fi
	while IFS= read -r node; do
		[[ -n "$node" ]] || continue
		# A node container left stopped - the podman machine was restarted and
		# `make cluster` was not run since - cannot be exec'd into, and an exec
		# failure is indistinguishable from a node whose config lacks certs.d.
		# Start it here (what hack/start-cluster.sh does for `make cluster`) so the
		# warning below only ever describes a config that really is missing.
		state=$("$engine" container inspect --format '{{.State.Status}}' "$node" 2>/dev/null || true)
		if [[ "$state" != running ]]; then
			[[ -n "$state" ]] ||
				die "kind node $node is not visible to $engine; set KIND_PROVIDER to the engine that created the cluster"
			info "starting stopped kind node $node"
			"$engine" container start "$node" >/dev/null
			deadline=$((SECONDS + 60))
			until "$engine" exec "$node" true >/dev/null 2>&1; do
				((SECONDS < deadline)) ||
					die "kind node $node did not accept exec within 60s of starting"
				sleep 2
			done
		fi
		if ! "$engine" exec "$node" grep -q 'certs.d' /etc/containerd/config.toml 2>/dev/null; then
			if [[ "${PATCH_CONTAINERD:-true}" != true ]]; then
				rm -f "$hosts"
				die "kind node $node does not load /etc/containerd/certs.d; recreate the cluster (make down && make cluster) so hack/kind/cluster.yaml's containerdConfigPatches takes effect"
			fi
			warn "kind node $node was created without the certs.d config path; enabling it in place"
			"$engine" exec "$node" sh -c 'cat >>/etc/containerd/config.toml <<EOF

[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/etc/containerd/certs.d"
EOF'
			"$engine" exec "$node" systemctl restart containerd
		fi
		"$engine" exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:${REGISTRY_PORT}"
		"$engine" cp "$hosts" "$node:/etc/containerd/certs.d/localhost:${REGISTRY_PORT}/hosts.toml" >/dev/null
		info "configured localhost:${REGISTRY_PORT} mirror on $node"
	done <<<"$nodes"
	rm -f "$hosts"
}
