#!/usr/bin/env bash
# Runs only inside the Podman E2E container. Podman --rm owns all cleanup.

set -euo pipefail

readonly PLUGIN_ID="caseneuve.herdr-worktree-bootstrap.e2e"
readonly WAIT_SECONDS=10

server_pid=""

debug() {
    [[ "${E2E_DEBUG:-}" == "1" ]] || return 0
    printf 'live Herdr E2E [debug]: %s\n' "$*" >&2
}

fail() {
    printf 'live Herdr E2E: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    local status=$?
    trap - EXIT
    set +e
    if [[ -n "$server_pid" ]]; then
        herdr server stop >/dev/null 2>&1 || kill "$server_pid" >/dev/null 2>&1
        wait "$server_pid" >/dev/null 2>&1
    fi
    exit "$status"
}

wait_for_server() {
    local deadline=$((SECONDS + WAIT_SECONDS))
    while ((SECONDS < deadline)); do
        if herdr status server --json >/tmp/server-status.json 2>/dev/null \
            && python3 -c '
import json
import sys
from pathlib import Path

sys.exit(not json.loads(Path("/tmp/server-status.json").read_text())["running"])
'; then
            return 0
        fi
        sleep 0.1
    done
    fail "Herdr headless server did not become ready"
}

wait_for_bootstrap() {
    local deadline=$((SECONDS + WAIT_SECONDS))
    local logs=""
    while ((SECONDS < deadline)); do
        if [[ -f /tmp/fixture-worktree/.worktree-bootstrap-private \
            && -f /tmp/fixture-worktree/.worktree-bootstrap-command-ran ]]; then
            logs="$(herdr plugin log list --plugin "$PLUGIN_ID" --limit 10)"
            if printf '%s' "$logs" | python3 -c '
import json
import sys

logs = json.load(sys.stdin)["result"]["logs"]


def notification_was_suppressed_headlessly(log):
    prefix = "[worktree-bootstrap] completion notification response: "
    for line in log.get("stdout", "").splitlines():
        if not line.startswith(prefix):
            continue
        try:
            result = json.loads(line.removeprefix(prefix))["result"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if not isinstance(result, dict):
            continue
        return (
            result.get("type") == "notification_show"
            and result.get("shown") is False
            and result.get("reason") == "no_foreground_client"
        )
    return False


sys.exit(not any(
    log.get("event") == "worktree.created"
    and log.get("status") == "succeeded"
    and notification_was_suppressed_headlessly(log)
    for log in logs
))
'; then
                debug "seeded file content: $(cat /tmp/fixture-worktree/.worktree-bootstrap-private)"
                debug "command marker exists: /tmp/fixture-worktree/.worktree-bootstrap-command-ran"
                if [[ "${E2E_DEBUG:-}" == "1" ]]; then
                    printf 'live Herdr E2E [debug]: plugin log: %s\n' "$logs" >&2
                fi
                return 0
            fi
        fi
        sleep 0.1
    done
    printf 'live Herdr E2E: plugin logs at timeout: %s\n' "${logs:-<unavailable>}" >&2
    fail "timed out waiting for the worktree bootstrap hook (expected no_foreground_client)"
}

for command in git herdr python3 tar; do
    command -v "$command" >/dev/null 2>&1 || fail "required command is unavailable: $command"
done

trap cleanup EXIT

# Exercise the notification API without an attached client: delivery must be
# reported as no_foreground_client rather than falsely claimed as shown.
mkdir -p "$XDG_CONFIG_HOME/herdr"
cat >"$XDG_CONFIG_HOME/herdr/config.toml" <<'EOF'
[ui.toast]
delivery = "herdr"
EOF

debug "using $(herdr --version) with isolated HOME and XDG directories"

# Copy the current checkout, including uncommitted source changes, into the
# container's temporary filesystem. The host checkout stays read-only.
mkdir -p /tmp/plugin
tar \
    --exclude-vcs \
    --exclude=.venv \
    --exclude=.reviews \
    --exclude=.ruff_cache \
    --exclude=__pycache__ \
    --exclude=tests/__pycache__ \
    -C /work \
    -cf - . \
    | tar -C /tmp/plugin -xf -
debug "copied the read-only checkout to /tmp/plugin"
python3 - /tmp/plugin/herdr-plugin.toml "$PLUGIN_ID" <<'PY'
from pathlib import Path
import re
import sys

manifest = Path(sys.argv[1])
plugin_id = sys.argv[2]
manifest.write_text(
    re.sub(r'^id = ".*"$', f'id = "{plugin_id}"', manifest.read_text(), flags=re.MULTILINE)
)
PY

herdr server >/tmp/herdr-server.log 2>&1 &
server_pid=$!
wait_for_server
debug "headless server ready: $(cat /tmp/server-status.json)"

herdr plugin link /tmp/plugin >/tmp/plugin-link.json
debug "temporary plugin link: $(cat /tmp/plugin-link.json)"
config_dir="$(herdr plugin config-dir "$PLUGIN_ID")"
mkdir -p "$config_dir"

mkdir -p /tmp/fixture
git -C /tmp/fixture init -q
git -C /tmp/fixture config user.email test@example.invalid
git -C /tmp/fixture config user.name "Worktree Bootstrap E2E"
printf 'fixture\n' >/tmp/fixture/README.md
printf '.worktree-bootstrap-private\n' >/tmp/fixture/.gitignore
git -C /tmp/fixture add README.md .gitignore
git -C /tmp/fixture commit -qm fixture
printf 'seed me\n' >/tmp/fixture/.worktree-bootstrap-private

cat >"$config_dir/config.toml" <<'EOF'
[[worktree]]
repo = "/tmp/fixture"
paths = [".worktree-bootstrap-private"]
commands = [
  ["python3", "-c", "from pathlib import Path; Path('.worktree-bootstrap-command-ran').touch()"],
]
EOF

herdr worktree create \
    --cwd /tmp/fixture \
    --branch herdr-bootstrap-e2e \
    --path /tmp/fixture-worktree \
    --no-focus \
    >/tmp/worktree-create.json
debug "worktree create result: $(cat /tmp/worktree-create.json)"

wait_for_bootstrap
printf 'live Herdr E2E: passed\n'
