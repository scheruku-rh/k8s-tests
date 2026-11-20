#!/usr/bin/env bash
# Start the Kind node containers (and the local registry) when the podman machine
# was stopped and started again. `make cluster` calls this before it waits for
# the nodes, so an existing cluster survives a machine restart.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

engine=$(container_engine)
cluster=${KIND_CLUSTER:-trustyai}

while IFS= read -r node; do
	[[ -n "$node" ]] || continue
	state=$("$engine" container inspect --format '{{.State.Status}}' "$node" 2>/dev/null || true)
	if [[ "$state" != running ]]; then
		info "starting Kind node $node"
		"$engine" container start "$node" >/dev/null
	fi
done < <("$engine" ps -a --filter "label=io.x-k8s.kind.cluster=$cluster" --format '{{.Names}}')

registry=$(registry_name)
if "$engine" container inspect "$registry" >/dev/null 2>&1; then
	state=$("$engine" container inspect --format '{{.State.Status}}' "$registry")
	if [[ "$state" != running ]]; then
		info "starting local registry $registry"
		"$engine" container start "$registry" >/dev/null
	fi
fi

# A just-started node needs a moment before the API server answers; without this
# the kubectl wait in `make cluster` races it and fails with "the server doesn't
# have a resource type". TIMEOUT is the deadline that wait uses next, so allow
# the same here: its single-unit h/m/s form (and a bare number of seconds) becomes
# seconds, and anything else - a compound 1h30m, say - falls back to the default
# instead of feeding the arithmetic a value it cannot take.
timeout=${TIMEOUT:-300s}
seconds=""
if [[ "$timeout" =~ ^[0-9]+$ ]]; then
	seconds=$timeout
elif [[ "$timeout" =~ ^([0-9]+)([smh])$ ]]; then
	case "${BASH_REMATCH[2]}" in
	s) seconds=${BASH_REMATCH[1]} ;;
	m) seconds=$((BASH_REMATCH[1] * 60)) ;;
	h) seconds=$((BASH_REMATCH[1] * 3600)) ;;
	esac
fi
timeout=${seconds:-300}
context=${KUBE_CONTEXT:-kind-$cluster}
deadline=$((SECONDS + timeout))
while ((SECONDS < deadline)); do
	if "${KUBECTL:-kubectl}" --context "$context" get --raw=/readyz >/dev/null 2>&1; then
		exit 0
	fi
	sleep 2
done
die "the API server for $context did not become reachable within ${timeout}s"
