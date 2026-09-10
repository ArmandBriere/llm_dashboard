# Local deploy for the llm-dashboard background service.
#
#   make          sync the runtime checkout, refresh deps, restart the
#                 LaunchAgent, verify it came back
#   make help     every target
#
# service.sh owns launchd; this file owns the ordering around it.
# Background: docs/deploying.md, docs/running-24-7.md

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := deploy
.NOTPARALLEL:

# launchd serves this checkout, which is not the worktree you edit in. The
# deploy and service targets act on RUNTIME_DIR; run/dev/test act on $(CURDIR).
RUNTIME_DIR ?= $(HOME)/src/llm_dashboard
REF ?= origin/main

LABEL := com.armandbriere.llm-dashboard
DOMAIN := gui/$(shell id -u)
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
TEMPLATE := $(RUNTIME_DIR)/$(LABEL).plist.template
SERVICE := $(RUNTIME_DIR)/service.sh
RUNTIME_PY := $(RUNTIME_DIR)/.venv/bin/python

HOST ?= 127.0.0.1
PORT ?= 8000
URL := http://$(HOST):$(PORT)
DEV_PORT ?= 8001

.PHONY: deploy sync deps restart verify log rollback install uninstall status logs stop run dev test venv port help

deploy: sync deps restart verify ## Deploy REF to the running service (default target)
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

deps: ## Sync the runtime venv against requirements.txt
	@echo "==> Syncing dependencies"
	@if command -v uv >/dev/null 2>&1; then \
		[ -x "$(RUNTIME_PY)" ] || uv venv "$(RUNTIME_DIR)/.venv"; \
		uv pip install -r "$(RUNTIME_DIR)/requirements.txt" --python "$(RUNTIME_PY)"; \
	else \
		[ -x "$(RUNTIME_PY)" ] || python3 -m venv "$(RUNTIME_DIR)/.venv"; \
		"$(RUNTIME_DIR)/.venv/bin/pip" install -q -r "$(RUNTIME_DIR)/requirements.txt"; \
	fi

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

run: venv ## Run this checkout in the foreground and open a browser (needs 'make stop')
	@./start.sh

dev: venv ## Run this checkout with auto-reload on DEV_PORT, alongside the service
	@.venv/bin/python -c "import watchfiles" 2>/dev/null || { \
		echo "==> Installing watchfiles (dev only: uvicorn --reload needs it)"; \
		if command -v uv >/dev/null 2>&1; then uv pip install -q watchfiles --python .venv/bin/python; \
		else .venv/bin/pip install -q watchfiles; fi; \
	}
	@.venv/bin/python -m uvicorn backend.main:app --reload --host "$(HOST)" --port "$(DEV_PORT)"

test: venv ## Run the test suite against this checkout
	@.venv/bin/python -m pytest

venv: .venv/bin/python ## Create this checkout's venv if it is missing

.venv/bin/python: requirements.txt
	@if command -v uv >/dev/null 2>&1; then \
		uv venv .venv; \
		uv pip install -r requirements.txt --python .venv/bin/python; \
	else \
		python3 -m venv .venv; \
		.venv/bin/pip install -q -r requirements.txt; \
	fi
	@touch .venv/bin/python

port: ## Show what is holding PORT
	@lsof -nP -iTCP:$(PORT) -sTCP:LISTEN || echo "nothing listening on $(PORT)"

help: ## List targets
	@grep -hE '^[a-z.].*:.*##' $(MAKEFILE_LIST) \
	  | sed -e 's/:.*##/\t/' \
	  | awk -F'\t' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  RUNTIME_DIR=$(RUNTIME_DIR)  REF=$(REF)  PORT=$(PORT)"
