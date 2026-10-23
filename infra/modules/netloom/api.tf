locals {
  # routeKey => requires a JWT?  Mirrors ROUTES in src/netloom/api.py.
  routes = {
    "GET /v1/health"                = false
    "GET /v1/me"                    = true
    "POST /v1/plans"                = true
    "POST /v1/networks"             = true
    "GET /v1/networks"              = true
    "GET /v1/networks/{id}"         = true
    "GET /v1/networks/{id}/subnets" = true
    "GET /v1/networks/{id}/events"  = true
    "DELETE /v1/networks/{id}"      = true
  }
}

resource "aws_apigatewayv2_api" "http" {
  count         = local.edge_count
  name          = "${local.name}-api"
  protocol_type = "HTTP"
  description   = "netloom - authenticated VPC provisioning API"

  dynamic "cors_configuration" {
    for_each = length(var.cors_allowed_origins) > 0 ? [1] : []
    content {
      allow_origins = var.cors_allowed_origins
      allow_methods = ["GET", "POST", "DELETE", "OPTIONS"]
      allow_headers = ["authorization", "content-type", "idempotency-key"]
      max_age       = 3600
    }
  }
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  count            = local.edge_count
  api_id           = aws_apigatewayv2_api.http[0].id
  name             = "cognito-jwt"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = "https://${aws_cognito_user_pool.users[0].endpoint}"
    audience = [aws_cognito_user_pool_client.humans[0].id, aws_cognito_user_pool_client.machines[0].id]
  }
}

resource "aws_apigatewayv2_integration" "api" {
  count                  = local.edge_count
  api_id                 = aws_apigatewayv2_api.http[0].id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 10000
}

resource "aws_apigatewayv2_route" "this" {
  for_each           = var.enable_edge ? local.routes : {}
  api_id             = aws_apigatewayv2_api.http[0].id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.api[0].id}"
  authorization_type = each.value ? "JWT" : "NONE"
  authorizer_id      = each.value ? aws_apigatewayv2_authorizer.cognito[0].id : null
}

resource "aws_cloudwatch_log_group" "access" {
  count             = local.edge_count
  name              = "/aws/apigateway/${local.name}-access"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "default" {
  count       = local.edge_count
  api_id      = aws_apigatewayv2_api.http[0].id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.api_throttle_rate
    throttling_burst_limit = var.api_throttle_burst
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access[0].arn
    format = jsonencode({
      requestId   = "$context.requestId"
      ip          = "$context.identity.sourceIp"
      user        = "$context.authorizer.claims.username"
      sub         = "$context.authorizer.claims.sub"
      route       = "$context.routeKey"
      status      = "$context.status"
      latencyMs   = "$context.responseLatency"
      authError   = "$context.authorizer.error"
      integration = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_lambda_permission" "apigw" {
  count         = local.edge_count
  statement_id  = "AllowHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http[0].execution_arn}/*/*"
}
