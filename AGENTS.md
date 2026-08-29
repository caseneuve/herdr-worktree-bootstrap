# AGENTS.md

## Scope

This repository is a standalone Herdr plugin. Keep user configuration outside
of the repository; Herdr supplies its location through
`HERDR_PLUGIN_CONFIG_DIR`.

## Validation

Run the focused suite before committing:

```bash
just test
```

See `README.md` for production installation and the isolated live-development
test route. Do not replace a GitHub-installed release with a local link under
the production plugin ID.

## Releases

Use CalVer release tags and manifest versions: `YYYY.MM.DD`, with `.N` for an
additional release on the same day. Keep `min_herdr_version` semantic.
