.PHONY: install build test lint clean

install:
	uv sync

build:
	uv build

test:
	uv run pytest -v

lint:
	uv tool run ruff check --fix src/ tests/

clean:
	rm -rf dist/ build/ *.egg-info src/*.egg-info .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
