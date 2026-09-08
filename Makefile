.PHONY: check test lint format install

# The one gate. Each command runs bare: piping one into `tail` masks its exit code,
# which is exactly how a formatting failure once reached a published tag.
check: test lint

test:
	pytest

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

install:
	pip install -e ".[dev]"
