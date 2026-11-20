# The harness is the `k8s-tests` CLI now; this file only keeps `make <target>` working.
#
#   make up OPERATOR_REPO=/path/to/trustyai-service-operator   ==  k8s-tests up ...
#
# Every variable the old Makefile read is still read from the environment, and a
# variable given on the command line still reaches the CLI (make exports those).

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

K8S_TESTS := uv run --quiet k8s-tests

.PHONY: help cluster cluster-delete down check-operator-repo images images-all load \
	deploy deploy-registry up up-registry clean status logs \
	test test-all test-evalhub test-evalhub-generation test-evalhub-adapters test-lmeval \
	test-keep test-select lint fmt

help: ## Display this help.
	@$(K8S_TESTS) --help

cluster: ## Create the Kind cluster (idempotent).
	@$(K8S_TESTS) cluster

cluster-delete: ## Delete the Kind cluster.
	@$(K8S_TESTS) down

down: ## Delete the Kind cluster.
	@$(K8S_TESTS) down

check-operator-repo: ## Check OPERATOR_REPO looks like a trustyai-service-operator checkout.
	@$(K8S_TESTS) check-operator-repo

images: ## Build the workload and module operator images.
	@$(K8S_TESTS) images

images-all: ## Build every source image and push it to the local registry.
	@$(K8S_TESTS) images-all

load: ## Load the built images into the Kind cluster.
	@$(K8S_TESTS) load

deploy: ## Deploy the module operator and the stack under it.
	@$(K8S_TESTS) deploy

deploy-registry: ## Deploy with the images images-all pushed.
	@$(K8S_TESTS) deploy-registry

up: ## Create the cluster, build, load and deploy.
	@$(K8S_TESTS) up

up-registry: ## Create the cluster, build and push every image, and deploy.
	@$(K8S_TESTS) up-registry

clean: ## Delete the built images, the local registry and the HTML report.
	@$(K8S_TESTS) clean

status: ## Show the module, the operators, the pods, EvalHub and the emulator.
	@$(K8S_TESTS) status

logs: ## Tail the operator logs.
	@$(K8S_TESTS) logs

test: ## Run every section except the EvalHub adapters.
	@$(K8S_TESTS) test

test-all: ## Run every section, the EvalHub adapters included.
	@$(K8S_TESTS) test --section all

test-evalhub: ## Run every EvalHub check except the adapter section.
	@$(K8S_TESTS) test --section evalhub

test-evalhub-generation: ## Run only the collection generation authorization checks.
	@$(K8S_TESTS) test --section generation

test-evalhub-adapters: ## Run only the EvalHub adapter checks.
	@$(K8S_TESTS) test --section adapters

test-lmeval: ## Run the LMEvalJob checks.
	@$(K8S_TESTS) test --section lmeval

test-keep: ## Run the suite and leave every resource on the cluster.
	@$(K8S_TESTS) test --keep-resources

test-select: ## Run one selection, e.g. SELECT='-m generation' or SELECT=tests/evalhub.
	@test -n '$(strip $(SELECT))' || { \
		echo "error: SELECT is required, e.g. make test-select SELECT='-m generation'" >&2; \
		exit 2; \
	}
	@$(K8S_TESTS) test --select '$(SELECT)'

lint: ## Lint and format-check the Python sources.
	@$(K8S_TESTS) lint

fmt: ## Format and autofix the Python sources.
	@$(K8S_TESTS) fmt
