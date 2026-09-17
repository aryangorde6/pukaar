terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.profile

  default_tags {
    tags = {
      project = "pukaar"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  # handler name -> file under lambdas/. Each becomes one function.
  functions = {
    create_incident = "create_incident"
    check_claim     = "check_claim"
    record_failure  = "record_failure"
    web             = "web"
  }

  # Named here rather than read from the resource so the web function can start
  # executions without a dependency cycle (machine -> functions -> machine).
  state_machine_name = "${var.prefix}-escalation"
  state_machine_arn  = "arn:aws:states:${var.region}:${data.aws_caller_identity.current.account_id}:stateMachine:${local.state_machine_name}"
}

# --- Tables ------------------------------------------------------------------

resource "aws_dynamodb_table" "incidents" {
  name         = "${var.prefix}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "incident_id"

  attribute {
    name = "incident_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "subjects" {
  name         = "${var.prefix}-subjects"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "subject_id"

  attribute {
    name = "subject_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "contacts" {
  name         = "${var.prefix}-contacts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "subject_id"
  range_key    = "contact_id"

  attribute {
    name = "subject_id"
    type = "S"
  }
  attribute {
    name = "contact_id"
    type = "S"
  }
}

# One row per (incident, contact, tier). The sort key is "<contact_id>#<tier>".
resource "aws_dynamodb_table" "notifications" {
  name         = "${var.prefix}-notifications"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "incident_id"
  range_key    = "contact_tier"

  attribute {
    name = "incident_id"
    type = "S"
  }
  attribute {
    name = "contact_tier"
    type = "S"
  }
  attribute {
    name = "token_hash"
    type = "S"
  }

  # A claim link carries a token; this is how the token finds its row.
  global_secondary_index {
    name            = "token_hash-index"
    hash_key        = "token_hash"
    projection_type = "ALL"
  }
}

# Running counters per contact per (hour, weekday|weekend). Sort key "14#weekday".
resource "aws_dynamodb_table" "response_stats" {
  name         = "${var.prefix}-response-stats"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "contact_id"
  range_key    = "bucket"

  attribute {
    name = "contact_id"
    type = "S"
  }
  attribute {
    name = "bucket"
    type = "S"
  }
}

locals {
  tables = [
    aws_dynamodb_table.incidents,
    aws_dynamodb_table.subjects,
    aws_dynamodb_table.contacts,
    aws_dynamodb_table.notifications,
    aws_dynamodb_table.response_stats,
  ]
}

# --- Lambda ------------------------------------------------------------------

data "archive_file" "lambdas" {
  for_each    = local.functions
  type        = "zip"
  source_file = "${path.module}/lambdas/${each.value}.py"
  output_path = "${path.module}/.build/${each.key}.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.prefix}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:*:log-group:/aws/lambda/${var.prefix}-*:*"]
  }

  statement {
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = concat(
      [for t in local.tables : t.arn],
      [for t in local.tables : "${t.arn}/index/*"],
    )
  }

  statement {
    actions   = ["states:StartExecution"]
    resources = [local.state_machine_arn]
  }

  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Pukaar"]
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.prefix}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# Declared explicitly so retention is set. Lambda's auto-created group never expires.
resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.functions
  name              = "/aws/lambda/${var.prefix}-${replace(each.key, "_", "-")}"
  retention_in_days = 7
}

resource "aws_lambda_function" "fn" {
  for_each = local.functions

  function_name    = "${var.prefix}-${replace(each.key, "_", "-")}"
  role             = aws_iam_role.lambda.arn
  handler          = "${each.value}.handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = 10
  filename         = data.archive_file.lambdas[each.key].output_path
  source_code_hash = data.archive_file.lambdas[each.key].output_base64sha256

  environment {
    variables = {
      INCIDENTS_TABLE      = aws_dynamodb_table.incidents.name
      SUBJECTS_TABLE       = aws_dynamodb_table.subjects.name
      CONTACTS_TABLE       = aws_dynamodb_table.contacts.name
      NOTIFICATIONS_TABLE  = aws_dynamodb_table.notifications.name
      RESPONSE_STATS_TABLE = aws_dynamodb_table.response_stats.name
      STATE_MACHINE_ARN    = local.state_machine_arn
      SUBJECT_ID           = var.subject_id
      WAIT_S               = tostring(var.wait_s)
      MAX_TIER             = tostring(var.max_tier)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# The one public origin. Everything the subject or a responder touches is served here.
resource "aws_lambda_function_url" "web" {
  function_name      = aws_lambda_function.fn["web"].function_name
  authorization_type = "NONE"
}

# authorization_type = NONE is not enough on its own. The URL resource above grants
# lambda:InvokeFunctionUrl to everyone, but since October 2025 a public URL also needs
# lambda:InvokeFunction, scoped to calls that arrive through the URL. Without this
# statement every request gets 403, with the first grant already in place.
resource "aws_lambda_permission" "web_url_public_invoke" {
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.fn["web"].function_name
  principal                = "*"
  invoked_via_function_url = true
}

# --- Step Functions ----------------------------------------------------------

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${var.prefix}-sfn"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

data "aws_iam_policy_document" "sfn" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [for f in aws_lambda_function.fn : f.arn]
  }
}

resource "aws_iam_role_policy" "sfn" {
  name   = "${var.prefix}-sfn"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}

locals {
  # Transient Lambda faults are retried; anything else on a spine task is a
  # broken escalation and goes to RecordFailure, which fails the execution.
  lambda_retry = [{
    ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"]
    IntervalSeconds = 1
    MaxAttempts     = 3
    BackoffRate     = 2
  }]
  spine_catch = [{
    ErrorEquals = ["States.ALL"]
    ResultPath  = "$.error"
    Next        = "RecordFailure"
  }]
}

resource "aws_sfn_state_machine" "escalation" {
  name     = local.state_machine_name
  role_arn = aws_iam_role.sfn.arn
  type     = "STANDARD"

  definition = jsonencode({
    Comment = "One press: open the incident, wait for someone to say they are going, decide."
    StartAt = "CreateIncident"
    States = {
      CreateIncident = {
        Type     = "Task"
        Resource = aws_lambda_function.fn["create_incident"].arn
        Parameters = {
          "incident_id.$"   = "$.incident_id"
          "subject_id.$"    = "$.subject_id"
          "wait_s.$"        = "$.wait_s"
          "max_tier.$"      = "$.max_tier"
          "execution_arn.$" = "$$.Execution.Id"
        }
        ResultPath = "$"
        Retry      = local.lambda_retry
        Catch      = local.spine_catch
        Next       = "WaitForClaim"
      }
      WaitForClaim = {
        Type        = "Wait"
        SecondsPath = "$.wait_s"
        Next        = "CheckClaim"
      }
      CheckClaim = {
        Type       = "Task"
        Resource   = aws_lambda_function.fn["check_claim"].arn
        ResultPath = "$"
        Retry      = local.lambda_retry
        Catch      = local.spine_catch
        Next       = "ClaimDecision"
      }
      ClaimDecision = {
        Type = "Choice"
        Choices = [
          { Variable = "$.claim", StringEquals = "claimed", Next = "Done" },
          { Variable = "$.claim", StringEquals = "cancelled", Next = "Done" },
        ]
        Default = "NoClaimYet"
      }
      # Placeholder for the widen loop. Skeleton only: nobody claimed, nothing else happens yet.
      NoClaimYet = {
        Type = "Pass"
        Next = "Done"
      }
      RecordFailure = {
        Type       = "Task"
        Resource   = aws_lambda_function.fn["record_failure"].arn
        ResultPath = "$.failure"
        Next       = "Failed"
      }
      Done = {
        Type = "Succeed"
      }
      Failed = {
        Type  = "Fail"
        Error = "EscalationFailed"
        Cause = "A spine state failed. The incident row carries the reason."
      }
    }
  })
}

# --- Outputs -----------------------------------------------------------------

output "state_machine_arn" {
  value = aws_sfn_state_machine.escalation.arn
}

output "web_url" {
  value = aws_lambda_function_url.web.function_url
}

output "tables" {
  value = [for t in local.tables : t.name]
}

output "incidents_table" {
  value = aws_dynamodb_table.incidents.name
}

output "create_incident_fn" {
  value = aws_lambda_function.fn["create_incident"].function_name
}
