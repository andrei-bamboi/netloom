# One-time bootstrap, run locally with admin credentials:
#
#   cd infra/bootstrap
#   terraform init && terraform apply -var github_repository=OWNER/REPO
#
# Creates:
#   * an S3 bucket for Terraform state (versioned, encrypted, S3-native locking)
#   * the GitHub Actions OIDC identity provider (unless it already exists)
#   * a deploy role GitHub Actions assumes - no long-lived AWS keys anywhere
#   * ../app/backend.hcl so `terraform init -backend-config=backend.hcl` just works
#
# Its own state stays local on purpose (chicken-and-egg); it is tiny and can be
# re-imported if lost.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "project" {
  type    = string
  default = "netloom"
}

variable "github_repository" {
  description = "OWNER/REPO allowed to deploy."
  type        = string
}

variable "create_oidc_provider" {
  description = "Set false if the account already has token.actions.githubusercontent.com."
  type        = bool
  default     = true
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.project, ManagedBy = "terraform-bootstrap" }
  }
}

data "aws_caller_identity" "me" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.me.account_id
  partition  = data.aws_partition.current.partition
  arn        = "arn:${local.partition}"
}

# --------------------------------------------------------------------------- state bucket

resource "aws_s3_bucket" "state" {
  bucket        = "${var.project}-tfstate-${local.account_id}-${var.region}"
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "tls_only" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# --------------------------------------------------------------------------- GitHub OIDC

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  oidc_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "${local.arn}:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "trust_github" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Only the main branch and the protected "aws" environment may deploy.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repository}:ref:refs/heads/main",
        "repo:${var.github_repository}:environment:aws",
      ]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.project}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.trust_github.json
  max_session_duration = 3600
}

# Scoped to what infra/app manages: everything named "<project>-*".
data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "State"
    actions   = ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
  }
  statement {
    sid = "Roles"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole", "iam:TagRole", "iam:UntagRole",
      "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:UpdateAssumeRolePolicy", "iam:PassRole",
    ]
    resources = ["${local.arn}:iam::${local.account_id}:role/${var.project}-*"]
  }
  statement {
    sid = "Services"
    actions = [
      "lambda:*", "apigateway:*", "states:*", "dynamodb:*", "cognito-idp:*", "logs:*", "xray:*",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "Identity"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# --------------------------------------------------------------------------- hand-off

resource "local_file" "backend" {
  filename = "${path.module}/../app/backend.hcl"
  content  = <<-EOT
    bucket       = "${aws_s3_bucket.state.bucket}"
    key          = "${var.project}/app.tfstate"
    region       = "${var.region}"
    encrypt      = true
    use_lockfile = true
  EOT
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  description = "Store as the AWS_DEPLOY_ROLE_ARN repository variable in GitHub."
  value       = aws_iam_role.deploy.arn
}
