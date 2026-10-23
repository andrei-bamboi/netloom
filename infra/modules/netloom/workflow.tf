# Network lifecycle state machine.
#
#   create:  Begin -> CreateVpc -> CreateSubnets -> ConfigureRouting -> Finalize
#               \______________ any failure ______________/
#                              Rollback -> MarkFailed -> Failed
#
#   delete:  Teardown -> Retire        (failure -> MarkFailed; DELETE can be retried)
#
# Every task calls the same worker Lambda with a different "step". The worker
# keeps all state in DynamoDB, so the machine only carries the network id.

locals {
  transient_errors = [
    "RetryableStepError",
    "Lambda.ServiceException",
    "Lambda.AWSLambdaException",
    "Lambda.SdkClientException",
    "Lambda.TooManyRequestsException",
  ]

  steps = {
    Begin            = { step = "begin", next = "CreateVpc", on_error = "Rollback" }
    CreateVpc        = { step = "vpc", next = "CreateSubnets", on_error = "Rollback" }
    CreateSubnets    = { step = "subnets", next = "ConfigureRouting", on_error = "Rollback" }
    ConfigureRouting = { step = "routing", next = "Finalize", on_error = "Rollback" }
    Finalize         = { step = "finalize", next = "Done", on_error = "Rollback" }
    Rollback         = { step = "rollback", next = "MarkFailed", on_error = "MarkFailed", error_path = "$.rollback_error" }
    MarkFailed       = { step = "fail", next = "Failed", on_error = "Failed", with_error = true, error_path = "$.mark_error" }
    Teardown         = { step = "teardown", next = "Retire", on_error = "MarkFailed" }
    Retire           = { step = "retire", next = "Done", on_error = "MarkFailed" }
  }

  task_states = {
    for state, cfg in local.steps : state => {
      Type     = "Task"
      Resource = "arn:${local.partition}:states:::lambda:invoke"
      Parameters = {
        FunctionName = aws_lambda_function.worker.arn
        Payload = merge(
          { step = cfg.step, "network_id.$" = "$.network_id" },
          try(cfg.with_error, false) ? { "error.$" = "$.error" } : {}
        )
      }
      ResultPath = null
      Retry = [{
        ErrorEquals     = local.transient_errors
        IntervalSeconds = 3
        BackoffRate     = 2
        MaxAttempts     = 6
        JitterStrategy  = "FULL"
      }]
      Catch = [{
        ErrorEquals = ["States.ALL"]
        ResultPath  = try(cfg.error_path, "$.error")
        Next        = cfg.on_error
      }]
      Next = cfg.next
    }
  }

  definition = {
    Comment        = "netloom network lifecycle"
    TimeoutSeconds = 1800
    StartAt        = "Route"
    States = merge(local.task_states, {
      Route = {
        Type    = "Choice"
        Choices = [{ Variable = "$.operation", StringEquals = "delete", Next = "Teardown" }]
        Default = "Begin"
      }
      Done   = { Type = "Succeed" }
      Failed = { Type = "Fail", Error = "NetworkOperationFailed", Cause = "See the network's events for details." }
    })
  }
}

resource "aws_cloudwatch_log_group" "workflow" {
  name              = "/aws/vendedlogs/states/${local.name}-lifecycle"
  retention_in_days = var.log_retention_days
}

resource "aws_sfn_state_machine" "lifecycle" {
  name       = "${local.name}-lifecycle"
  role_arn   = aws_iam_role.workflow.arn
  type       = "STANDARD"
  definition = jsonencode(local.definition)

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.workflow.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  tracing_configuration {
    enabled = true
  }
}
