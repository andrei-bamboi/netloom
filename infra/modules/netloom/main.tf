data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  name       = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.region

  # The "edge" is the public face of the API: Cognito + API Gateway HTTP API.
  # It can be switched off for emulators that lack those services (LocalStack
  # free tier); the core - Lambdas, workflow, ledger - is always deployed.
  edge_count = var.enable_edge ? 1 : 0
}

resource "random_string" "suffix" {
  count   = local.edge_count
  length  = 6
  upper   = false
  special = false
}

# --------------------------------------------------------------------------- storage

resource "aws_dynamodb_table" "ledger" {
  name                        = "${local.name}-ledger"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = var.protect_data

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "kind"
    type = "S"
  }
  attribute {
    name = "id"
    type = "S"
  }

  global_secondary_index {
    name            = "by_kind"
    projection_type = "ALL"

    key_schema {
      attribute_name = "kind"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "id"
      key_type       = "RANGE"
    }
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# --------------------------------------------------------------------------- code

data "archive_file" "code" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src"
  output_path = "${path.root}/.build/netloom.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = toset(["api", "worker"])
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "api" {
  function_name    = "${local.name}-api"
  description      = "netloom HTTP API"
  role             = aws_iam_role.api.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "netloom.api.handler"
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  memory_size      = 256
  timeout          = 10

  environment {
    variables = {
      TABLE_NAME        = aws_dynamodb_table.ledger.name
      STATE_MACHINE_ARN = aws_sfn_state_machine.lifecycle.arn
      MAX_SUBNETS       = tostring(var.max_subnets_per_network)
      LOG_LEVEL         = "INFO"
    }
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [aws_cloudwatch_log_group.fn]
}

resource "aws_lambda_function" "worker" {
  function_name    = "${local.name}-worker"
  description      = "netloom provisioning steps (invoked by Step Functions)"
  role             = aws_iam_role.worker.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "netloom.provisioner.handler"
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  memory_size      = 256
  timeout          = 90

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.ledger.name
      LOG_LEVEL  = "INFO"
    }
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [aws_cloudwatch_log_group.fn]
}
