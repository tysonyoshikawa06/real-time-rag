.PHONY: up down smoke smoke-db logs produce watch inject-status inject-clear

# --env-file .env is required because Docker Compose v5+ no longer auto-reads
# .env from the working directory. This loads POSTGRES_PASSWORD (and any future
# secrets) into variable interpolation for the compose file.
COMPOSE = docker compose --env-file .env -f infra/docker-compose.yml

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

smoke:
	uv run python infra/smoke_test.py

smoke-db:
	uv run python infra/smoke_test_postgres.py

logs:
	$(COMPOSE) logs -f

produce:
	uv run python -m producer.main

watch:
	uv run python -m producer.watch

# Inject incidents: uv run python -m producer.inject gateway_degradation --gateway stripe-proxy --duration 2m
inject-status:
	uv run python -m producer.inject status

inject-clear:
	uv run python -m producer.inject clear
