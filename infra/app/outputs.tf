output "api_url" { value = module.netloom.api_url }
output "user_pool_id" { value = module.netloom.user_pool_id }
output "user_client_id" { value = module.netloom.user_client_id }
output "automation_client_id" { value = module.netloom.automation_client_id }
output "token_url" { value = module.netloom.token_url }
output "oauth_scope" { value = module.netloom.oauth_scope }
output "api_function_name" { value = module.netloom.api_function_name }
output "state_machine_arn" { value = module.netloom.state_machine_arn }
output "table_name" { value = module.netloom.table_name }
output "region" { value = module.netloom.region }

output "automation_client_secret" {
  value     = module.netloom.automation_client_secret
  sensitive = true
}
