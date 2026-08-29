e2e-image := "herdr-worktree-bootstrap-e2e"

test:
    uv run --group dev python -m unittest discover -s tests -v

build-e2e:
    podman build --tag {{e2e-image}} --file test/e2e/Containerfile .

test-e2e:
    podman image exists {{e2e-image}} || { echo "E2E image missing; run: just build-e2e" >&2; exit 2; }
    podman run --rm --network none \
        --mount type=bind,src="$(pwd)",dst=/work,ro \
        --mount type=bind,src="$(command -v herdr)",dst=/usr/local/bin/herdr,ro \
        --env HOME=/tmp/home \
        --env XDG_CONFIG_HOME=/tmp/config \
        --env XDG_STATE_HOME=/tmp/state \
        --env E2E_DEBUG \
        {{e2e-image}}

format:
    uv run --group dev ruff format .

format-check:
    uv run --group dev ruff format --check .

lint:
    uv run --group dev ruff check .

hooks:
    prek install

check: format-check lint test
