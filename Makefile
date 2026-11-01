.DEFAULT_GOAL := help
.PHONY: help bootstrap dev infra-up infra-down migrate revision test test-api test-web lint lint-api lint-web format verify clean

PY := python
COMPOSE := docker compose -f infra/compose/docker-compose.yml

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Install everything and start local infrastructure
	cp -n .env.example .env || true
	$(PY) scripts/api.py install
	npm install
	$(COMPOSE) up -d
	$(PY) scripts/api.py migrate
	@echo "ready - run 'make dev'"

infra-up: ## Start Postgres + Redis
	$(COMPOSE) up -d

infra-down: ## Stop Postgres + Redis
	$(COMPOSE) down

migrate: ## Apply database migrations (as the schema owner)
	$(PY) scripts/api.py migrate

revision: ## Autogenerate a migration - always read what it produced
	$(PY) scripts/api.py revision $(m)

dev: ## Run API and web together (two terminals is usually nicer)
	$(PY) scripts/api.py dev & npm run dev

test: test-api test-web ## Run every test suite

test-api: ## pytest
	$(PY) scripts/api.py test

test-web: ## vitest
	npm run test

lint: lint-api lint-web ## Lint everything

lint-api: ## ruff + mypy + import-linter
	$(PY) scripts/api.py lint

lint-web: ## eslint + tsc
	npm run lint && npm run typecheck

format: ## Auto-format the API
	$(PY) scripts/api.py format

verify: lint test ## What CI runs

clean: ## Remove caches and build output
	rm -rf apps/api/.venv apps/api/.pytest_cache apps/api/.mypy_cache apps/api/.ruff_cache
	rm -rf apps/web/.next apps/web/node_modules node_modules
