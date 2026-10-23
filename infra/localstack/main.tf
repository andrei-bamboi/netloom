# Root stack for LocalStack: the same ../modules/netloom deployed against an
# emulated AWS on http://localhost:4566. Nothing here touches a real account.
#
#   make local-up        # start LocalStack (docker compose)
#   make local-deploy    # terraform apply against it
#   make local-smoke     # create -> poll -> read -> delete through the API Lambda
#   make local-down
#
# Free LocalStack (Hobby / former Community) has no Cognito and no API Gateway
# v2, so the edge is off by default and the API Lambda is invoked directly with
# the same event API Gateway would send. With a plan that includes those
# services, run with -var enable_edge=true to deploy the full stack.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.30"
    }
  }

  # Local state on purpose: the emulator is disposable, so is its state.
  backend "local" {
    path = "terraform.tfstate"
  }
}

variable "endpoint" {
  description = "LocalStack edge URL."
  type        = string
  default     = "http://localhost:4566"
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "enable_edge" {
  description = "Deploy Cognito + API Gateway v2 (requires a LocalStack plan that includes them)."
  type        = bool
  default     = false
}

provider "aws" {
  region     = var.region
  access_key = "test"
  secret_key = "test"

  # Do not reach out to real AWS for anything.
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true

  endpoints {
    apigateway     = var.endpoint
    apigatewayv2   = var.endpoint
    cloudwatch     = var.endpoint
    cloudwatchlogs = var.endpoint
    cognitoidp     = var.endpoint
    dynamodb       = var.endpoint
    ec2            = var.endpoint
    iam            = var.endpoint
    lambda         = var.endpoint
    s3             = var.endpoint
    sfn            = var.endpoint
    sts            = var.endpoint
    xray           = var.endpoint
  }

  default_tags {
    tags = {
      Project     = "netloom"
      Environment = "local"
      ManagedBy   = "terraform"
    }
  }
}

module "netloom" {
  source = "../modules/netloom"

  environment        = "local"
  enable_edge        = var.enable_edge
  log_retention_days = 1
}

output "endpoint" { value = var.endpoint }
output "region" { value = module.netloom.region }
output "api_function_name" { value = module.netloom.api_function_name }
output "state_machine_arn" { value = module.netloom.state_machine_arn }
output "table_name" { value = module.netloom.table_name }
output "api_url" { value = module.netloom.api_url }
output "user_pool_id" { value = module.netloom.user_pool_id }
output "user_client_id" { value = module.netloom.user_client_id }
