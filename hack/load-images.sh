#!/usr/bin/env bash
# Load the built operator images into the Kind cluster.
#
# `kind load docker-image` shells out to `docker` even when Kind itself runs on
# the podman provider, so export the images with whichever engine built them and
# hand Kind the archive instead. Each image gets its own archive: `podman save`
# collapses several images into a single manifest entry, which would load the
# first image under every tag.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

engine=$(container_engine)
name="${KIND_CLUSTER:-trustyai}"

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

for image in "$WORKLOAD_IMG" "$MODULE_IMG"; do
	archive="$workdir/image-$(echo "$image" | tr "/:" "--").tar"
	info "exporting $image with $engine"
	"$engine" save -o "$archive" "$image"
	info "loading $image into Kind cluster $name"
	kind_cmd load image-archive --name "$name" "$archive"
done

info "loaded both operator images into $name"
