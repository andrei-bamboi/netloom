# Running, deploying and demoing netloom

## Local: LocalStack (no AWS account)

No AWS account is needed. Terraform deploys the **same module** that would go to AWS into LocalStack, then you drive it through Swagger UI or `curl`.

### Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | **3.12+** (3.13 recommended) | macOS's built-in `python3` (Xcode, 3.9) is too old: `brew install python@3.13` |
| Terraform | 1.10+ | `brew install terraform` |
| Docker | Docker Desktop running | LocalStack runs each Lambda in its own container through the Docker socket |
| LocalStack | free auth token **or** the 4.14 image | see step 2 |
| `curl`, `jq` | any | only for the curl examples |

### 1. Set up Python

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
make check          # optional: lint + 40 pytest tests + terraform validate/test, all offline
```

### 2. Choose the LocalStack image

```bash
# Option A: current LocalStack (2026.03+ needs a token; the free Hobby plan is enough)
export LOCALSTACK_AUTH_TOKEN=...            # app.localstack.cloud → Auth Tokens

# Option B: last Community build, needs no token
export LOCALSTACK_IMAGE=localstack/localstack:4.14
```

### 3. Deploy and test end to end

```bash
make local-e2e      # = local-up (start LocalStack) + local-deploy (terraform apply) + local-smoke
```

`local-smoke` creates a network, then:

- waits for the workflow to reach `AVAILABLE`;
- checks that the VPC and subnets really exist in emulated EC2;
- reads back the stored data;
- replays a request with the same idempotency key;
- has a second user read and delete the network;
- prints the audit trail.

The API Lambda, Step Functions workflow, worker Lambda, DynamoDB and EC2 all run inside LocalStack.

### 4. Swagger UI

```bash
make local-gateway  # keep it running → http://localhost:8080/docs
```

1. Open <http://localhost:8080/docs>.
2. Click **Authorize** and enter `local-alice`.
3. Use **Try it out** on `POST /v1/networks`, then on `GET /v1/networks/{id}` with the returned id.
4. Authorize as `local-bob` to show that any authenticated user can read or delete Alice's network.

### 5. Or with curl

See the [curl walkthrough in API.md](API.md#walkthrough-with-curl-localstack).

### 6. Stop

```bash
make local-down     # stops LocalStack and deletes its state; nothing persists
```

### What's different from AWS

LocalStack's free tier has no **Cognito** and no **API Gateway v2**. So `infra/localstack` sets `enable_edge = false`, which deploys everything except those two.

`local-gateway` takes their place:

- it maps each HTTP request to the route, the same way API Gateway does;
- it invokes the deployed API Lambda with the identical payload-v2 event;
- it emulates the JWT authorizer with **dev tokens**: `Bearer local-<name>` is accepted, and anything else on a protected route gets `401`.

This is a development aid, not a security boundary. Real token verification is configured in Terraform and covered by the module's contract tests. If your LocalStack plan includes Cognito and API Gateway v2, run `terraform -chdir=infra/localstack apply -var enable_edge=true` to deploy them too.

### Safety: LocalStack never touches AWS

The LocalStack stack:

- hard-codes fake credentials (`test`/`test`);
- points every AWS endpoint at `http://localhost:4566`;
- keeps its state in a local file.

`local_smoke.py` and `local_gateway.py` do the same. So even if your shell's default AWS profile points at a real account, `make local-*` can't reach it. Still, it's good hygiene not to keep real credentials under `[default]` in `~/.aws/credentials`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `ImportError: cannot import name 'UTC' from 'datetime'` or other import errors | Your venv uses an old Python (macOS default 3.9). Recreate it with `python3.13 -m venv .venv` |
| `make: *** No rule to make target 'local-e2e'` | You're on an old checkout. Pull the latest `main` |
| LocalStack exits right after start / license error | Set `LOCALSTACK_AUTH_TOKEN`, or use `LOCALSTACK_IMAGE=localstack/localstack:4.14` |
| `port is already allocated` (4566 or 8080) | Another LocalStack or gateway is running: `make local-down`, or `docker ps` and stop it. For the gateway: `python3 scripts/local_gateway.py --port 8081` |
| `Table already exists` / `ResourceInUse` on deploy | Leftover state from an earlier run: `make local-down && make local-e2e` |
| Lambda invoke hangs on first call | First run pulls the Lambda runtime image. Give it a minute; later calls are fast |
| Swagger UI page is blank | It loads its assets from cdn.jsdelivr.net. Check that your network allows it |
| `curl` to localhost fails behind a corporate proxy | `export NO_PROXY=localhost,127.0.0.1` |

## AWS (optional)

Use an AWS account you control, such as a personal or sandbox account. Before running anything, check which account your terminal will act on:

```bash
export AWS_PROFILE=<your-profile>
aws sts get-caller-identity          # make sure this is the account you intend to use
```

### One-time bootstrap

```bash
make bootstrap REPO=<github-owner>/<repo>   # e.g. REPO=andreib/netloom
```

This creates:

- the Terraform state bucket (S3, versioned, with native locking);
- GitHub's OIDC identity provider;
- a deploy role that only `main` and the `aws` environment of **that repository** can assume.

It also writes `infra/app/backend.hcl`.

### Deploy from your machine

```bash
make init
cp infra/app/terraform.tfvars.example infra/app/terraform.tfvars   # region, invited users
make deploy && make outputs
```

### Deploy through GitHub Actions

1. In GitHub, create an environment named **`aws`**. You can add required reviewers to it.
2. Add these repository variables from the bootstrap output: `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION` and `TF_STATE_BUCKET`. Optionally add `NETLOOM_INVITED_USERS`, e.g. `["reviewer@example.com"]`.
3. Push to `main`. The workflow runs CI, then `plan`, `apply` and a health check covering `/v1/health`, `/docs`, `/v1/openapi.json` and the unauthenticated `401`.

Without those variables, the `deploy` workflow fails on push. That's expected if you only use LocalStack.

### Use it

```bash
make user EMAIL=me@example.com                                  # create a Cognito user
export TOKEN=$(python scripts/netloom_auth.py login --email me@example.com)
open "$(terraform -chdir=infra/app output -raw api_url)docs"     # Swagger UI → Authorize → paste $TOKEN
make smoke EMAIL=me@example.com                                 # full end-to-end check
```

For machines: `TOKEN=$(python scripts/netloom_auth.py machine-token)`.

To clean up, `DELETE` every network through the API first, because Terraform doesn't own those VPCs. Then run `make destroy`.

## Demo script (≈10 min, LocalStack)

Start with `make local-e2e`, then keep `make local-gateway` running and open <http://localhost:8080/docs>.

1. **Architecture.** Show `architecture.png`: an asynchronous API, Step Functions with rollback, the DynamoDB ledger, and one Terraform module with two roots.
2. **Auth.** Before authorizing, run `GET /v1/networks` → `401`. Authorize as `local-alice`, then `GET /v1/me`.
3. **Dry run.** `POST /v1/plans` with a layout shows the carved CIDRs. An overlapping explicit request returns a `422` listing every problem.
4. **Create.** `POST /v1/networks` → `202 QUEUED` with a `Location` header.
5. **Retrieve.** `GET /v1/networks/{id}` → `AVAILABLE` with `vpc_id`, `subnet_id`s and AZs. Cross-check with the `aws --endpoint-url` command in [API.md](API.md#walkthrough-with-curl-localstack), then show `/events`.
6. **Idempotency.** Repeat the POST with the same `Idempotency-Key` → same id, `idempotent-replayed: true`.
7. **Open authorization.** Re-authorize as `local-bob`, list networks, and see Alice's network.
8. **Teardown.** `DELETE` → `DECOMMISSIONING` → `DELETED`. The VPC is gone; the record and audit trail remain.
9. **Code quality.** Run `make check`: 40 Python tests, Terraform contract tests, and spec, code and infrastructure checked against each other.

## Operations

- **Observability**:
  - Lambda logs are structured JSON.
  - API Gateway writes JSON access logs that include the caller's `sub`.
  - X-Ray tracing is on for both Lambdas and Step Functions.
  - Workflow errors are logged in CloudWatch.
- **Throttling**: by default the stage allows 20 req/s with bursts of 40 (`api_throttle_*`).
- **Data**: DynamoDB on demand, with point-in-time recovery and encryption. Idempotency records expire through TTL. Set `protect_data=true` for deletion protection on the table and the user pool.
- **Cost at rest**: close to $0. Everything is pay-per-request, and the VPCs and subnets created are free. The deliberately absent NAT gateway would be the first real cost.

## Known limitations / next steps

- IPv4 only. Planned next: an IPv6 `/56` per VPC and a `/64` per subnet.
- One region per deployment (the API's own region).
- No NAT for private tiers. Planned as an opt-in `nat: "single" | "per-zone"`.
- `status` filtering on lists is applied after DynamoDB's page limit, so a page can come back with fewer items than `limit`. A status GSI would fix that if needed.
- If a user puts their own resources (ENIs, instances) into a netloom VPC, teardown can't delete it. Teardown then retries and ends `FAILED` with `DependencyViolation` in the events. Remove those resources and call `DELETE` again.
