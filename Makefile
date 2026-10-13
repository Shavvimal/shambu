.PHONY: install lint format typecheck test check

install:
	uv sync --group dev

lint:
	uv run ruff check src tests

format:
	uv run ruff check --fix src tests
	uv run ruff format src tests

typecheck:
	uv run mypy

test:
	uv run pytest -q

check: lint typecheck test
