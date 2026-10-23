SHELL := /bin/bash
TF    := terraform -chdir=infra/app
TFL   := terraform -chdir=infra/localstack
TFM   := terraform -chdir=infra/modules/netloom
COMPOSE := docker compose -f infra/localstack/docker-compose.yml

.PHONY: help install lint fmt test tf-test check bootstrap init plan deploy destroy outputs user token smoke \
        local-up local-deploy local-smoke local-e2e local-destroy local-down

help: ## list targets
	@grep -E '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

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

tf-test: ## terraform validate (all roots) + offline module contract tests (mock providers)
	$(TFM) init -input=false >/dev/null && $(TFM) validate && $(TFM) test
	$(TF) init -backend=false -input=false >/dev/null && $(TF) validate
	$(TFL) init -input=false >/dev/null && $(TFL) validate

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

# ----------------------------------------------------------------------------- LocalStack
# Same module, deployed to an emulator. Needs Docker and (for LocalStack >= 2026.03)
# LOCALSTACK_AUTH_TOKEN - the free Hobby plan is enough.

local-up: ## start LocalStack and wait until healthy
	$(COMPOSE) up -d --wait

local-deploy: ## terraform apply against LocalStack
	$(TFL) init -input=false >/dev/null
	$(TFL) apply -auto-approve -input=false

local-smoke: ## end-to-end: plan, create, poll, verify EC2, retrieve, delete
	python3 scripts/local_smoke.py

local-e2e: local-up local-deploy local-smoke ## all of the above in one go

local-destroy: ## remove the stack from LocalStack
	$(TFL) destroy -auto-approve -input=false

local-down: ## stop LocalStack and forget its state
	$(COMPOSE) down -v
	-docker ps -aq --filter "name=lambda-netloom-local" | xargs -r docker rm -f >/dev/null
	rm -f infra/localstack/terraform.tfstate infra/localstack/terraform.tfstate.backup
