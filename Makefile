# Developer entrypoints and the local deploy for the llm-dashboard service.
#
#   make setup    install everything the checkout needs (uv sync + bun install)
#   make run      run this checkout in the foreground and open a browser
#   make check    lint + format check + tests, the same gate CI runs
#   make deploy   sync the runtime checkout, refresh deps, restart the
#                 LaunchAgent, verify it came back
#   make help     every target
#
# service.sh owns launchd; this file owns the ordering around it.
# Background: docs/deploying.md, docs/running-24-7.md

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
.NOTPARALLEL:

# launchd serves this checkout, which is not the worktree you edit in. The
# deploy and service targets act on RUNTIME_DIR; everything else acts on $(CURDIR).
RUNTIME_DIR ?= $(HOME)/src/llm_dashboard
REF ?= origin/main

LABEL := com.armandbriere.llm-dashboard
DOMAIN := gui/$(shell id -u)
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
TEMPLATE := $(RUNTIME_DIR)/$(LABEL).plist.template
SERVICE := $(RUNTIME_DIR)/service.sh

HOST ?= 127.0.0.1
PORT ?= 8000
URL := http://$(HOST):$(PORT)
DEV_PORT ?= 8001

PY := .venv/bin/python
RUFF := .venv/bin/ruff
PRETTIER := node_modules/.bin/prettier
SHELL_SCRIPTS := start.sh run-service.sh service.sh

.PHONY: help setup venv node_modules run dev test lint format check \
        deploy sync deps restart verify log rollback install uninstall status logs stop port

##@ Development (this checkout)

setup: venv node_modules ## Install Python and formatting dependencies

venv: .venv/.synced ## Create or update this checkout's venv

.venv/.synced: pyproject.toml uv.lock
	@command -v uv >/dev/null 2>&1 || { echo "ERROR: uv is required: https://docs.astral.sh/uv/ (brew install uv)"; exit 1; }
	@uv sync --quiet
	@touch $@

node_modules: package.json bun.lock
	@command -v bun >/dev/null 2>&1 || { echo "ERROR: bun is required for formatting: https://bun.sh (brew install oven-sh/bun/bun)"; exit 1; }
	@bun install --silent
	@touch $@

run: venv ## Run in the foreground and open a browser (needs 'make stop' if the service holds the port)
	@./start.sh

dev: venv ## Run with auto-reload on DEV_PORT, alongside the service
	@$(PY) -m uvicorn backend.main:app --reload --host "$(HOST)" --port "$(DEV_PORT)"

test: venv ## Run the test suite
	@$(PY) -m pytest

lint: venv node_modules ## Lint Python, check formatting of everything, shellcheck the scripts
	@echo "==> ruff check"
	@$(RUFF) check backend tests
	@echo "==> ruff format --check"
	@$(RUFF) format --check backend tests
	@echo "==> prettier --check"
	@$(PRETTIER) --check . --log-level warn
	@if command -v shellcheck >/dev/null 2>&1; then \
		echo "==> shellcheck"; shellcheck -S warning $(SHELL_SCRIPTS); \
	else \
		echo "==> shellcheck not installed, skipping (brew install shellcheck)"; \
	fi

format: venv node_modules ## Rewrite Python, frontend and docs in the canonical style
	@$(RUFF) check backend tests --fix
	@$(RUFF) format backend tests
	@$(PRETTIER) --write . --log-level warn

check: lint test ## Everything CI runs

##@ Service (the runtime checkout under RUNTIME_DIR)

deploy: sync deps restart verify ## Deploy REF to the running service
	@echo "==> Deployed $(REF) to $(RUNTIME_DIR) — $(URL)"

sync: ## Fast-forward the runtime checkout to REF
	@test -d "$(RUNTIME_DIR)/.git" || { echo "ERROR: no checkout at $(RUNTIME_DIR); pass RUNTIME_DIR=..."; exit 1; }
	@echo "==> Syncing $(RUNTIME_DIR) to $(REF)"
	@git -C "$(RUNTIME_DIR)" fetch --quiet origin
	@if [ -n "$$(git -C "$(RUNTIME_DIR)" status --porcelain)" ]; then \
		echo "ERROR: $(RUNTIME_DIR) has uncommitted changes — the runtime is not a place to keep local edits:"; \
		git -C "$(RUNTIME_DIR)" status --short; \
		exit 1; \
	fi
	@git -C "$(RUNTIME_DIR)" merge --ff-only "$(REF)"

deps: ## Sync the runtime venv against uv.lock (runtime dependencies only)
	@echo "==> Syncing dependencies"
	@uv sync --project "$(RUNTIME_DIR)" --no-dev --quiet

# A kickstart reuses the already-rendered plist, so a template change needs a
# full install instead. Comparing against the rendered copy also covers the
# not-yet-installed case.
restart: ## Restart the service, reinstalling the agent if the plist template moved
	@if [ ! -f "$(PLIST)" ] || ! sed -e 's|__APP_DIR__|$(RUNTIME_DIR)|g' -e 's|__HOME__|$(HOME)|g' \
		  "$(TEMPLATE)" | diff -q - "$(PLIST)" >/dev/null 2>&1; then \
		echo "==> Agent plist is stale or missing — reinstalling"; \
		"$(SERVICE)" install; \
	else \
		echo "==> Restarting $(LABEL)"; \
		"$(SERVICE)" restart; \
	fi

# Asserts on the served bytes, not on files on disk. Deliberately does not
# assert on the /api/status body: the collection pass a restart triggers can
# legitimately come back empty when Anthropic rate-limits it (see deploying.md).
verify: ## Check the service is listening and report what it is serving
	@echo "==> Verifying"
	@for i in $$(seq 1 30); do \
		if [ "$$(curl -s -o /dev/null -w '%{http_code}' "$(URL)/" || true)" = "200" ]; then break; fi; \
		sleep 0.5; \
	done; \
	code=$$(curl -s -o /dev/null -w '%{http_code}' "$(URL)/" || true); \
	if [ "$$code" != "200" ]; then \
		echo "ERROR: $(URL)/ returned '$$code' — check 'make logs'"; \
		exit 1; \
	fi; \
	echo "GET $(URL)/ -> $$code"
	@git -C "$(RUNTIME_DIR)" log --oneline -1
	@"$(SERVICE)" status
	@curl -s "$(URL)/api/status"; echo

log: ## Recent history of the runtime checkout
	@git -C "$(RUNTIME_DIR)" log --oneline -10

rollback: ## Reset the runtime to SHA and restart: make rollback SHA=abc1234
	@test -n "$(SHA)" || { echo "ERROR: pass SHA=<good-sha>; 'make log' lists candidates"; exit 1; }
	@git -C "$(RUNTIME_DIR)" reset --hard "$(SHA)"
	@$(MAKE) --no-print-directory deps restart verify

install: ## Render the plist, load the agent, start it now
	@"$(SERVICE)" install

uninstall: ## Stop the service and remove the agent
	@"$(SERVICE)" uninstall

status: ## Service state / pid / last exit code
	@"$(SERVICE)" status

logs: ## Tail the service logs
	@"$(SERVICE)" logs

# bootout leaves the rendered plist in place, so 'make install' brings it back.
stop: ## Unload the service to free the port (make install restarts it)
	@launchctl bootout "$(DOMAIN)/$(LABEL)" 2>/dev/null || true
	@echo "Unloaded $(LABEL) — 'make install' brings it back"

port: ## Show what is holding PORT
	@lsof -nP -iTCP:$(PORT) -sTCP:LISTEN || echo "nothing listening on $(PORT)"

help: ## List targets
	@awk 'BEGIN {FS = ":.*##"} \
	  /^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
	  /^[a-zA-Z_.-]+:.*##/ {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  RUNTIME_DIR=$(RUNTIME_DIR)  REF=$(REF)  PORT=$(PORT)  DEV_PORT=$(DEV_PORT)"
