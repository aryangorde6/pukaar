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
    select_tier     = "select_tier"
    notify          = "notify"
    check_claim     = "check_claim"
    broadcast       = "broadcast"
    record_failure  = "record_failure"
  }

  # Named here rather than read from the resource so the web function can start
  # executions without a dependency cycle (machine -> functions -> machine).
  state_machine_name = "${var.prefix}-escalation"
  broadcast_name     = "${var.prefix}-broadcast"
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

# One zip of the whole handlers directory, shared by every function, so
# handlers can import templates.py. A change to any file redeploys all of them.
data "archive_file" "lambdas" {
  type        = "zip"
  source_dir  = "${path.module}/lambdas"
  output_path = "${path.module}/.build/lambdas.zip"
  excludes    = ["__pycache__"]
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
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = ["arn:aws:ses:${var.region}:${data.aws_caller_identity.current.account_id}:identity/${var.sender_domain}"]
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

# The web function has its own role: it is the only principal that can open the
# sealed record, and the only one that starts or wakes the machine. It cannot send
# email; the paging functions cannot decrypt.
resource "aws_iam_role" "web" {
  name               = "${var.prefix}-web"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "web" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:*:log-group:/aws/lambda/${var.prefix}-web:*"]
  }

  statement {
    actions = [
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

  # Waking the parked machine. SendTaskSuccess has no resource-level scope.
  statement {
    actions   = ["states:SendTaskSuccess"]
    resources = ["*"]
  }

  # A cancel after someone has already claimed: the machine is done, so the web
  # function asks broadcast to send the false alarm itself. It still cannot send mail.
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${local.broadcast_name}"]
  }

  # Decrypt only, and only when the call names whose record it is.
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.record.arn]
    condition {
      test     = "Null"
      variable = "kms:EncryptionContext:subject_id"
      values   = ["false"]
    }
  }
}

resource "aws_iam_role_policy" "web" {
  name   = "${var.prefix}-web"
  role   = aws_iam_role.web.id
  policy = data.aws_iam_policy_document.web.json
}

# --- Sealed record -----------------------------------------------------------
# Her medical notes are encrypted under a key of this stack's own, because the
# AWS-managed DynamoDB key cannot be called from application code. The seed
# encrypts with the subject's id as encryption context; the same id must be
# presented to decrypt, so one subject's ciphertext cannot be opened as another's.
resource "aws_kms_key" "record" {
  description             = "${var.prefix}: sealed medical records, opened only for whoever is going"
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "record" {
  name          = "alias/${var.prefix}-record"
  target_key_id = aws_kms_key.record.key_id
}

# Declared explicitly so retention is set. Lambda's auto-created group never expires.
resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = merge(local.functions, { web = "web" })
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
  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = merge(local.lambda_env, {
      BASE_URL = aws_lambda_function_url.web.function_url
    })
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

locals {
  lambda_env = {
    INCIDENTS_TABLE      = aws_dynamodb_table.incidents.name
    SUBJECTS_TABLE       = aws_dynamodb_table.subjects.name
    CONTACTS_TABLE       = aws_dynamodb_table.contacts.name
    NOTIFICATIONS_TABLE  = aws_dynamodb_table.notifications.name
    RESPONSE_STATS_TABLE = aws_dynamodb_table.response_stats.name
    STATE_MACHINE_ARN    = local.state_machine_arn
    BROADCAST_FN         = local.broadcast_name
    SUBJECT_ID           = var.subject_id
    SENDER               = var.sender
    WAIT_S               = tostring(var.wait_s)
    MAX_TIER             = tostring(var.max_tier)
  }
}

# The web function is declared on its own: the fan-out functions need its URL
# for the links in their emails, and a function cannot depend on its own URL.
resource "aws_lambda_function" "web" {
  function_name    = "${var.prefix}-web"
  role             = aws_iam_role.web.arn
  handler          = "web.handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = 10
  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# The one public origin. Everything the subject or a responder touches is served here.
resource "aws_lambda_function_url" "web" {
  function_name      = aws_lambda_function.web.function_name
  authorization_type = "NONE"
}

# authorization_type = NONE is not enough on its own. The URL resource above grants
# lambda:InvokeFunctionUrl to everyone, but since October 2025 a public URL also needs
# lambda:InvokeFunction, scoped to calls that arrive through the URL. Without this
# statement every request gets 403, with the first grant already in place.
resource "aws_lambda_permission" "web_url_public_invoke" {
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.web.function_name
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
  statement {
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.incidents.arn]
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
        Next       = "SelectTier"
      }
      SelectTier = {
        Type       = "Task"
        Resource   = aws_lambda_function.fn["select_tier"].arn
        ResultPath = "$"
        Retry      = local.lambda_retry
        Catch      = local.spine_catch
        Next       = "NotifyTier"
      }
      # Everyone in the tier is paged at the same moment. One failed send stays
      # inside its own iteration; the Map as a whole cannot fail because of it.
      NotifyTier = {
        Type      = "Map"
        ItemsPath = "$.contacts"
        ItemSelector = {
          "incident_id.$"     = "$.incident_id"
          "tier.$"            = "$.tier_index"
          "subject.$"         = "$.subject"
          "started_at.$"      = "$.started_at"
          "pressed_at.$"      = "$.pressed_at"
          "contacted_count.$" = "$.contacted_count"
          "contact.$"         = "$$.Map.Item.Value"
        }
        ItemProcessor = {
          ProcessorConfig = { Mode = "INLINE" }
          StartAt         = "NotifyOne"
          States = {
            NotifyOne = {
              Type     = "Task"
              Resource = aws_lambda_function.fn["notify"].arn
              Retry = concat(local.lambda_retry, [{
                ErrorEquals     = ["SesThrottled"]
                IntervalSeconds = 1
                MaxAttempts     = 4
                BackoffRate     = 2
              }])
              Catch = [{
                ErrorEquals = ["States.ALL"]
                ResultPath  = "$.error"
                Next        = "NotifyFailed"
              }]
              End = true
            }
            NotifyFailed = {
              Type = "Pass"
              Parameters = {
                notified       = false
                reason         = "crashed"
                "contact_id.$" = "$.contact.contact_id"
                "error.$"      = "$.error"
              }
              End = true
            }
          }
        }
        ResultPath = "$.notify_results"
        Next       = "TallyNotified"
      }
      # A Choice cannot call an intrinsic, so the count is materialised first.
      TallyNotified = {
        Type = "Pass"
        Parameters = {
          "sent_count.$" = "States.ArrayLength($.notify_results[?(@.notified == true)])"
        }
        ResultPath = "$.tally"
        Next       = "NotifyDecision"
      }
      # One unreachable person is a normal Tuesday. A tier that reached nobody is a
      # broken escalation, and waiting a minute for a claim that cannot come is the
      # silent failure this machine exists to refuse.
      NotifyDecision = {
        Type = "Choice"
        Choices = [
          { Variable = "$.tally.sent_count", NumericEquals = 0, Next = "RecordFailure" },
        ]
        Default = "WaitForClaim"
      }
      # Not a timer. The machine parks its task token on the incident row and
      # sleeps until a claim or a cancel hands it back, or the tier timeout
      # fires. Either way the next state reads the row - the token only wakes
      # the machine early, it never decides anything.
      WaitForClaim = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:dynamodb:updateItem.waitForTaskToken"
        Parameters = {
          TableName                = aws_dynamodb_table.incidents.name
          Key                      = { incident_id = { "S.$" = "$.incident_id" } }
          UpdateExpression         = "SET task_token = :t, token_set_at = :n"
          ConditionExpression      = "#s = :open"
          ExpressionAttributeNames = { "#s" = "status" }
          ExpressionAttributeValues = {
            ":t"    = { "S.$" = "$$.Task.Token" }
            ":n"    = { "S.$" = "$$.State.EnteredTime" }
            ":open" = { S = "OPEN" }
          }
        }
        TimeoutSecondsPath = "$.wait_s"
        ResultPath         = "$.wake"
        Catch = concat([{
          # Timeout: nobody answered in time. Condition failed: it was claimed or
          # cancelled before the token could even be written. Both: go and look.
          ErrorEquals = ["States.Timeout", "DynamoDb.ConditionalCheckFailedException"]
          ResultPath  = "$.wake"
          Next        = "CheckClaim"
        }], local.spine_catch)
        Next = "CheckClaim"
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
          { Variable = "$.claim", StringEquals = "claimed", Next = "BroadcastClaim" },
          { Variable = "$.claim", StringEquals = "cancelled", Next = "Cancelled" },
          {
            And = [
              { Variable = "$.claim", StringEquals = "none" },
              { Variable = "$.tier_index", NumericLessThanPath = "$.max_tier" },
            ]
            Next = "NextTier"
          },
        ]
        Default = "FinalFallback"
      }
      # Someone said they are going: everyone who was paged is told who, by name.
      BroadcastClaim = {
        Type       = "Task"
        Resource   = aws_lambda_function.fn["broadcast"].arn
        Parameters = { kind = "someone_going", "incident_id.$" = "$.incident_id" }
        ResultPath = "$.broadcast"
        Retry      = local.lambda_retry
        Catch      = local.spine_catch
        Next       = "Done"
      }
      # She pressed cancel: everyone who was paged is told it was a false alarm.
      Cancelled = {
        Type       = "Task"
        Resource   = aws_lambda_function.fn["broadcast"].arn
        Parameters = { kind = "false_alarm", "incident_id.$" = "$.incident_id" }
        ResultPath = "$.broadcast"
        Retry      = local.lambda_retry
        Catch      = local.spine_catch
        Next       = "Done"
      }
      # Nobody came and there is another circle to widen to. SelectTier picks it.
      NextTier = {
        Type = "Pass"
        Next = "SelectTier"
      }
      # Every circle has been tried. Everyone on her list is told to call 112 or go.
      FinalFallback = {
        Type       = "Task"
        Resource   = aws_lambda_function.fn["broadcast"].arn
        Parameters = { kind = "no_one_reached", "incident_id.$" = "$.incident_id" }
        ResultPath = "$.broadcast"
        Retry      = local.lambda_retry
        Catch      = local.spine_catch
        Next       = "Done"
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

moved {
  from = aws_lambda_function.fn["web"]
  to   = aws_lambda_function.web
}

output "record_key" {
  value = aws_kms_alias.record.name
}
