SHELL := /bin/bash
TF    := terraform -chdir=infra/app

.PHONY: help install lint fmt test tf-test check bootstrap init plan deploy destroy outputs user token smoke

help: ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## install dev dependencies
	pip install -r requirements-dev.txt

lint: ## ruff lint + format check, terraform fmt check
	ruff check . && ruff format --check .
	terraform fmt -check -recursive infra

fmt: ## auto-format python and terraform
	ruff format . && ruff check --fix .
	terraform fmt -recursive infra

test: ## python unit + integration tests (moto, no AWS needed)
	python3 -m pytest

tf-test: ## terraform validate + offline contract tests (mock providers)
	$(TF) init -backend=false -input=false >/dev/null && $(TF) validate && $(TF) test

check: lint test tf-test ## everything CI runs

bootstrap: ## one-time: state bucket + GitHub OIDC deploy role (needs REPO=owner/name)
	@test -n "$(REPO)" || (echo "usage: make bootstrap REPO=owner/name" && exit 1)
	terraform -chdir=infra/bootstrap init -input=false
	terraform -chdir=infra/bootstrap apply -var github_repository=$(REPO)

init: ## init the app stack against the remote state created by bootstrap
	$(TF) init -input=false -reconfigure -backend-config=backend.hcl

plan: ## terraform plan
	$(TF) plan

deploy: ## terraform apply
	$(TF) apply

destroy: ## remove the stack (delete networks through the API first!)
	$(TF) destroy

outputs: ## show stack outputs
	$(TF) output

user: ## create a user: make user EMAIL=me@example.com
	python scripts/netloom_auth.py add-user --email $(EMAIL)

token: ## print an access token: make token EMAIL=me@example.com
	@python scripts/netloom_auth.py login --email $(EMAIL)

smoke: ## end-to-end test against the deployed API: make smoke EMAIL=me@example.com
	API_URL=$$($(TF) output -raw api_url) TOKEN=$$(python scripts/netloom_auth.py login --email $(EMAIL)) ./scripts/smoke_test.sh
