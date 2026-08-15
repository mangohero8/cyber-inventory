# Every command you need, in one place.
#
# The real purpose of a Makefile on a team project is that CI and humans run
# the *same* commands. When `make test` is what runs locally and what runs in
# the pipeline, "passes on my machine but fails in CI" mostly disappears.

.PHONY: help install test lint fmt run build run-container smoke clean

# Provenance, computed the same way CI computes it.
GIT_COMMIT ?= $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
BUILD_TIME ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
IMAGE      ?= cyber-inventory
TAG        ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo local)

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install dev dependencies
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements-dev.txt

test:  ## Run the test suite with coverage
	.venv/bin/pytest --cov=app --cov-report=term-missing

lint:  ## Check code style and security rules
	.venv/bin/ruff check .

fmt:  ## Auto-fix what can be auto-fixed
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

run:  ## Run locally with hot reload, provenance from the current commit
	GIT_COMMIT=$(GIT_COMMIT) BUILD_TIME=$(BUILD_TIME) ENVIRONMENT=local \
		.venv/bin/uvicorn app.main:app --reload --port 8080

build:  ## Build the container image, stamped with the current commit
	docker build \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_TIME=$(BUILD_TIME) \
		-t $(IMAGE):$(TAG) \
		-t $(IMAGE):latest \
		.
	@echo "built $(IMAGE):$(TAG)"

run-container:  ## Run the built image
	docker run --rm -p 8080:8080 -e ENVIRONMENT=docker $(IMAGE):latest

smoke:  ## Verify a running instance is alive AND is the expected commit
	@echo "--- health ---"
	@curl -sf localhost:8080/healthz || (echo "FAILED: not healthy"; exit 1)
	@echo "\n--- version ---"
	@curl -sf localhost:8080/version
	@echo ""
	@curl -sf localhost:8080/version | grep -q '"commit": *"unknown"' \
		&& (echo "FAILED: no build provenance -- image was not built by CI"; exit 1) \
		|| echo "OK: provenance present"

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
