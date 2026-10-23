# Authentication: Amazon Cognito issues JWTs, API Gateway verifies them.
#
# * Humans sign in with e-mail + password (USER_PASSWORD_AUTH / SRP) and send
#   the *access token* as "Authorization: Bearer <token>".
# * Automation (CI jobs, other services) uses the OAuth2 client-credentials
#   flow on the hosted domain with the "netloom/api" scope.
#
# Authorization is intentionally flat: any valid token from either client is
# allowed on every route, as the exercise requires.

resource "aws_cognito_user_pool" "users" {
  name                     = "${local.name}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = var.protect_data ? "ACTIVE" : "INACTIVE"
  mfa_configuration        = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true # no self sign-up
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "humans" {
  name                          = "${local.name}-cli"
  user_pool_id                  = aws_cognito_user_pool.users.id
  generate_secret               = false
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  access_token_validity         = var.access_token_minutes
  id_token_validity             = var.access_token_minutes
  refresh_token_validity        = 7

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_pool_domain" "oauth" {
  domain       = "${local.name}-${random_string.suffix.result}"
  user_pool_id = aws_cognito_user_pool.users.id
}

resource "aws_cognito_resource_server" "api" {
  identifier   = var.project
  name         = "${local.name} API"
  user_pool_id = aws_cognito_user_pool.users.id

  scope {
    scope_name        = "api"
    scope_description = "Full access to the netloom API"
  }
}

resource "aws_cognito_user_pool_client" "machines" {
  name                                 = "${local.name}-automation"
  user_pool_id                         = aws_cognito_user_pool.users.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = ["${aws_cognito_resource_server.api.identifier}/api"]
  access_token_validity                = var.access_token_minutes
  supported_identity_providers         = ["COGNITO"]

  token_validity_units {
    access_token = "minutes"
  }
}

resource "aws_cognito_user" "invited" {
  for_each     = toset(var.invited_users)
  user_pool_id = aws_cognito_user_pool.users.id
  username     = each.value

  attributes = {
    email          = each.value
    email_verified = true
  }

  desired_delivery_mediums = ["EMAIL"]
}
