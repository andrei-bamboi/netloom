output "api_url" {
  description = "Base URL of the API."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "user_pool_id" {
  value = aws_cognito_user_pool.users.id
}

output "user_client_id" {
  description = "App client for humans (USER_PASSWORD_AUTH / SRP)."
  value       = aws_cognito_user_pool_client.humans.id
}

output "automation_client_id" {
  description = "App client for machines (OAuth2 client_credentials). Secret: see automation_client_secret."
  value       = aws_cognito_user_pool_client.machines.id
}

output "automation_client_secret" {
  value     = aws_cognito_user_pool_client.machines.client_secret
  sensitive = true
}

output "token_url" {
  description = "OAuth2 token endpoint for the client_credentials flow."
  value       = "https://${aws_cognito_user_pool_domain.oauth.domain}.auth.${var.region}.amazoncognito.com/oauth2/token"
}

output "oauth_scope" {
  value = "${aws_cognito_resource_server.api.identifier}/api"
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.lifecycle.arn
}

output "table_name" {
  value = aws_dynamodb_table.ledger.name
}

output "region" {
  value = var.region
}
