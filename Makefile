SHELL := /bin/bash
# Must precede any $(shell): GNU make >= 4.4 passes exported variables to $(shell) and
# command-line variables are expanded on export, so `make obs-bans UNBAN='$(shell cmd)'`
# would run cmd at parse time (TEMPO_IMAGE_DEFAULT := $(shell ...)). See obs-bans.
unexport UNBAN
.DEFAULT_GOAL := help

ENV_FILE ?= .env
TEST_ENV_FILE ?= .env.test
COMPOSE := docker compose
# Services started by `test-integration` (db_update/master_bot/bot_worker join after Stage 1 wave 2)
TEST_SERVICES ?= postgres redis rabbitmq minio
WAIT_TIMEOUT ?= 300
# Unit tests: label of the Dockerfile stage `test` images (make test-unit runs the image by id,
# so several clones on one host never race on a shared tag; make clean prunes them by label),
# paths and pytest args (e.g. make test-unit UNIT_TESTS=tests/unit/test_encrypted_string.py PYTEST_ARGS=-v)
TEST_IMAGE_LABEL := tg_shop_platform.role=unit-test
UNIT_TESTS ?= tests/unit
PYTEST_ARGS ?= -q
# Extra pytest args of test-integration (test-report passes --req-report/--junitxml)
INTEGRATION_PYTEST_ARGS ?=
# test-report: raw results (git-ignored build/) and the customer report (Markdown)
REPORT_DIR ?= build/reports
# For delivery: commit the code, then make test-report REPORT_MD=docs/reports/milestone-I-tests.md
# on the clean tree and commit the report separately (it names the verified commit).
REPORT_MD ?= $(REPORT_DIR)/milestone-I-tests.md
# promtool image = the Prometheus image pinned in docker-compose.obs.yml (single source)
PROM_DIR := ops/observability/prometheus
PROMTOOL_IMAGE ?= $(shell sed -n 's/.*PROMETHEUS_IMAGE:-\([^}]*\)}.*/\1/p' docker-compose.obs.yml | head -n1)
# make ready: how long to wait for /health/ready = 200 of master_bot and bot_worker (seconds)
READY_TIMEOUT ?= 60
# Observability stack (docker-compose.obs.yml); uses the same ENV_FILE (ENV_FILE=.env.test for test)
OBS_COMPOSE = $(COMPOSE) -f docker-compose.obs.yml --env-file $(ENV_FILE)
OBS_DIRS := OBS_URGENTRY_DATA_DIR OBS_TEMPO_DATA_DIR OBS_GRAFANA_DATA_DIR OBS_PROMETHEUS_DATA_DIR \
	OBS_LOKI_DATA_DIR OBS_ALERTMANAGER_DATA_DIR OBS_CADDY_DATA_DIR OBS_CADDY_LOGS_DIR \
	OBS_CROWDSEC_DATA_DIR
# Dirs of services running as 10001:10001 (urgentry, Tempo): chowned by make init-dirs
# (directly as root, otherwise via a one-shot container: no sudo needed)
OBS_10001_DIRS := OBS_URGENTRY_DATA_DIR OBS_TEMPO_DATA_DIR
OBS_CHOWN_IMAGE ?= alpine:3.21@sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab
# Tempo image pinned in docker-compose.obs.yml (config check of obs-validate)
TEMPO_IMAGE_DEFAULT := $(shell sed -n 's/.*TEMPO_IMAGE:-\([^}]*\)}.*/\1/p' docker-compose.obs.yml | head -n1)
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

# Main stack (no observability profile). ALLOY_SINGLE_HOST=1 adds the same overlay as
# agent_compose: otherwise `make up` recreates master_bot/bot_worker without the obs network
# (SENTRY_DSN -> caddy:8081 breaks) on a single host. The overlay declares the obs network as
# external, so it is added only when that network exists (obs stack up); otherwise the bots
# start without it (WARN) and `make agent-up` joins them after `make obs-up`.
define stack_compose
$(call load_env,$(ENV_FILE)); \
files="-f docker-compose.yml"; \
if [ "$${ALLOY_SINGLE_HOST:-0}" = "1" ]; then \
	if docker network inspect "$${PROJECT_NAME:-tg_shop_platform}_obs_default" >/dev/null 2>&1; then \
		files="$$files -f $(ALLOY_SINGLE_HOST_FILE)"; \
	else echo "WARN: obs-стек не поднят: боты запущены без obs-сети; после make obs-up выполните make agent-up" >&2; fi; \
fi; \
$(COMPOSE) $$files --env-file $(ENV_FILE)
endef

# agent-up with ALLOY_SINGLE_HOST=1 needs the obs network: Alloy without the obs stack is useless.
define agent_obs_net_guard
$(call load_env,$(ENV_FILE)); \
net="$${PROJECT_NAME:-tg_shop_platform}_obs_default"; \
if [ "$${ALLOY_SINGLE_HOST:-0}" = "1" ] && ! docker network inspect "$$net" >/dev/null 2>&1; then \
	echo "ERROR: network $$net not found (ALLOY_SINGLE_HOST=1): run make obs-up first" >&2; exit 1; fi
endef

# Exports APP_VERSION/APP_COMMIT/APP_BUILD_TIME (image build args) into the recipe shell.
# A missing milestone tag fails the recipe with a hint (see docs/ops/versioning.md).
# Alloy starts with an empty OBS_TEMPO_OTLP_ENDPOINT (logs/metrics keep flowing), but its
# otelcol.exporter.otlp.tempo fails ("received empty target") and app traces are dropped.
define agent_tempo_guard
$(call load_env,$(ENV_FILE)); \
test -n "$${OBS_TEMPO_OTLP_ENDPOINT:-}" || { echo "ERROR: OBS_TEMPO_OTLP_ENDPOINT is empty in $(ENV_FILE): set tempo:4317 (one host) or <VPN IP of VPS 2>:4317" >&2; exit 1; }
endef

define version_env
vars=$$(sh ops/scripts/version.sh --env) || exit 1; eval "$$vars"; \
export APP_VERSION APP_COMMIT APP_BUILD_TIME; echo "App version: $$APP_VERSION ($$APP_COMMIT)"
endef

.PHONY: help install env env-check init-dirs env-test version up ready down migrate lint format format-check check \
	check-dev test test-unit test-unit-dev \
	test-integration test-integration-down test-alerts test-report clean archive-audit \
	obs-config obs-up obs-down obs-ps obs-logs obs-bootstrap obs-auth obs-bans obs-validate obs-crowdsec-rekey \
	agent-config agent-up agent-down agent-logs

# Loads $(1) into the recipe shell (bash expands ${PROJECT_DATA_DIR}-style references).
# Limitation (same as docker compose interpolation): values must not contain unescaped `$`,
# backticks or unbalanced quotes — see .env.example.
# Loads ENV_FILE and exports mode-dependent variables of the obs stack (CADDY_MODE,
# GRAFANA_ROOT_URL, GRAFANA_SERVE_FROM_SUB_PATH, URGENTRY_PUBLIC_URL) for every docker compose
# call (shell env wins over --env-file). $(1)=--check: guard of obs-up/obs-config;
# --lenient: obs-down/ps/logs (CADDY_MODE may be missing in an old .env).
define obs_env
$(call load_env,$(ENV_FILE)); \
obs_exports=$$(bash ops/scripts/obs_mode.sh $(1)) || exit 1; eval "$$obs_exports"
endef

define load_env
test -f "$(1)" || { echo "ERROR: $(1) not found ($(if $(filter $(abspath .env),$(abspath $(1))),make env,make env-test))"; exit 1; }; \
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

# FORCE=1: the new file is generated into <file>.new first; only after success the old one is
# backed up to <file>.bak.<timestamp> (mode 600, never overwritten) and replaced.
env: ## Generate ENV_FILE from .env.example with random secrets (SKIP if exists; FORCE=1 regenerates, backup .bak.<ts>)
	@set -e; f="$(ENV_FILE)"; \
	if [ -e "$$f" ] && [ "$(FORCE)" != "1" ]; then echo "SKIP: $$f exists (FORCE=1 to regenerate)"; exit 0; fi; \
	if [ ! -e "$$f" ]; then \
		OBS_AUTH_IMPLICIT="$(if $(filter env,$(MAKECMDGOALS)),0,1)" \
			bash ops/scripts/env_test.sh --init .env.example "$$f"; exit 0; fi; \
	rm -f "$$f.new"; \
	bash ops/scripts/env_test.sh --init .env.example "$$f.new"; \
	bak="$$f.bak.$$(date +%Y%m%d%H%M%S)"; \
	if [ -e "$$bak" ]; then bak="$$bak.$$$$"; fi; \
	install -m 600 "$$f" "$$bak"; \
	mv -f "$$f.new" "$$f"; \
	echo "Backup: $$bak; $$f replaced (old passwords stay in existing PG/Redis/RabbitMQ data)"

# The main env file is generated automatically on a fresh clone (only for ENV_FILE=.env;
# .env.test comes from make env-test).
INIT_DIRS_DEPS := $(if $(filter $(abspath .env),$(abspath $(ENV_FILE))),env)

# Service dirs (db, redis, ...) are re-owned by their containers; the logs dir is written by
# app containers running as APP_UID, so it must be writable by us or owned by APP_UID.
# A permission problem ends with a chown hint instead of a raw `mkdir: Permission denied`.
init-dirs: $(INIT_DIRS_DEPS) ## Create bind-mount data directories from ENV_FILE (.env is generated if missing)
	@bash ops/scripts/env_check.sh --warn .env.example "$(ENV_FILE)"
	@$(call load_env,$(ENV_FILE)); \
	deny() { \
		owner="$${APP_UID:-$$(id -u)}:$${APP_GID:-$$(id -g)}"; root="$${PROJECT_DATA_DIR%/}"; \
		case "$$1" in "$$root"|"$$root"/*) [ -n "$$root" ] || root="$$1";; *) root="$$1";; esac; \
		echo "ERROR: $$1 is not writable for uid=$$(id -u) gid=$$(id -g)." >&2; \
		echo "       Host data dirs must belong to APP_UID:APP_GID. Fix once on the host:" >&2; \
		echo "       sudo mkdir -p $$root && sudo chown -R $$owner $$root && make init-dirs" >&2; \
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
	if [ -n "$${OBS_CROWDSEC_DATA_DIR:-}" ]; then \
		mkdir -p "$$OBS_CROWDSEC_DATA_DIR/data" "$$OBS_CROWDSEC_DATA_DIR/config" 2>/dev/null \
			|| deny "$$OBS_CROWDSEC_DATA_DIR"; \
	fi; \
	if [ -n "$${OBS_CADDY_LOGS_DIR:-}" ] && [ ! -e "$$OBS_CADDY_LOGS_DIR/access.log" ]; then \
		{ touch "$$OBS_CADDY_LOGS_DIR/access.log" && chmod 640 "$$OBS_CADDY_LOGS_DIR/access.log"; } 2>/dev/null \
			|| deny "$$OBS_CADDY_LOGS_DIR"; \
	fi; \
	for var in $(OBS_APP_UID_DIRS); do \
		[ -n "$${!var:-}" ] || continue; \
		probe="$${!var}/.init-dirs-probe"; \
		{ touch "$$probe" 2>/dev/null && rm -f "$$probe"; } \
			|| [ "$$(stat -c %u "$${!var}")" = "$${APP_UID:-}" ] || deny "$${!var}"; \
	done; \
	for var in $(OBS_10001_DIRS); do \
		[ -n "$${!var:-}" ] || continue; d="$${!var}"; \
		[ "$$(stat -c %u:%g "$$d")" = "10001:10001" ] && continue; \
		{ chown 10001:10001 "$$d" 2>/dev/null \
			|| docker run --rm -v "$$d:/d" $(OBS_CHOWN_IMAGE) chown 10001:10001 /d; } \
			|| { echo "ERROR: $$d must be owned by 10001:10001 (urgentry/Tempo): sudo chown 10001:10001 $$d" >&2; exit 1; }; \
		echo "OK $$var owned by 10001:10001"; \
	done

env-check: ## Check ENV_FILE and TEST_ENV_FILE have all keys of .env.example (names only, rc=1 if missing)
	@bash ops/scripts/env_check.sh .env.example "$(ENV_FILE)" "$(TEST_ENV_FILE)"

env-test: ## Generate .env.test from .env (isolated data dirs, shifted ports, mock token, stable key)
	@bash ops/scripts/env_test.sh "$(ENV_FILE)" "$(TEST_ENV_FILE)"

version: ## Print the application version (0.1.<milestone>.<commit>[+dirty])
	@sh ops/scripts/version.sh

up: ## Build and start the stack in background (image gets the current version), then make ready
	@$(version_env); \
	$(stack_compose) up -d --build --wait --wait-timeout $(WAIT_TIMEOUT)
	@$(MAKE) --no-print-directory ready ENV_FILE=$(ENV_FILE)

ready: ## Wait for /health/ready = 200 in master_bot and bot_worker (READY_TIMEOUT=60 s)
	@READY_TIMEOUT=$(READY_TIMEOUT) timeout $$(( $(READY_TIMEOUT) + 60 )) \
		bash ops/scripts/wait_ready.sh --env-file $(ENV_FILE)

down: ## Stop the stack
	$(COMPOSE) --env-file $(ENV_FILE) down

migrate: init-dirs ## Apply DB migrations (db_update service); creates bind-mount dirs first
	@$(version_env); \
	$(COMPOSE) --env-file $(ENV_FILE) run --rm db_update

lint: ## Run ruff and mypy
	uv run ruff check . && uv run mypy app tests

format: ## Format code with ruff
	uv run ruff format . && uv run ruff check --fix .

format-check: ## Check formatting (no changes)
	uv run ruff format --check .

check: test-unit ## Unit tests in Docker (uv not needed on the host)

check-dev: lint format-check test-unit-dev ## Lint + format check + unit tests via uv (development)

# --iidfile instead of `build -q`: the build log (and the reason of a failed build) stays visible.
test-unit: ## Unit tests in Docker (Dockerfile stage `test`, code mounted read-only; uv not needed)
	@iid=$$(mktemp) && trap 'rm -f "$$iid"' EXIT && \
	docker build --target test --label $(TEST_IMAGE_LABEL) --iidfile "$$iid" . && \
	docker run --rm --init --user "$$(id -u):$$(id -g)" -e HOME=/tmp -v "$(CURDIR):/app:ro" -w /app \
		"$$(cat "$$iid")" python -m pytest $(UNIT_TESTS) $(PYTEST_ARGS) -p no:cacheprovider

test-unit-dev: ## Unit tests on the host via uv (make install first)
	uv run pytest $(UNIT_TESTS) $(PYTEST_ARGS)

test: test-unit ## Alias of test-unit (compatibility; uv not needed)

test-integration: env-test ## Start test infra (TEST_SERVICES) and run integration tests; stack stays up
	@$(MAKE) --no-print-directory init-dirs ENV_FILE=$(TEST_ENV_FILE)
	@$(version_env); \
	$(COMPOSE) --env-file $(TEST_ENV_FILE) up -d --build --wait --wait-timeout $(WAIT_TIMEOUT) $(TEST_SERVICES)
	@$(call load_env,$(TEST_ENV_FILE)); \
	export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT="$${POSTGRES_PORT_HOST:-5432}" \
		REDIS_HOST=127.0.0.1 REDIS_PORT="$${REDIS_PORT_HOST:-6379}" \
		RABBITMQ_HOST=127.0.0.1 RABBITMQ_PORT="$${RABBITMQ_PORT_HOST:-5672}" \
		MINIO_ENDPOINT="127.0.0.1:$${MINIO_API_PORT_HOST:-9000}" \
		REQUIRE_INFRA=1; \
	rc=0; uv run pytest -m integration tests/integration $(INTEGRATION_PYTEST_ARGS) || rc=$$?; \
	if [ $$rc -eq 5 ]; then echo "No integration tests collected — treated as success"; rc=0; fi; \
	echo "Test stack is left running; stop it with: make test-integration-down [PURGE=1]"; \
	exit $$rc

test-alerts: ## Validate Prometheus alert rules and run their unit tests (promtool in Docker)
	@test -n "$(PROMTOOL_IMAGE)" || { echo "ERROR: Prometheus image not found in docker-compose.obs.yml"; exit 1; }
	docker run --rm --entrypoint promtool -v "$(CURDIR)/$(PROM_DIR):/p:ro" -w /p/tests $(PROMTOOL_IMAGE) \
		check rules $(patsubst $(PROM_DIR)/%,/p/%,$(wildcard $(PROM_DIR)/rules/*.yml))
	docker run --rm --entrypoint promtool -v "$(CURDIR)/$(PROM_DIR):/p:ro" -w /p/tests $(PROMTOOL_IMAGE) \
		test rules rules_test.yml

# $(SUBMAKE) instead of $(MAKE): GNU make runs lines that mention $(MAKE) even under `make -n`,
# which would start the test stack; an indirect reference keeps `make -n test-report` a dry run.
SUBMAKE = $(MAKE)

# Failing tests do not stop the chain: the report is always written, the target exits non-zero.
# The test stack is stopped afterwards only if it was not running before (test-integration
# itself leaves it up).
test-report: ## Unit + integration + promtool -> REPORT_MD (build/reports/milestone-I-tests.md; delivery: REPORT_MD=docs/reports/...)
	@mkdir -p $(REPORT_DIR); \
	rm -f $(addprefix $(REPORT_DIR)/,unit.json unit.xml integration.json integration.xml promtool.txt promtool.rc); \
	was_up=0; \
	if [ -f "$(TEST_ENV_FILE)" ] && [ -n "$$($(COMPOSE) --env-file $(TEST_ENV_FILE) ps -q 2>/dev/null)" ]; then was_up=1; fi; \
	rc=0; \
	echo "== unit tests"; \
	uv run pytest tests/unit -q --req-report=$(REPORT_DIR)/unit.json --junitxml=$(REPORT_DIR)/unit.xml || rc=1; \
	echo "== integration tests"; \
	$(SUBMAKE) --no-print-directory test-integration \
		INTEGRATION_PYTEST_ARGS="--req-report=$(REPORT_DIR)/integration.json --junitxml=$(REPORT_DIR)/integration.xml" || rc=1; \
	if [ $$was_up -eq 0 ]; then \
		echo "== stopping the test stack started by test-report"; \
		$(SUBMAKE) --no-print-directory test-integration-down || rc=1; \
	fi; \
	echo "== alert rules (promtool)"; \
	prc=0; $(SUBMAKE) --no-print-directory test-alerts > $(REPORT_DIR)/promtool.txt 2>&1 || prc=$$?; \
	cat $(REPORT_DIR)/promtool.txt; echo $$prc > $(REPORT_DIR)/promtool.rc; [ $$prc -eq 0 ] || rc=1; \
	uv run python ops/scripts/test_report.py --reports $(REPORT_DIR) --output $(REPORT_MD) || rc=1; \
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
	@if command -v docker >/dev/null 2>&1; then \
		docker image prune -af --filter label=$(TEST_IMAGE_LABEL) \
			|| echo "WARN: unit-test images ($(TEST_IMAGE_LABEL)) not removed" >&2; \
	fi

obs-config: ## Validate docker-compose.obs.yml with ENV_FILE (CADDY_MODE/VPS_IP/domains/OBS_AUTH_*/CROWDSEC_BOUNCER_KEY + compose config)
	@$(call obs_env,--check); \
	$(OBS_COMPOSE) config --quiet && echo "docker-compose.obs.yml OK ($(ENV_FILE), CADDY_MODE=$$CADDY_MODE)"

# Builds the own Caddy image (Go modules: network on the first build) and validates
# Caddyfile.<CADDY_MODE> in it; only the Caddyfile is mounted (no data dirs needed).
obs-validate: ## Build the Caddy image, caddy validate Caddyfile.<CADDY_MODE>, tempo -config.verify (ENV_FILE)
	@$(call obs_env,--check); \
	$(OBS_COMPOSE) build caddy && img=$$($(OBS_COMPOSE) config --images | grep -m1 -- '-obs-caddy:') && \
	docker run --rm -e VPS_IP -e GRAFANA_DOMAIN -e URGENTRY_DOMAIN -e OBS_AUTH_USER -e OBS_AUTH_HASH \
		-e CROWDSEC_BOUNCER_KEY -v "$(CURDIR)/ops/observability/caddy/Caddyfile.$$CADDY_MODE:/etc/caddy/Caddyfile:ro" \
		"$$img" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
	&& echo "Caddyfile.$$CADDY_MODE OK ($$img)" \
	&& timg="$${TEMPO_IMAGE:-$(TEMPO_IMAGE_DEFAULT)}" \
	&& docker run --rm -v "$(CURDIR)/ops/observability/tempo/tempo.yaml:/etc/tempo/tempo.yaml:ro" "$$timg" \
		-config.file=/etc/tempo/tempo.yaml -config.expand-env=true -config.verify=true \
	&& echo "tempo.yaml OK ($$timg)"

# The image registers the bouncer only if it does not exist yet: after changing
# CROWDSEC_BOUNCER_KEY the old registration must be deleted, otherwise LAPI answers 403.
obs-crowdsec-rekey: ## Re-register the Caddy bouncer after changing CROWDSEC_BOUNCER_KEY (recreates crowdsec + caddy)
	@$(call obs_env,--check); set -e; \
	$(OBS_COMPOSE) up -d --no-deps --wait --wait-timeout $(WAIT_TIMEOUT) crowdsec; \
	if $(OBS_COMPOSE) exec -T crowdsec cscli bouncers list -o json | grep -Eq '"name": *"caddy"'; then \
		$(OBS_COMPOSE) exec -T crowdsec cscli bouncers delete caddy; \
	else echo "bouncer caddy is not registered: nothing to delete"; fi; \
	$(OBS_COMPOSE) up -d --no-deps --force-recreate --wait --wait-timeout $(WAIT_TIMEOUT) crowdsec; \
	$(OBS_COMPOSE) up -d --no-deps --force-recreate --wait --wait-timeout $(WAIT_TIMEOUT) caddy

# --prepare: urgentry bootstrap secrets must be in ENV_FILE before its first start.
obs-up: init-dirs ## Start observability stack (ENV_FILE=.env.test for test copy), wait for healthy, bootstrap urgentry
	@bash ops/scripts/obs_bootstrap.sh --prepare "$(ENV_FILE)"
	@$(call obs_env,--check); \
	$(OBS_COMPOSE) up -d --build --wait --wait-timeout $(WAIT_TIMEOUT)
	@$(MAKE) --no-print-directory obs-bootstrap ENV_FILE=$(ENV_FILE)

obs-bootstrap: ## Idempotent: urgentry project key via API -> SENTRY_DSN in ENV_FILE (backup .bak)
	@$(call obs_env); \
	bash ops/scripts/obs_bootstrap.sh "$(ENV_FILE)"

# Password: interactive (hidden) or generated (empty answer / no tty), PASSWORD_STDIN=1 reads stdin.
obs-auth: ## Set Caddy Basic Auth (OBS_AUTH_USER/OBS_AUTH_HASH in ENV_FILE), recreate caddy if running
	@bash ops/scripts/obs_auth.sh "$(ENV_FILE)"
	@$(call obs_env); \
	if [ -n "$$($(OBS_COMPOSE) ps -q caddy 2>/dev/null)" ]; then \
		$(OBS_COMPOSE) up -d --no-deps --force-recreate --wait --wait-timeout $(WAIT_TIMEOUT) caddy; \
	else echo "caddy is not running: the new credentials apply on make obs-up"; fi

obs-down: ## Stop observability stack (data in OBS_*_DATA_DIR is kept)
	@$(call obs_env,--lenient); $(OBS_COMPOSE) down

# UNBAN: GNU make exports command-line variables and EXPANDS them on export
# (`UNBAN='$(shell ...)'` would run), so it is unexported at the top of this file (before any
# $(shell)); the recipe reads the raw text via $(value) inside single quotes (no shell
# expansion) and validates it as an IP before use.
UNBAN_SQ = '$(subst ','\'',$(value UNBAN))'
obs-bans: ## CrowdSec decisions (bans) of the public Caddy; UNBAN=<ip> removes the ban of one IP
	@unban=$(UNBAN_SQ); \
	if [ -n "$$unban" ]; then \
		python3 -c 'import ipaddress, sys; ipaddress.ip_address(sys.argv[1])' "$$unban" 2>/dev/null \
			|| { echo "ERROR: UNBAN is not an IP address" >&2; exit 1; }; \
	fi; \
	$(call obs_env,--lenient); \
	if [ -n "$$unban" ]; then \
		$(OBS_COMPOSE) exec -T crowdsec cscli decisions delete --ip "$$unban"; \
	else $(OBS_COMPOSE) exec -T crowdsec cscli decisions list; fi

obs-ps: ## Show observability stack services
	@$(call obs_env,--lenient); $(OBS_COMPOSE) ps

obs-logs: ## Follow observability logs (SERVICE=grafana to filter)
	@$(call obs_env,--lenient); $(OBS_COMPOSE) logs -f --tail=200 $(SERVICE)

# Same image (tag + digest) as the alloy service in docker-compose.yml.
ALLOY_CHECK_IMAGE ?= grafana/alloy:v1.11.3@sha256:8c7256f412feb9f5f48f9f6f9394dc97ca887f63dea9304f347970ecc1787669
ALLOY_CONFIG := ops/observability/alloy/config.alloy
otel-smoke: ## Tracing smoke in master_bot: span -> SQL -> RabbitMQ -> Sentry error; prints TRACE_ID (output also to container stdout -> Loki). DEEP_ERROR=1: demo SELECTs + deep chained error
	@$(agent_compose) exec -T master_bot bash -o pipefail -c 'python - $(if $(DEEP_ERROR),--deep-error) 2>&1 | tee /proc/1/fd/1' < ops/scripts/otel_smoke.py

agent-config: ## Validate main compose (profile observability) + Alloy config (fmt + validate)
	@$(agent_tempo_guard)
	@$(agent_compose) config --quiet && echo "docker-compose.yml + profile observability OK ($(ENV_FILE))"
	@docker run --rm -v "$(CURDIR)/$(dir $(ALLOY_CONFIG)):/c:ro" --entrypoint /bin/alloy $(ALLOY_CHECK_IMAGE) \
		fmt /c/$(notdir $(ALLOY_CONFIG)) | diff -u $(ALLOY_CONFIG) - \
		|| { echo "ERROR: $(ALLOY_CONFIG) is not alloy-fmt formatted (see diff above)" >&2; exit 1; }
	@docker run --rm -v "$(CURDIR)/$(dir $(ALLOY_CONFIG)):/c:ro" --entrypoint /bin/alloy $(ALLOY_CHECK_IMAGE) \
		validate /c/$(notdir $(ALLOY_CONFIG)) && echo "$(ALLOY_CONFIG): alloy fmt + validate OK"

agent-up: init-dirs ## Start the main stack + Alloy (profile observability), wait for healthy
	@$(agent_tempo_guard)
	@$(agent_obs_net_guard)
	@$(version_env); \
	$(agent_compose) up -d --build --wait --wait-timeout $(WAIT_TIMEOUT)
	@$(MAKE) --no-print-directory ready ENV_FILE=$(ENV_FILE)

agent-down: ## Stop only Alloy + docker-events + docker-socket-proxy (the main stack keeps running)
	@$(agent_compose) rm -s -f alloy docker-events docker-socket-proxy

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
