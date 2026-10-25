# netloom API reference

| | Base URL | Swagger UI | Token |
|---|---|---|---|
| **LocalStack** | `http://localhost:8080` (`make local-gateway`) | <http://localhost:8080/docs> | dev token `local-<name>`, e.g. `local-alice` |
| **AWS** | `terraform -chdir=infra/app output -raw api_url` | `<api_url>/docs` | Cognito access token (`make token EMAIL=…`) |

Every request except the public ones sends `Authorization: Bearer <token>`. The machine-readable contract is [openapi.yaml](openapi.yaml), served live at `/v1/openapi.json`.

![Swagger UI](swagger-ui.png)

## Endpoints

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /docs` | public | **Swagger UI**: interactive docs, with *Authorize* → paste token → *Try it out* |
| `GET /v1/openapi.json` | public | the OpenAPI 3.1 document Swagger UI reads |
| `GET /v1/health` | public | liveness |
| `GET /v1/me` | JWT | shows the identity the API sees (useful in the auth demo) |
| `POST /v1/plans` | JWT | **dry run**: validates a definition and returns the computed subnets without creating anything |
| `POST /v1/networks` | JWT | creates a network → `202` + `Location`; supports the `Idempotency-Key` header |
| `GET /v1/networks?limit&cursor&status` | JWT | lists networks, newest first, with cursor pagination |
| `GET /v1/networks/{id}` | JWT | network + subnets + AWS ids + status |
| `GET /v1/networks/{id}/subnets` | JWT | subnets with `subnet_id`, AZ and usable IPs |
| `GET /v1/networks/{id}/events` | JWT | audit trail: who requested what, and what the workflow did |
| `DELETE /v1/networks/{id}` | JWT | tears it down. The record stays, with status `DELETED` |

## Network lifecycle

`POST /v1/networks` returns **202** right away with status `QUEUED` and a `Location` header. Poll `GET /v1/networks/{id}` until the status settles:

```
QUEUED → PROVISIONING → AVAILABLE          DELETE → DECOMMISSIONING → DELETED
                     ↘ FAILED (rolled back; DELETE cleans any leftovers)
```

Every response that describes a network carries its AWS ids (`vpc_id`, `internet_gateway_id`, `public_route_table_id`, and each subnet's `subnet_id` and `availability_zone`). It also includes `created_by`, `error` when status is `FAILED`, and `links` to related resources.

## Request conventions

| Topic | Behaviour |
|---|---|
| **Idempotency** | Send `Idempotency-Key: <8-64 chars>` on `POST /v1/networks`. A retry with the same key and body returns the original network, with the response header `idempotent-replayed: true`. The same key with a different body gets `422`. Keys are scoped per user and kept for 24 h |
| **Pagination** | `GET /v1/networks?limit=1..100&cursor=…&status=AVAILABLE` returns networks newest first. Pass the returned `next_cursor` to fetch the next page |
| **Errors** | `{"error": {"code", "message", "details"?}}`. Codes: `400` bad_request · `401` unauthenticated · `404` not_found · `409` conflict, e.g. deleting while provisioning · `422` validation_failed, which lists **every** problem with its field path |
| **Limits** | Body up to 32 KB · at most 16 subnets per network · API throttled at 20 rps, burst 40 (AWS) |

## Describing subnets

**Layout** (tiers × zones). The planner picks consecutive, non-overlapping blocks:

```json
{ "name": "payments-dev", "cidr": "10.20.0.0/16",
  "layout": { "tiers": ["public", "private"], "zones": 3, "prefix": 20 },
  "tags": { "team": "payments" } }
```

→ `public-a 10.20.0.0/20`, `public-b 10.20.16.0/20`, `public-c 10.20.32.0/20`, `private-a 10.20.48.0/20`, … If you leave out `prefix`, it uses the largest size that fits.

**Explicit**:

```json
{ "name": "lab", "cidr": "172.16.0.0/20",
  "subnets": [
    { "name": "web",  "cidr": "172.16.0.0/24",  "zone": "a", "tier": "public"  },
    { "name": "db-a", "cidr": "172.16.8.0/23",  "zone": "a" },
    { "name": "db-b", "cidr": "172.16.10.0/23", "zone": "b" } ] }
```

The planner reports **every** problem at once, each with its field path:

```json
{ "error": { "code": "validation_failed", "message": "The request is not a valid network definition.",
  "details": [ { "field": "subnets[1].cidr", "message": "10.0.0.128/25 overlaps 10.0.0.0/24 (a)" },
               { "field": "subnets[2].cidr", "message": "10.1.0.0/24 is outside the VPC range 10.0.0.0/16" } ] } }
```

What the planner enforces:

- The VPC block must be RFC 1918 and between /16 and /28.
- Subnets must sit inside the VPC, must not overlap, and their names must be unique.
- At most 16 subnets (configurable).
- Tag keys `aws:*`, `netloom:*` and `Name` are reserved.

Zones `a`–`f` are *logical*. They map onto the region's available AZs in sorted order, so the same request works in any region.

**Public tier**: the subnet gets `MapPublicIpOnLaunch`, and the VPC gets an internet gateway and a route table with `0.0.0.0/0 → IGW` associated to the public subnets. **Private tier** gets no internet path. A NAT gateway was left out on purpose to keep the demo free of hourly charges.

## Authentication & authorization

- **Who can get a token.** Self sign-up is disabled. Users are invited through Terraform (`invited_users`, and Cognito e-mails them a temporary password) or created by an admin with `make user`. Passwords need at least 12 characters with mixed case and digits. Users can turn on TOTP MFA.
- **Two token types, both accepted:**
  - *Humans*: e-mail + password → access token (the `cli` app client).
  - *Machines*: OAuth2 `client_credentials` with the `netloom/api` scope (the `automation` app client). Use this for CI jobs and other services.
- **Verification** happens in API Gateway's JWT authorizer: signature, issuer, expiry and audience. Requests without a valid token get `401` and never reach Lambda. The Lambda also refuses calls that arrive without claims, as defence in depth.
- **Authorization** is open to all authenticated users, as required. Bob can read and delete Alice's network. A test asserts this. Accountability comes from the audit trail instead: every event records `actor.sub` and `actor.username`.
- **Least privilege in AWS.** The worker can *create* EC2 resources only if the request carries the tag `netloom:managed=true`. It can *modify or delete* only resources that already carry that tag. It can't touch any VPC netloom didn't create.

On **LocalStack** there's no Cognito. `local-gateway` emulates the JWT authorizer with dev tokens: `Bearer local-<name>` is accepted, and anything else gets `401`. It's a development aid, not a security boundary.

## Walkthrough with curl (LocalStack)

```bash
API=http://localhost:8080
AUTH='Authorization: Bearer local-alice'

curl -s -w ' %{http_code}\n' $API/v1/networks                       # → {"message":"Unauthorized"} 401
curl -s -H "$AUTH" $API/v1/me | jq                                   # who the API thinks you are

curl -s -H "$AUTH" -H 'Content-Type: application/json' $API/v1/plans \
  -d '{"name":"demo","cidr":"10.42.0.0/16","layout":{"tiers":["public","private"],"zones":2,"prefix":24}}' | jq .plan.subnets

ID=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-0001' $API/v1/networks \
  -d '{"name":"demo","cidr":"10.42.0.0/16","layout":{"tiers":["public","private"],"zones":2,"prefix":24}}' | jq -r .id)

curl -s -H "$AUTH" $API/v1/networks/$ID | jq '{status, vpc_id, internet_gateway_id}'   # QUEUED → … → AVAILABLE
curl -s -H "$AUTH" $API/v1/networks/$ID/subnets | jq -r '.items[] | [.name,.cidr,.availability_zone,.subnet_id] | @tsv'
curl -s -H "$AUTH" $API/v1/networks/$ID/events  | jq -r '.items[] | [.type,.actor.username,.message] | @tsv'
curl -s -H 'Authorization: Bearer local-bob' -X DELETE $API/v1/networks/$ID | jq .status   # → DECOMMISSIONING
```

To check the resources directly in emulated EC2, always pass fake credentials so the AWS CLI can't fall back to a real profile:

```bash
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_REGION=eu-central-1 \
  aws --endpoint-url http://localhost:4566 ec2 describe-subnets \
  --filters Name=tag:netloom:network-id,Values=$ID --query 'Subnets[].[CidrBlock,AvailabilityZone,SubnetId]' --output table
```

On AWS, use the same calls with `API=<api_url>` and a real token: `TOKEN=$(python scripts/netloom_auth.py login --email me@example.com)`. For machines, use `python scripts/netloom_auth.py machine-token` instead.

## Keeping the spec honest

- `docs/openapi.yaml` is the source you edit. `make openapi` compiles it to `src/netloom/openapi.json`, which ships inside the Lambda. The Lambda runtime has no YAML parser, so the API serves the compiled JSON.
- The API serves the spec with a relative server URL (`/`), so "Try it out" always calls the host that served it: API Gateway, a custom domain, or the local gateway.
- `tests/test_openapi.py` fails if:
  - the spec isn't valid OpenAPI 3.1;
  - the compiled JSON is stale;
  - the routes in the **spec, the Python route table and Terraform** aren't identical;
  - any of those three disagree about which routes are public;
  - an operation is missing an `operationId`, tag or summary.
