SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= .env
TEST_ENV_FILE ?= .env.test
COMPOSE := docker compose
# Services started by `test-integration` (db_update/master_bot/bot_worker join after Stage 1 wave 2)
TEST_SERVICES ?= postgres redis rabbitmq minio
WAIT_TIMEOUT ?= 300

.PHONY: help install init-dirs env-test up down migrate lint format format-check check test \
	test-integration test-integration-down clean archive-audit

# Loads $(1) into the recipe shell (bash expands ${PROJECT_DATA_DIR}-style references).
# Limitation (same as docker compose interpolation): values must not contain unescaped `$`,
# backticks or unbalanced quotes — see .env.example.
define load_env
test -f "$(1)" || { echo "ERROR: $(1) not found (cp .env.example .env)"; exit 1; }; \
set -a; source "$(1)"; set +a
endef

# Removes PROJECT_DATA_DIR from $(TEST_ENV_FILE). Guard: absolute path, depth >= 2, ends with _test.
# Files of bind mounts may belong to container uids (e.g. 999), so they are removed via docker.
define purge_test_data
$(call load_env,$(TEST_ENV_FILE)); \
dir="$${PROJECT_DATA_DIR%/}"; \
case "$$dir" in \
	*/../*|*/./*|*/..|*/.) echo "ERROR: refusing to remove '$$dir': relative segments"; exit 1;; \
	/*/*_test) ;; \
	*) echo "ERROR: refusing to remove '$$dir': must be absolute, depth >= 2, end with _test"; exit 1;; \
esac; \
if [ -d "$$dir" ]; then \
	docker run --rm -v "$$dir":/d alpine sh -c 'rm -rf /d/* /d/.[!.]* /d/..?*' \
		&& rmdir "$$dir" && echo "Removed $$dir" \
		|| { echo "ERROR: could not remove $$dir; remove it manually"; exit 1; }; \
else echo "Nothing to remove: $$dir"; fi
endef

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install dependencies (uv sync) and pre-commit hooks
	uv sync
	uv run pre-commit install

init-dirs: ## Create bind-mount data directories from ENV_FILE
	@$(call load_env,$(ENV_FILE)); \
	for var in POSTGRES_DATA_DIR REDIS_DATA_DIR RABBITMQ_DATA_DIR MINIO_DATA_DIR LOGS_DATA_DIR; do \
		[ -n "$${!var:-}" ] || { echo "ERROR: $$var is empty in $(ENV_FILE)"; exit 1; }; \
		mkdir -p "$${!var}" && echo "OK $$var=$${!var}"; \
	done; \
	mkdir -p "$$LOGS_DATA_DIR"/{users,bots,system}

env-test: ## Generate .env.test from .env (isolated data dirs, shifted ports, mock token, stable key)
	@bash ops/scripts/env_test.sh "$(ENV_FILE)" "$(TEST_ENV_FILE)"

up: ## Build and start the stack in background
	$(COMPOSE) --env-file $(ENV_FILE) up -d --build

down: ## Stop the stack
	$(COMPOSE) --env-file $(ENV_FILE) down

migrate: ## Apply DB migrations (db_update service)
	$(COMPOSE) --env-file $(ENV_FILE) run --rm db_update

lint: ## Run ruff and mypy
	uv run ruff check . && uv run mypy app tests

format: ## Format code with ruff
	uv run ruff format . && uv run ruff check --fix .

format-check: ## Check formatting (no changes)
	uv run ruff format --check .

check: lint format-check test ## Lint + format check + unit tests

test: ## Run unit tests
	uv run pytest tests/unit -q

test-integration: env-test ## Start test infra (TEST_SERVICES) and run integration tests; stack stays up
	@$(MAKE) --no-print-directory init-dirs ENV_FILE=$(TEST_ENV_FILE)
	$(COMPOSE) --env-file $(TEST_ENV_FILE) up -d --build --wait --wait-timeout $(WAIT_TIMEOUT) $(TEST_SERVICES)
	@$(call load_env,$(TEST_ENV_FILE)); \
	export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT="$${POSTGRES_PORT_HOST:-5432}" \
		REDIS_HOST=127.0.0.1 REDIS_PORT="$${REDIS_PORT_HOST:-6379}" \
		RABBITMQ_HOST=127.0.0.1 RABBITMQ_PORT="$${RABBITMQ_PORT_HOST:-5672}" \
		MINIO_ENDPOINT="127.0.0.1:$${MINIO_API_PORT_HOST:-9000}"; \
	rc=0; uv run pytest -m integration tests/integration || rc=$$?; \
	if [ $$rc -eq 5 ]; then echo "No integration tests collected — treated as success"; rc=0; fi; \
	echo "Test stack is left running; stop it with: make test-integration-down [PURGE=1]"; \
	exit $$rc

test-integration-down: ## Stop integration stack (PURGE=1 also removes test data dir)
	$(COMPOSE) --env-file $(TEST_ENV_FILE) down -v
	@if [ "$(PURGE)" = "1" ]; then $(purge_test_data); fi

clean: ## Stop test stack, remove test data dir and caches
	@if [ -f "$(TEST_ENV_FILE)" ]; then \
		$(COMPOSE) --env-file $(TEST_ENV_FILE) down -v && { $(purge_test_data); }; \
	else echo "$(TEST_ENV_FILE) not found, skipping test stack cleanup"; fi
	rm -rf .ruff_cache .mypy_cache .pytest_cache .coverage htmlcov
	find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} +

archive-audit: ## Archive old audit partitions to MinIO inside app container (DRY_RUN=1 for dry run)
	@test -f ops/scripts/archive_audit_partitions.py || { \
		echo "ERROR: ops/scripts/archive_audit_partitions.py не найден — появится в задаче archive_audit_partitions"; exit 1; }
	$(COMPOSE) --env-file $(ENV_FILE) run --rm --no-deps db_update \
		python ops/scripts/archive_audit_partitions.py $(if $(filter 1,$(DRY_RUN)),--dry-run)
