SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= .env
TEST_ENV_FILE ?= .env.test
COMPOSE := docker compose
# Services started by `test-integration` (db_update/master_bot/bot_worker join after Stage 1 wave 2)
TEST_SERVICES ?= postgres redis rabbitmq minio
WAIT_TIMEOUT ?= 300
# Observability stack (docker-compose.obs.yml); uses the same ENV_FILE (ENV_FILE=.env.test for test)
OBS_COMPOSE = $(COMPOSE) -f docker-compose.obs.yml --env-file $(ENV_FILE)
OBS_DIRS := OBS_POSTGRES_DATA_DIR OBS_REDIS_DATA_DIR OBS_GRAFANA_DATA_DIR OBS_PROMETHEUS_DATA_DIR \
	OBS_LOKI_DATA_DIR OBS_ALERTMANAGER_DATA_DIR OBS_CADDY_DATA_DIR
# Dirs of services running as APP_UID:APP_GID (`user:` in docker-compose.obs.yml): must stay writable
OBS_APP_UID_DIRS := OBS_GRAFANA_DATA_DIR OBS_PROMETHEUS_DATA_DIR OBS_LOKI_DATA_DIR \
	OBS_ALERTMANAGER_DATA_DIR
# Collection agent (service alloy, profile observability in docker-compose.yml).
# ALLOY_SINGLE_HOST=1 in ENV_FILE adds the override joining the obs stack network.
ALLOY_SINGLE_HOST_FILE := ops/observability/alloy/compose.single-host.yml
define agent_compose
$(call load_env,$(ENV_FILE)); \
files="-f docker-compose.yml"; \
if [ "$${ALLOY_SINGLE_HOST:-0}" = "1" ]; then files="$$files -f $(ALLOY_SINGLE_HOST_FILE)"; fi; \
$(COMPOSE) $$files --env-file $(ENV_FILE) --profile observability
endef

.PHONY: help install init-dirs env-test up down migrate lint format format-check check test \
	test-integration test-integration-down clean archive-audit \
	obs-config obs-up obs-down obs-ps obs-logs agent-config agent-up agent-down agent-logs

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

# Service dirs (db, redis, ...) are re-owned by their containers; the logs dir is written by
# app containers running as APP_UID, so it must be writable by us or owned by APP_UID.
# A permission problem ends with a chown hint instead of a raw `mkdir: Permission denied`.
init-dirs: ## Create bind-mount data directories from ENV_FILE
	@$(call load_env,$(ENV_FILE)); \
	deny() { \
		echo "ERROR: $$1 is not writable for uid=$$(id -u) gid=$$(id -g)." >&2; \
		echo "       Host data dirs must belong to APP_UID:APP_GID. Fix once on the host:" >&2; \
		echo "       sudo chown -R $${APP_UID:-$$(id -u)}:$${APP_GID:-$$(id -g)} $$1 && make init-dirs" >&2; \
		exit 1; \
	}; \
	for var in POSTGRES_DATA_DIR REDIS_DATA_DIR RABBITMQ_DATA_DIR MINIO_DATA_DIR LOGS_DATA_DIR; do \
		[ -n "$${!var:-}" ] || { echo "ERROR: $$var is empty in $(ENV_FILE)"; exit 1; }; \
		mkdir -p "$${!var}" 2>/dev/null || deny "$${!var}"; \
		echo "OK $$var=$${!var}"; \
	done; \
	if [ ! -w "$$LOGS_DATA_DIR" ] && [ "$$(stat -c %u "$$LOGS_DATA_DIR")" != "$${APP_UID:-}" ]; then \
		deny "$$LOGS_DATA_DIR"; \
	fi; \
	for sub in users bots system system/tmp; do \
		mkdir -p "$$LOGS_DATA_DIR/$$sub" 2>/dev/null || deny "$$LOGS_DATA_DIR"; \
	done; \
	if [ -n "$${ALLOY_DATA_DIR:-}" ]; then \
		mkdir -p "$$ALLOY_DATA_DIR" 2>/dev/null || deny "$$ALLOY_DATA_DIR"; \
		echo "OK ALLOY_DATA_DIR=$$ALLOY_DATA_DIR"; \
	else echo "SKIP ALLOY_DATA_DIR (empty; needed only for make agent-up)"; fi; \
	for var in $(OBS_DIRS); do \
		if [ -z "$${!var:-}" ]; then echo "SKIP $$var (empty; needed only for make obs-up)"; continue; fi; \
		mkdir -p "$${!var}" 2>/dev/null || deny "$${!var}"; \
		echo "OK $$var=$${!var}"; \
	done; \
	for var in $(OBS_APP_UID_DIRS); do \
		[ -n "$${!var:-}" ] || continue; \
		probe="$${!var}/.init-dirs-probe"; \
		{ touch "$$probe" 2>/dev/null && rm -f "$$probe"; } \
			|| [ "$$(stat -c %u "$${!var}")" = "$${APP_UID:-}" ] || deny "$${!var}"; \
	done

env-test: ## Generate .env.test from .env (isolated data dirs, shifted ports, mock token, stable key)
	@bash ops/scripts/env_test.sh "$(ENV_FILE)" "$(TEST_ENV_FILE)"

up: ## Build and start the stack in background
	$(COMPOSE) --env-file $(ENV_FILE) up -d --build

down: ## Stop the stack
	$(COMPOSE) --env-file $(ENV_FILE) down

migrate: init-dirs ## Apply DB migrations (db_update service); creates bind-mount dirs first
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
		MINIO_ENDPOINT="127.0.0.1:$${MINIO_API_PORT_HOST:-9000}" \
		REQUIRE_INFRA=1; \
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

obs-config: ## Validate docker-compose.obs.yml with ENV_FILE (docker compose config)
	$(OBS_COMPOSE) config --quiet && echo "docker-compose.obs.yml OK ($(ENV_FILE))"

obs-up: init-dirs ## Start observability stack (ENV_FILE=.env.test for test copy), wait for healthy
	$(OBS_COMPOSE) up -d --wait --wait-timeout $(WAIT_TIMEOUT)

obs-down: ## Stop observability stack (data in OBS_*_DATA_DIR is kept)
	$(OBS_COMPOSE) down

obs-ps: ## Show observability stack services
	$(OBS_COMPOSE) ps

obs-logs: ## Follow observability logs (SERVICE=grafana to filter)
	$(OBS_COMPOSE) logs -f --tail=200 $(SERVICE)

agent-config: ## Validate main compose with the observability profile (Alloy)
	@$(agent_compose) config --quiet && echo "docker-compose.yml + profile observability OK ($(ENV_FILE))"

agent-up: init-dirs ## Start the main stack + Alloy (profile observability), wait for healthy
	@$(agent_compose) up -d --build --wait --wait-timeout $(WAIT_TIMEOUT)

agent-down: ## Stop only Alloy (the main stack keeps running)
	@$(agent_compose) rm -s -f alloy

agent-logs: ## Follow Alloy logs
	@$(agent_compose) logs -f --tail=200 alloy

# --tmp-dir must be a path *inside* the container: the script runs in ``db_update``, where
# ``LOGS_DATA_DIR`` (host path) is mounted at ``${LOGS_DIR}`` (``/app/logs``). Temporary
# archives live in ``system/tmp`` (not next to users/bots/system). ``abspath``
# here would produce a host path the container cannot see (RC-4).
archive-audit: ## Archive old audit partitions to MinIO inside app container (DRY_RUN=1 for dry run)
	@test -f ops/scripts/archive_audit_partitions.py || { \
		echo "ERROR: ops/scripts/archive_audit_partitions.py не найден — появится в задаче archive_audit_partitions"; exit 1; }
	@$(call load_env,$(ENV_FILE)); \
	$(COMPOSE) --env-file $(ENV_FILE) run --rm --no-deps db_update \
		python ops/scripts/archive_audit_partitions.py \
			--tmp-dir "$${LOGS_DIR:-/app/logs}/system/tmp" \
			$(if $(filter 1,$(DRY_RUN)),--dry-run)
