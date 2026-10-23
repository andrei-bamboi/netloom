# Offline contract tests for the stack - no AWS credentials needed.
#   cd infra/app && terraform init -backend=false && terraform test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:eu-central-1:123456789012:function:netloom-dev-worker" }
  }
}

mock_provider "random" {}

run "routes_are_protected_except_health" {
  command = plan

  assert {
    condition     = length(aws_apigatewayv2_route.this) == 9
    error_message = "Unexpected number of routes."
  }
  assert {
    condition     = aws_apigatewayv2_route.this["GET /v1/health"].authorization_type == "NONE"
    error_message = "Health must be public."
  }
  assert {
    condition = alltrue([
      for k, r in aws_apigatewayv2_route.this : r.authorization_type == "JWT" if k != "GET /v1/health"
    ])
    error_message = "Every other route must require a JWT."
  }
}

run "self_signup_is_disabled" {
  command = plan

  assert {
    condition     = aws_cognito_user_pool.users.admin_create_user_config[0].allow_admin_create_user_only
    error_message = "Only administrators may create users."
  }
}

run "workflow_has_rollback_wiring" {
  command = plan

  assert {
    condition     = local.definition.StartAt == "Route"
    error_message = "Workflow must start with the operation router."
  }
  assert {
    condition = alltrue([
      for s in ["Begin", "CreateVpc", "CreateSubnets", "ConfigureRouting", "Finalize"] :
      local.definition.States[s].Catch[0].Next == "Rollback"
    ])
    error_message = "Every create step must roll back on failure."
  }
  assert {
    condition     = local.definition.States.Rollback.Next == "MarkFailed" && local.definition.States.MarkFailed.Next == "Failed"
    error_message = "Rollback must end in a failed execution with the record marked FAILED."
  }
  assert {
    condition     = local.definition.States.Teardown.Next == "Retire" && local.definition.States.Teardown.Catch[0].Next == "MarkFailed"
    error_message = "Teardown must not trigger rollback."
  }
  assert {
    condition = alltrue([
      for name, st in local.definition.States : contains(keys(local.definition.States), st.Next) if can(st.Next)
    ])
    error_message = "A state points at a state that does not exist."
  }
}

run "table_is_pay_per_request_with_ttl" {
  command = plan

  assert {
    condition     = aws_dynamodb_table.ledger.billing_mode == "PAY_PER_REQUEST" && aws_dynamodb_table.ledger.ttl[0].attribute_name == "expires_at"
    error_message = "Ledger table misconfigured."
  }
}
