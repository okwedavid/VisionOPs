# ---------------------------------------------------------------------------
# VisionOps — the canonical command surface.
#
# Humans, agents and CI all run these targets (docs/TECHNICAL_DECISIONS.md
# §"Tooling"), so nobody has to remember two ways to run the same check.
# Every target carries a `## ` comment; `make help` prints them.
#
# Backend commands go through `uv run --frozen --extra dev`: `--frozen` never
# rewrites uv.lock behind your back, and `--extra dev` is what puts pytest,
# ruff and mypy in the environment (they are optional dependencies).
# ---------------------------------------------------------------------------

.DEFAULT_GOAL := help
SHELL := /bin/bash

UV := uv run --frozen --extra dev
CLI := $(UV) visionops
NPM := npm --prefix frontend

# Coverage gate for `make test-cov`. PRD §48 sets the backend bar at 75%;
# raise it, never lower it (docs/AGENT_WORKFLOW.md §"Never weaken a gate").
COVERAGE_GATE ?= 75

.PHONY: help install env model dev dev\:fixture demo lint format typecheck test \
        test-cov test-cv test-security test-frontend test-e2e test-model test-all \
        audit migrate migrate-test prune bench build build-web build-wheel clean

help: ## Show this help
	@printf '\nVisionOps — make <target>\n\n'
	@grep -E '^[a-zA-Z0-9_.\\:-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk '{ i = index($$0, "##"); if (i) { name = substr($$0, 1, i - 1); \
			t = index(name, ": "); if (t) name = substr(name, 1, t - 1); \
			sub(/[[:space:]]+$$/, "", name); gsub(/\\/, "", name); \
			desc = substr($$0, i + 3); sub(/^[[:space:]]+/, "", desc); \
			printf "  \033[36m%-16s\033[0m %s\n", name, desc } }'
	@printf '\n'

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

install: ## Install backend (uv) and frontend (npm) dependencies
	uv sync --frozen --extra dev
	$(NPM) ci

env: ## Create .env from .env.example with a dev master key and bootstrap key
	@test -f .env || cp .env.example .env
	@chmod 600 .env
	@grep -qE '^VISIONOPS_MASTER_KEY=.' .env || \
		(printf '\nVISIONOPS_MASTER_KEY=' >> .env && \
		 $(UV) python -c 'from visionops.core.security import generate_master_key; print(generate_master_key())' >> .env && \
		 echo 'generated VISIONOPS_MASTER_KEY in .env (mode 600) — back it up; losing it makes camera credentials unrecoverable')
	@# `visionops init` migrates, creates the organisation and writes the
	@# first API key into .env with 0600 permissions (it is shown only there).
	@$(CLI) init

model: ## Download and verify the detection model
	@$(CLI) model

migrate: ## Apply schema migrations
	@$(CLI) migrate

migrate-test: ## upgrade → downgrade → upgrade on a throwaway database
	@set -euo pipefail; \
	tmp=$$(mktemp -d); \
	db="sqlite:///$$tmp/migrate.db"; \
	echo "sqlite: $$db"; \
	VISIONOPS_DATABASE_URL="$$db" $(CLI) migrate; \
	VISIONOPS_DATABASE_URL="$$db" $(CLI) migrate --downgrade base; \
	VISIONOPS_DATABASE_URL="$$db" $(CLI) migrate; \
	rm -rf "$$tmp"; \
	if [ -n "$${VISIONOPS_TEST_DATABASE_URL:-}" ]; then \
		echo "postgres: $$VISIONOPS_TEST_DATABASE_URL"; \
		VISIONOPS_DATABASE_URL="$$VISIONOPS_TEST_DATABASE_URL" $(CLI) migrate; \
		VISIONOPS_DATABASE_URL="$$VISIONOPS_TEST_DATABASE_URL" $(CLI) migrate --downgrade base; \
		VISIONOPS_DATABASE_URL="$$VISIONOPS_TEST_DATABASE_URL" $(CLI) migrate; \
	else \
		echo "postgres: skipped (set VISIONOPS_TEST_DATABASE_URL to run it)"; \
	fi; \
	echo "migrate-test ok"

prune: ## Apply the retention policy
	@$(CLI) prune

bench: ## Measure inference latency and pipeline FPS on this host
	@$(CLI) bench

demo: ## Seed a hardware-free demo wired to the synthetic fixture video
	@$(CLI) demo

# --------------------------------------------------------------------------
# Development
# --------------------------------------------------------------------------

dev: ## Run the API (:8000) and the web app (:5173) with hot reload
	@trap 'kill $$API_PID $$WEB_PID 2>/dev/null' EXIT INT TERM; \
	$(CLI) serve --reload & API_PID=$$!; \
	$(NPM) run dev & WEB_PID=$$!; \
	wait

dev\:fixture: demo ## Create the fixture camera, then run the app
	@$(MAKE) --no-print-directory dev

# --------------------------------------------------------------------------
# Quality gates
# --------------------------------------------------------------------------

lint: ## Ruff lint + format check (what CI runs)
	$(UV) ruff check backend tests scripts
	$(UV) ruff format --check backend tests scripts

format: ## Apply ruff format to the Python sources
	$(UV) ruff format backend tests scripts

typecheck: ## mypy over the backend
	$(UV) mypy

test: ## Backend suite: unit, cv, integration, api, security
	$(UV) pytest -q

test-cov: ## Backend suite with coverage and the PRD §48 coverage gates
	$(UV) pytest -q --cov --cov-report=term-missing
	$(UV) coverage json -o .coverage.json
	$(UV) python scripts/check_coverage.py .coverage.json

test-cv: ## CV pipeline tests only (deterministic, no model weights needed)
	$(UV) pytest tests/cv -q

test-security: ## Security suite (auth, rate limits, path containment, redaction)
	$(UV) pytest tests/security -q

test-model: ## Real-model suite (requires weights; skipped when absent)
	$(UV) pytest -m model -q

test-frontend: ## Frontend unit tests with the coverage gate (vitest)
	$(NPM) run test:coverage

test-e2e: ## Playwright end-to-end against a real backend (installs chromium)
	@if [ ! -x .venv/bin/python ]; then echo "run 'make install' first"; exit 1; fi
	@cd frontend && npx playwright install --with-deps chromium
	$(NPM) run test:e2e

test-all: lint typecheck test test-frontend test-e2e test-model ## Everything runnable locally

audit: ## Audit backend and frontend dependencies for known vulnerabilities
	uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/visionops-requirements.txt
	uvx --from pip-audit pip-audit -r /tmp/visionops-requirements.txt
	$(NPM) audit --omit=dev

build: build-web build-wheel ## Production build: web bundle + python wheel

build-web: ## Frontend production bundle (tsc + vite)
	$(NPM) run build

build-wheel: ## Python wheel + sdist
	uv build

clean: ## Remove caches and build output (never touches .env, data/ or models/)
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml
	rm -rf frontend/dist frontend/test-results frontend/playwright-report frontend/coverage
	rm -rf dist build *.egg-info
	find . -name '__pycache__' -type d -prune \
		-not -path './.venv/*' -not -path './frontend/node_modules/*' -exec rm -rf {} +
