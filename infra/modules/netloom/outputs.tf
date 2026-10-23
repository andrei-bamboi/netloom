output "api_url" {
  description = "Base URL of the API (null when enable_edge = false)."
  value       = one(aws_apigatewayv2_stage.default[*].invoke_url)
}

output "user_pool_id" {
  value = one(aws_cognito_user_pool.users[*].id)
}

output "user_client_id" {
  description = "App client for humans (USER_PASSWORD_AUTH / SRP)."
  value       = one(aws_cognito_user_pool_client.humans[*].id)
}

output "automation_client_id" {
  description = "App client for machines (OAuth2 client_credentials)."
  value       = one(aws_cognito_user_pool_client.machines[*].id)
}

output "automation_client_secret" {
  value     = one(aws_cognito_user_pool_client.machines[*].client_secret)
  sensitive = true
}

output "token_url" {
  description = "OAuth2 token endpoint for the client_credentials flow."
  value       = var.enable_edge ? "https://${aws_cognito_user_pool_domain.oauth[0].domain}.auth.${local.region}.amazoncognito.com/oauth2/token" : null
}

output "oauth_scope" {
  value = var.enable_edge ? "${aws_cognito_resource_server.api[0].identifier}/api" : null
}

output "api_function_name" {
  description = "API Lambda - invoke directly when the edge is disabled."
  value       = aws_lambda_function.api.function_name
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.lifecycle.arn
}

output "table_name" {
  value = aws_dynamodb_table.ledger.name
}

output "region" {
  value = local.region
}
