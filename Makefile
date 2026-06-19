.PHONY: up down smoke logs

up:
	docker compose -f infra/docker-compose.yml up -d

down:
	docker compose -f infra/docker-compose.yml down

smoke:
	uv run python infra/smoke_test.py

logs:
	docker compose -f infra/docker-compose.yml logs -f
