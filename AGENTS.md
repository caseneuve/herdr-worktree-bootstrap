# AGENTS.md

## Scope

This repository is a standalone Herdr plugin. Keep user configuration outside
of the repository; Herdr supplies its location through
`HERDR_PLUGIN_CONFIG_DIR`.

## Development and validation

Use the locked UV development environment, then install the repository hooks:

```bash
uv sync --group dev
prek install
```

Run every check before committing:

```bash
just check
```

Ruff formats Python at 99 columns and checks it through Prek pre-commit hooks.
Run `just build-e2e` once (or after changing its Containerfile), then `just
test-e2e` to exercise a real `worktree.created` event. It requires Podman and a
local `herdr` binary; all fixture setup and cleanup stays inside the
short-lived container. See `README.md` for production installation.

## Releases

Use CalVer release tags and manifest versions: `YYYY.MM.DD`, with `.N` for an
additional release on the same day. Keep `min_herdr_version` semantic.
