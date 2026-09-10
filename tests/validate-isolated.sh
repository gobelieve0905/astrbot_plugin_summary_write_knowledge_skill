#!/usr/bin/env bash
set -euo pipefail
# Dedicated temporary container: no network, credentials, production mounts or adapter.
server=${1:?Usage: bash tests/validate-isolated.sh SSH_HOST}
plugin_root=$(cd "$(dirname "$0")/.." && pwd)
stage=$(ssh "$server" 'mktemp -d /var/tmp/knowledge-plugin-test.XXXXXX')
cleanup() { ssh "$server" "sudo rm -rf -- '$stage'" >/dev/null; }
trap cleanup EXIT
cd "$plugin_root"
COPYFILE_DISABLE=1 tar --no-xattrs -cf - main.py models.py prompts.py backend.py store.py service.py tests | ssh "$server" "tar -xf - -C '$stage'"
ssh "$server" sudo bash -s -- "$stage" <<'REMOTE'
set -euo pipefail
stage=$1
image=$(docker inspect --format '{{.Image}}' astrbot-astrbot-1)
for test_entry in unit integration; do
  if [ "$test_entry" = unit ]; then
    args=(-m unittest discover -s /test/tests -v)
  else
    args=(/test/tests/integration_offline.py)
  fi
  docker run --rm --network none --read-only --memory=512m --memory-swap=768m \
    --cpus=1 --pids-limit=128 --cgroup-parent=build.slice \
    --tmpfs /tmp:rw,size=128m -e ASTRBOT_ROOT=/tmp/check -e HOME=/tmp/check \
    -e PYTHONPATH=/opt/astrbot:/test -v "$stage:/test:ro" \
    "$image" -B "${args[@]}" > "$stage/$test_entry.log" 2>&1 || {
      tail -n 90 "$stage/$test_entry.log"
      exit 1
    }
  tail -n 6 "$stage/$test_entry.log"
done
REMOTE
