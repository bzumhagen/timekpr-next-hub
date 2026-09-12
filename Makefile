# timekpr-next-hub developer commands. See README.md for the full guide.
#
# Safe under parallel make (`make -j12`, this repo's fish alias): every
# Python-touching target depends on the venv-sync stamp file below rather
# than shelling out to `uv sync` directly, so concurrent targets don't race
# to write the same venv. Ordering elsewhere comes only from prerequisites,
# never from the order targets happen to be listed in.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DELETE_ON_ERROR:
MAKEFLAGS += --no-builtin-rules
.DEFAULT_GOAL := help

UV ?= uv
COMPOSE ?= docker compose
IMAGE ?= timekpr-hub:dev
PYTEST_ARGS ?=

DEV_COMPOSE := $(COMPOSE) -f deploy/compose.dev.yml
PROD_COMPOSE := $(COMPOSE) -f deploy/docker-compose.yml --env-file deploy/.env

DEV_DATABASE_URL ?= postgresql+asyncpg://timekpr_hub:timekpr_hub@127.0.0.1:55432/timekpr_hub_dev
TEST_DATABASE_URL ?= postgresql+asyncpg://timekpr_hub:timekpr_hub@127.0.0.1:55432/timekpr_hub_test

.PHONY: help install lock upgrade fmt lint typecheck test test-db test-e2e test-all check \
        db-up db-down db-shell migrate migrate-test revision dev \
        build build-wheels image deploy-up deploy-down deploy-logs clean distclean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- install --

# Real file target (not .PHONY) keyed on every file that can change what
# `uv sync` installs, so parallel targets share one sync instead of racing,
# and a no-op `make check` doesn't re-sync at all.
.venv/.synced: pyproject.toml uv.lock core/pyproject.toml hub/pyproject.toml agent/pyproject.toml
	$(UV) sync --all-packages
	@touch $@

install: .venv/.synced ## Sync the uv workspace venv (core + hub + agent + dev deps)

lock: ## Re-lock dependencies (respects existing pins)
	$(UV) lock

upgrade: ## Re-lock dependencies, upgrading everything to latest allowed
	$(UV) lock --upgrade

# -------------------------------------------------------------- lint/type --

fmt: .venv/.synced ## Auto-format and auto-fix lint issues
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint: .venv/.synced ## Check formatting and lint rules (no changes made)
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck: .venv/.synced ## Run mypy across core/hub/agent
	$(UV) run mypy core hub agent

# ------------------------------------------------------------------ tests --

test: .venv/.synced ## Run tests that need no external services (unit/property/simulation)
	$(UV) run pytest -m "not db" $(PYTEST_ARGS)

test-db: .venv/.synced db-up migrate-test ## Run only the Postgres-backed tests (starts/migrates the dev DB first)
	TEST_DATABASE_URL="$(TEST_DATABASE_URL)" TIMEKPR_HUB_REQUIRE_DB=1 \
		$(UV) run pytest -m db $(PYTEST_ARGS)

test-e2e: .venv/.synced db-up migrate-test ## Run only the end-to-end acceptance/chaos tests (real agent tick loop + real hub + real Postgres)
	TEST_DATABASE_URL="$(TEST_DATABASE_URL)" TIMEKPR_HUB_REQUIRE_DB=1 \
		$(UV) run pytest tests/e2e $(PYTEST_ARGS)

test-all: .venv/.synced db-up migrate-test ## Run the full suite in one process, DB required (this is CI's gate)
	TEST_DATABASE_URL="$(TEST_DATABASE_URL)" TIMEKPR_HUB_REQUIRE_DB=1 \
		$(UV) run pytest $(PYTEST_ARGS)

check: lint typecheck test ## Everything CI runs before a merge (lint + typecheck + non-DB tests)

# --------------------------------------------------------------------- db --

db-up: ## Start the local dev/test Postgres (127.0.0.1:55432)
	$(DEV_COMPOSE) up -d
	@echo "waiting for postgres..."
	@until $(DEV_COMPOSE) exec -T postgres pg_isready -U timekpr_hub >/dev/null 2>&1; do sleep 1; done

db-down: ## Stop and remove the local dev/test Postgres (tmpfs data, so this also wipes it)
	$(DEV_COMPOSE) down

db-shell: ## psql into the dev database
	$(DEV_COMPOSE) exec postgres psql -U timekpr_hub -d timekpr_hub_dev

migrate: .venv/.synced db-up ## Apply Alembic migrations to the dev database
	DATABASE_URL="$(DEV_DATABASE_URL)" $(UV) run alembic -c hub/alembic.ini upgrade head

migrate-test: .venv/.synced db-up ## Apply Alembic migrations to the test database
	DATABASE_URL="$(TEST_DATABASE_URL)" $(UV) run alembic -c hub/alembic.ini upgrade head

revision: .venv/.synced db-up ## Autogenerate a migration: make revision MSG="add foo column"
	@if [ -z "$(MSG)" ]; then echo "usage: make revision MSG=\"describe the change\"" >&2; exit 1; fi
	DATABASE_URL="$(DEV_DATABASE_URL)" $(UV) run alembic -c hub/alembic.ini revision --autogenerate -m "$(MSG)"

dev: .venv/.synced migrate ## Run the hub locally against the dev database, with auto-reload
	DATABASE_URL="$(DEV_DATABASE_URL)" $(UV) run --package timekpr-hub \
		uvicorn timekpr_hub.app:app --reload --host 127.0.0.1 --port 8000

# ---------------------------------------------------------------- packaging --

build-wheels: .venv/.synced ## Build wheels for core/hub/agent into dist/
	$(UV) build --all-packages -o dist/

image: ## Build the hub's production container image
	docker build -f hub/Dockerfile -t $(IMAGE) .

build: build-wheels image ## Build wheels and the hub container image

# ------------------------------------------------------------------ deploy --

deploy-up: ## Start the production stack (hub + postgres + caddy + backups)
	@test -f deploy/.env || { echo "deploy/.env missing -- copy deploy/.env.example and fill it in" >&2; exit 1; }
	$(PROD_COMPOSE) up -d --build

deploy-down: ## Stop the production stack
	$(PROD_COMPOSE) down

deploy-logs: ## Tail production stack logs
	$(PROD_COMPOSE) logs -f

# ------------------------------------------------------------------ clean --

clean: ## Remove caches (pytest/ruff/mypy/hypothesis) and dist/
	rm -rf .pytest_cache .ruff_cache .mypy_cache .hypothesis dist build
	find . -name __pycache__ -not -path './.venv/*' -not -path './agent/.venv/*' -exec rm -rf {} +

distclean: clean ## clean, plus remove the venv entirely
	rm -rf .venv
