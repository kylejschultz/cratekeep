#!/bin/sh
set -eu

image=${1:?usage: tests/container_smoke.sh IMAGE [EVIDENCE_DIR]}
evidence=${2:-}
fixture_root=$(mktemp -d "${TMPDIR:-/tmp}/cratekeep-container-smoke.XXXXXX")
container_name="cratekeep-smoke-$$"
python_bin=${PYTHON:-python3}
repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

cleanup() {
  if [ -n "$evidence" ]; then
    mkdir -p "$evidence"
    docker logs "$container_name" > "$evidence/container.log" 2>&1 || true
  fi
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  rm -rf "$fixture_root"
}
trap cleanup EXIT INT TERM

source_commit=$(git -C "$repo_root" rev-parse --verify HEAD)
source_tree=$(git -C "$repo_root" rev-parse --verify 'HEAD^{tree}')
source_set_definition="recursive Git tree for every tracked source/test/doc path; excludes untracked evidence"

mkdir -p "$fixture_root/state" "$fixture_root/fixtures"
chmod 0777 "$fixture_root/state" "$fixture_root/fixtures"
if [ -z "$evidence" ]; then
  evidence="$fixture_root/evidence"
fi
mkdir -p "$evidence"

docker image inspect "$image" \
  --format '{{json .Id}} {{json .RepoDigests}} {{json .Os}}/{{json .Architecture}}' > "$evidence/image-identity.txt"
docker run -d --name "$container_name" \
  --user "$(id -u):$(id -g)" \
  -e PYTHONPATH=/app:/smoke \
  -p 127.0.0.1::8788 \
  -v "$fixture_root/state:/data/config" \
  -v "$fixture_root/fixtures:/fixtures" \
  -v "$repo_root/tests/runtime_app.py:/smoke/runtime_app.py:ro" \
  "$image" gunicorn --bind=0.0.0.0:8788 --workers=1 --threads=4 \
  --access-logfile=- --access-logformat='%(r)s %(s)s %(L)s'  'runtime_app:create_app()' >/dev/null
port=$(docker port "$container_name" 8788/tcp | sed 's/.*://')
base_url="http://127.0.0.1:$port"

attempt=0
until "$python_bin" -c "import urllib.request; urllib.request.urlopen('$base_url/healthz', timeout=2)" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker logs "$container_name" >&2
    exit 1
  fi
  sleep 1
done

PYTHONPATH="$repo_root:$repo_root/tests${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" tests/react_runtime_smoke.py \
  --base-url "$base_url" \
  --fixture-root "$fixture_root/fixtures" \
  --app-inbox /fixtures/inbox \
  --app-library /fixtures/library \
  --state-path "$fixture_root/state" \
  --evidence-dir "$evidence" \
  --source-commit "$source_commit" \
  --source-tree "$source_tree" \
  --source-set-definition "$source_set_definition"

# Verify diagnostic records reach Docker's ordinary log stream, not just Python handlers.
docker logs "$container_name" > "$evidence/container.log" 2>&1
"$python_bin" - "$evidence/container.log" <<'PYLOG'
import json
from pathlib import Path
import sys
records = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line.startswith('{"event":"matching.')]
completed = [row for row in records if row['event'] == 'matching.completed']
assert {'inbox', 'library'} <= {row['source'] for row in completed}, 'Missing container match diagnostics'
assert any(row['candidates'] and 'penalties' in row['candidates'][0] for row in completed)
print(f"Container match diagnostics verified: {len(records)} records")
PYLOG
