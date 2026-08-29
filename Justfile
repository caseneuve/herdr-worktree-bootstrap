test:
    uv run --group dev python -m unittest discover -s tests -v

format:
    uv run --group dev ruff format .

format-check:
    uv run --group dev ruff format --check .

lint:
    uv run --group dev ruff check .

hooks:
    prek install

check: format-check lint test
