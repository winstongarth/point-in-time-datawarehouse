.PHONY: up down logs migrate downgrade lint format typecheck test check

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f postgres

migrate:
	uv run alembic upgrade head

downgrade:
	uv run alembic downgrade -1

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy --strict src

test:
	uv run pytest

check: lint typecheck test
