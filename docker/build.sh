#!/usr/bin/env bash
# Build the bamboo-mcp-services image (S-24).
#
#   docker/build.sh                # build for linux/amd64 and load it locally
#   docker/build.sh --push         # build and push to $IMAGE
#
# The platform is fixed to linux/amd64 because the target hosts are x86_64; on
# an Apple Silicon Mac a plain 'docker build' would produce arm64.  Override
# with PLATFORM=linux/arm64 for a local test image only.
#
# Environment:
#   IMAGE        repository to tag (default: bamboo-mcp-services);
#                e.g. registry.cern.ch/<project>/bamboo-mcp-services
#   PLATFORM     target platform (default: linux/amd64)
#   ALLOW_DIRTY  set to 1 to build with uncommitted changes to tracked files
#
# The tag is <version>-<12-char commit>, and the commit is also recorded in
# the image's labels and in /opt/bamboo/build-info.json.  Extra arguments go
# to 'docker buildx build' unchanged.
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-bamboo-mcp-services}"
PLATFORM="${PLATFORM:-linux/amd64}"

if [[ ! -f docker/model-manifest.sha256 ]]; then
    echo "docker/model-manifest.sha256 is missing: generate it on aipanda033 (README-container.md)." >&2
    exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" && "${ALLOW_DIRTY:-0}" != 1 ]]; then
    echo "Tracked files have uncommitted changes; commit them, or set ALLOW_DIRTY=1." >&2
    exit 1
fi

commit="$(git rev-parse HEAD)"
short="$(git rev-parse --short=12 HEAD)"
version="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)"
if [[ -z "${version}" ]]; then
    echo "Cannot read the version from pyproject.toml." >&2
    exit 1
fi
if [[ "${ALLOW_DIRTY:-0}" == 1 && -n "$(git status --porcelain --untracked-files=no)" ]]; then
    short="${short}-dirty"
fi
tag="${IMAGE}:${version}-${short}"

output=(--load)
for arg in "$@"; do
    if [[ "${arg}" == "--push" || "${arg}" == --output* ]]; then
        output=()
    fi
done

set -x
docker buildx build \
    --platform "${PLATFORM}" \
    --build-arg VERSION="${version}" \
    --build-arg GIT_COMMIT="${commit}" \
    --tag "${tag}" \
    ${output[@]+"${output[@]}"} \
    "$@" \
    .
