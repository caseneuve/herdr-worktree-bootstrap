# Worktree Bootstrap for Herdr

A [Herdr plugin](https://herdr.dev/docs/plugins/) that runs after Herdr creates
a Git worktree. It can copy selected local files into the new checkout and run
setup commands there.

The hook is deliberately post-create and asynchronous. It does not add options
to `herdr worktree`, delay the new workspace, undo a failed bootstrap, or alter
Herdr's normal worktree behavior.

## Requirements

- Herdr with plugin support (`0.7.0` or later)
- Python `3.11` or later (`tomllib` is used for configuration)
- Git for optional hook and submodule setup

## Install a release

Install from GitHub with Herdr's native plugin installer. Pin a CalVer release
tag rather than installing an unreviewed branch head:

```bash
herdr plugin install caseneuve/herdr-worktree-bootstrap --ref 2026.08.29
```

Herdr stores the installed source separately from user configuration. Create
and edit the managed `config.toml` with:

```bash
herdr plugin config-dir caseneuve.herdr-worktree-bootstrap
mkdir -p "$(herdr plugin config-dir caseneuve.herdr-worktree-bootstrap)"
$EDITOR "$(herdr plugin config-dir caseneuve.herdr-worktree-bootstrap)/config.toml"
```

### Releases

Plugin releases use CalVer tags and manifest versions in `YYYY.MM.DD` form. A
second release on the same day appends `.N`, for example `2026.08.29.1`. The
manifest's `version` and its Git tag must match. `min_herdr_version` remains a
semantic version because Herdr validates it as one.

## Configuration

Each `[[worktree]]` block is an independent, repository-specific rule. `repo`
is required; there is no global or catch-all rule. Multiple blocks may match
the same repository: all copies run in declaration order, then all commands
do.

```toml
[[worktree]]
repo = "~/src/example-project"
paths = [".env", ".pi"]
git-hooks = true
git-submodules = true
commands = [
  ["git", "submodule", "update", "--init", "--recursive"],
]

[[worktree]]
repo = "~/src/another-project"
paths = ["~/private/another-project.env"]
commands = [
  ["mise", "install"],
]
```

`repo` is expanded with `~` and compared to Herdr's canonical source parent
checkout path. If no rule matches, the hook logs that it has nothing to do and
succeeds.

### Copy paths

`paths` is a one-way seed list, not an `rsync --delete` mirror.

| Configured path | Source | Destination |
| --- | --- | --- |
| `.env` | `<parent-repo>/.env` | `<new-worktree>/.env` |
| `.pi` | `<parent-repo>/.pi` | `<new-worktree>/.pi` |
| `/private/project/.env` | that absolute path | `<new-worktree>/.env` |
| `~/private/project.env` | expanded home-relative path | `<new-worktree>/project.env` |

Relative paths must remain below the parent repository root; `..` paths are
rejected. Files copy as files, directories copy recursively, and symbolic
links are preserved as links.

For every configured path:

- a missing source is logged and skipped;
- an existing destination is logged as a failure and is never overwritten;
- a copy failure is logged and later paths and commands still run.

A fresh Git worktree can still contain a tracked file with a configured name.
That is an existing destination collision, so the plugin leaves it untouched.

### Git setup

#### Hooks

Set `git-hooks = true` to configure the new worktree's Git hooks before custom
commands run. The plugin enables the repository-wide `extensions.worktreeConfig`
setting, resolves the new worktree's Git directory, then writes this
worktree-specific setting:

```text
core.hooksPath = <new-worktree-git-dir>/hooks
```

This does not install a hook manager or generate hooks. Keep those
project-specific steps in `commands`; for example, `prek install` can target
the resolved Git directory with an explicit shell command.

#### Submodules

Set `git-submodules = true` to run this command before custom commands:

```bash
git submodule update --init --recursive
```

It initializes nested submodules and checks them out at the exact commits
pinned by the parent repository, cloning or fetching required objects as
needed. It deliberately does not use `--remote`, so it never advances a
submodule to its remote branch head or dirties the parent worktree.

### Commands

Each `commands` item is an argv array, never an implicitly evaluated shell
string. Commands run from the new worktree root after all configured copies and
optional Git hook setup, in declaration order.

When a command needs shell expansion or command substitution, make the shell
explicit in its argv array:

```toml
["sh", "-c", 'npm exec -- prek install --git-dir "$(git rev-parse --git-dir)" --prepare-hooks']
```

Every command is attempted even if an earlier copy or command failed. A command
that cannot start or exits non-zero is logged as a failure. Once every step has
been attempted, the plugin exits non-zero if any copy or command failed, so
Herdr marks the event-hook run as failed in its plugin log.

## Logs

Inspect completed hooks with:

```bash
herdr plugin log list --plugin caseneuve.herdr-worktree-bootstrap
```

The log includes the copied/skipped paths, each argv command, and command
stdout or stderr captured by Herdr.

## Development and test

The plugin has no runtime Python dependencies. Development tools are locked in
`uv.lock`. With `uv` and `prek` available on `PATH`, create the environment
and install the Prek hook shims once per checkout:

```bash
uv sync --group dev
prek install
```

Prek runs `ruff format` and `ruff check` on changed Python files. Ruff is
configured for a 99-column line length; a formatter change must be staged
before committing.

Run the full local suite from the repository root:

```bash
just check
```

Individual commands are also available:

```bash
just format        # apply Ruff formatting
just lint          # run Ruff checks
just test          # run the focused unit suite
```

## Development: live Herdr test

Unit tests do not start Herdr. Use this route when an agent needs to validate
that a local change runs from a real `worktree.created` event.

This flow must run inside a disposable Herdr session (`HERDR_ENV=1`). It opens
a temporary workspace in that session. Do not target a real repository.

Herdr keeps one global registration per plugin ID. Linking a local checkout
with the released ID would replace the GitHub-installed release, so this route
uses a temporary development ID and separate managed configuration.

1. Create a temporary plugin worktree and give only that checkout a development
   identity:

   ```bash
   plugin_repo="$(git rev-parse --show-toplevel)"
   dev_root="$(mktemp -d)"
   rmdir "$dev_root"
   git -C "$plugin_repo" worktree add --detach "$dev_root" HEAD

   dev_id="caseneuve.herdr-worktree-bootstrap.dev-$$"
   python3 - "$dev_root/herdr-plugin.toml" "$dev_id" <<'PY'
   from pathlib import Path
   import re
   import sys

   manifest = Path(sys.argv[1])
   plugin_id = sys.argv[2]
   manifest.write_text(
       re.sub(r'^id = ".*"$', f'id = "{plugin_id}"', manifest.read_text(), flags=re.MULTILINE)
   )
   PY
   herdr plugin link "$dev_root"
   ```

2. Create an isolated Git fixture, an ignored file to seed, and development
   configuration that matches only that fixture:

   ```bash
   fixture_root="$(mktemp -d)"
   git -C "$fixture_root" init -q
   git -C "$fixture_root" config user.email test@example.invalid
   git -C "$fixture_root" config user.name "Worktree Bootstrap Test"
   printf 'fixture\n' > "$fixture_root/README.md"
   printf '.worktree-bootstrap-private\n' > "$fixture_root/.gitignore"
   git -C "$fixture_root" add README.md .gitignore
   git -C "$fixture_root" commit -qm fixture
   printf 'seed me\n' > "$fixture_root/.worktree-bootstrap-private"

   config_dir="$(herdr plugin config-dir "$dev_id")"
   mkdir -p "$config_dir"
   cat > "$config_dir/config.toml" <<EOF
   [[worktree]]
   repo = "$fixture_root"
   paths = [".worktree-bootstrap-private"]
   commands = [
     ["python3", "-c", "from pathlib import Path; Path('.worktree-bootstrap-command-ran').touch()"],
   ]
   EOF
   ```

3. Create the fixture worktree through Herdr, then verify the hook result and
   its log. The hook is asynchronous, so wait until the two files exist before
   asserting success.

   ```bash
   fixture_worktree="$(mktemp -d)"
   rmdir "$fixture_worktree"
   worktree_result="$(herdr worktree create \
     --cwd "$fixture_root" \
     --branch herdr-bootstrap-dev \
     --path "$fixture_worktree" \
     --no-focus \
     --trust-repository)"
   workspace_id="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["workspace"]["workspace_id"])' <<<"$worktree_result")"

   python3 - "$fixture_worktree" <<'PY'
   from pathlib import Path
   import sys
   import time

   worktree = Path(sys.argv[1])
   expected = [
       worktree / ".worktree-bootstrap-private",
       worktree / ".worktree-bootstrap-command-ran",
   ]
   deadline = time.monotonic() + 10
   while time.monotonic() < deadline:
       if all(path.is_file() for path in expected):
           break
       time.sleep(0.1)
   else:
       raise SystemExit(f"timed out waiting for {expected}")
   PY
   herdr plugin log list --plugin "$dev_id"
   ```

4. Close the disposable Herdr workspace, then unregister and remove all
   temporary resources. Keep the GitHub-installed release registered.

   ```bash
   herdr workspace close "$workspace_id"
   herdr plugin unlink "$dev_id"
   rm -rf "$(herdr plugin config-dir "$dev_id")"
   git -C "$plugin_repo" worktree remove --force "$dev_root"
   git -C "$fixture_root" worktree remove --force "$fixture_worktree"
   rm -rf "$fixture_root"
   ```

The development manifest change is confined to the temporary Git worktree; do
not commit it.
