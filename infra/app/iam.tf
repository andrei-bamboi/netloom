# Least-privilege roles: one per runtime component.

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assume_states" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

locals {
  table_arns = [aws_dynamodb_table.ledger.arn, "${aws_dynamodb_table.ledger.arn}/index/*"]
  ec2_arn    = "arn:${local.partition}:ec2:${var.region}:${local.account_id}"
}

# --------------------------------------------------------------------------- API function

resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "Ledger"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:ConditionCheckItem"]
    resources = local.table_arns
  }
  statement {
    sid       = "StartWorkflow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.lifecycle.arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.fn["api"].arn}:*"]
  }
  statement {
    sid       = "Tracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "api" {
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

# --------------------------------------------------------------------------- worker function

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "Ledger"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = local.table_arns
  }

  statement {
    sid       = "Discover"
    actions   = ["ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeInternetGateways", "ec2:DescribeRouteTables", "ec2:DescribeAvailabilityZones"]
    resources = ["*"] # Describe* does not support resource-level permissions
  }

  # Creation is only allowed when the request tags the resource as ours.
  statement {
    sid       = "CreateTaggedOnly"
    actions   = ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateInternetGateway", "ec2:CreateRouteTable"]
    resources = ["${local.ec2_arn}:vpc/*", "${local.ec2_arn}:subnet/*", "${local.ec2_arn}:internet-gateway/*", "${local.ec2_arn}:route-table/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/netloom:managed"
      values   = ["true"]
    }
  }

  # CreateSubnet / CreateRouteTable also authorise against the parent VPC,
  # which must itself be one of ours.
  statement {
    sid       = "UseOwnVpc"
    actions   = ["ec2:CreateSubnet", "ec2:CreateRouteTable"]
    resources = ["${local.ec2_arn}:vpc/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/netloom:managed"
      values   = ["true"]
    }
  }

  statement {
    sid       = "TagOnCreate"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.ec2_arn}:*/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["CreateVpc", "CreateSubnet", "CreateInternetGateway", "CreateRouteTable"]
    }
  }

  # Everything that modifies or deletes is restricted to resources we tagged.
  statement {
    sid = "ManageOwnResources"
    actions = [
      "ec2:ModifyVpcAttribute",
      "ec2:ModifySubnetAttribute",
      "ec2:AttachInternetGateway",
      "ec2:DetachInternetGateway",
      "ec2:CreateRoute",
      "ec2:AssociateRouteTable",
      "ec2:DisassociateRouteTable",
      "ec2:DeleteSubnet",
      "ec2:DeleteRouteTable",
      "ec2:DeleteInternetGateway",
      "ec2:DeleteVpc",
    ]
    resources = ["${local.ec2_arn}:*/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/netloom:managed"
      values   = ["true"]
    }
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.fn["worker"].arn}:*"]
  }
  statement {
    sid       = "Tracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# --------------------------------------------------------------------------- state machine

resource "aws_iam_role" "workflow" {
  name               = "${local.name}-workflow"
  assume_role_policy = data.aws_iam_policy_document.assume_states.json
}

data "aws_iam_policy_document" "workflow" {
  statement {
    sid       = "InvokeWorker"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker.arn, "${aws_lambda_function.worker.arn}:*"]
  }
  statement {
    sid = "VendedLogs" # Step Functions log delivery requires these on "*"
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "Tracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "workflow" {
  role   = aws_iam_role.workflow.id
  policy = data.aws_iam_policy_document.workflow.json
}
