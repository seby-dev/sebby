.PHONY: check
check:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy --strict src tests
	uv run pytest -q
