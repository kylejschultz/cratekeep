#!/bin/sh
# Explicitly invoked local candidate publication; does not deploy or publish releases.
set -eu

registry=${CANDIDATE_REGISTRY:?Set CANDIDATE_REGISTRY to your private registry hostname}
evidence=${1:?usage: CANDIDATE_REGISTRY=registry.example.com scripts/publish-candidate.sh /absolute/evidence/path}
case "$registry" in
  ''|*[!a-z0-9.:-]*) echo 'CANDIDATE_REGISTRY must be a hostname with an optional port, without a URL scheme or path.' >&2; exit 1 ;;
esac
case "$evidence" in
  /*) ;;
  *) echo 'Evidence path must be absolute and outside the checkout.' >&2; exit 1 ;;
esac
repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$repo_root"
case "$evidence/" in
  "$repo_root/"*) echo 'Keep evidence outside the checkout.' >&2; exit 1 ;;
esac
if [ -n "$(git status --porcelain)" ]; then
  echo 'Commit or preserve local changes before publishing a candidate with a source SHA.' >&2
  exit 1
fi
source_sha=$(git rev-parse HEAD)
image="$registry/cratekeep:sha-$source_sha"
python_bin=${PYTHON:-$repo_root/.venv/bin/python}
mkdir -p "$evidence"

# The Dockerfile copies these tracked bundles; it does not build them.
npm run build
git diff --exit-code -- beets_mvp/static/ui

docker build --platform linux/amd64 --build-arg "BUILD_SHA=$source_sha" \
  --label "org.opencontainers.image.revision=$source_sha" -t "$image" .
actual_platform=$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')
[ "$actual_platform" = linux/amd64 ] || { echo 'Candidate platform does not match Unraid.' >&2; exit 1; }
docker image inspect "$image" --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -Fx "BUILD_SHA=$source_sha" >/dev/null
PYTHON="$python_bin" tests/container_smoke.sh "$image" "$evidence/smoke"
# Do not publish a moving tag after a failed smoke or a concurrent source change.
[ "$(git rev-parse HEAD)" = "$source_sha" ] && [ -z "$(git status --porcelain)" ] \
  || { echo 'Source changed during validation; candidate was not published.' >&2; exit 1; }

docker push "$image" > "$evidence/push-sha.log" 2>&1
docker tag "$image" "$registry/cratekeep:candidate"
docker push "$registry/cratekeep:candidate" > "$evidence/push-candidate.log" 2>&1
docker image inspect "$image" --format '{{json .RepoDigests}}' > "$evidence/registry-digests.json"
printf 'Published %s and %s/cratekeep:candidate\nSource: %s\nPlatform: linux/amd64\n' "$image" "$registry" "$source_sha"
