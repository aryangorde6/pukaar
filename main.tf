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
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
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

# Every table can be restored to any second of the last 35 days, and none can be dropped
# by a destroy or a console click until that flag is turned off first. The record is the
# product; the rails are cheap.
resource "aws_dynamodb_table" "incidents" {
  name         = "${var.prefix}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  point_in_time_recovery { enabled = true }
  deletion_protection_enabled = true
  hash_key                    = "incident_id"

  attribute {
    name = "incident_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "subjects" {
  name         = "${var.prefix}-subjects"
  billing_mode = "PAY_PER_REQUEST"
  point_in_time_recovery { enabled = true }
  deletion_protection_enabled = true
  hash_key                    = "subject_id"

  attribute {
    name = "subject_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "contacts" {
  name         = "${var.prefix}-contacts"
  billing_mode = "PAY_PER_REQUEST"
  point_in_time_recovery { enabled = true }
  deletion_protection_enabled = true
  hash_key                    = "subject_id"
  range_key                   = "contact_id"

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
  point_in_time_recovery { enabled = true }
  deletion_protection_enabled = true
  hash_key                    = "incident_id"
  range_key                   = "contact_tier"

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
  point_in_time_recovery { enabled = true }
  deletion_protection_enabled = true
  hash_key                    = "contact_id"
  range_key                   = "bucket"

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
    actions = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = [
      "arn:aws:ses:${var.region}:${data.aws_caller_identity.current.account_id}:identity/${var.sender_domain}",
      aws_sesv2_configuration_set.pages.arn, # the identity's default set rides on every send, so SES authorises it too
    ]
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
  for_each          = merge(local.functions, { web = "web", checkin = "checkin" })
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
      BASE_URL           = aws_lambda_function_url.web.function_url
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token # the paging functions only; web never sends
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

# Her page carries this key and nothing else does: a cancel or a position needs it,
# so a responder's link or a timeline id cannot call off her alert or move her.
resource "random_password" "her_key" {
  length  = 32
  special = false
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
    variables = merge(local.lambda_env, { HER_KEY = random_password.her_key.result })
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# The weekly check-in: not in the machine, same role as the paging functions (it pages),
# fired by a schedule. One low-stakes page to everyone on her list, so the ranking learns
# who is reachable at which hour without waiting for an emergency.
resource "aws_lambda_function" "checkin" {
  function_name    = "${var.prefix}-checkin"
  role             = aws_iam_role.lambda.arn
  handler          = "checkin.handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = 30 # six emails, in sequence
  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = merge(local.lambda_env, {
      BASE_URL           = aws_lambda_function_url.web.function_url
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
    })
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_scheduler_schedule" "checkin" {
  count                        = var.checkin_schedule == "" ? 0 : 1
  name                         = "${var.prefix}-checkin"
  schedule_expression          = var.checkin_schedule
  schedule_expression_timezone = "Asia/Kolkata"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.checkin.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ subject_id = var.subject_id })
  }
}

resource "aws_iam_role" "scheduler" {
  name = "${var.prefix}-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-checkin"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.checkin.arn }]
  })
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
    StartAt = "Start"
    # A real alert ends in minutes (three circles of wait_s). Nothing may wait forever: a
    # timed-out execution is one of the endings the rule below mails the operator about.
    TimeoutSeconds = 3600
    States = {
      # A fresh press opens a row. A resumed alert - the one who said they were going
      # stepped back - already has one: pick it up at the circle it had reached.
      Start = {
        Type    = "Choice"
        Choices = [{ Variable = "$.resumed", IsPresent = true, Next = "ResumeIncident" }]
        Default = "CreateIncident"
      }
      ResumeIncident = {
        Type     = "Task"
        Resource = aws_lambda_function.fn["create_incident"].arn
        Parameters = {
          "incident_id.$" = "$.incident_id"
          resumed         = true
        }
        ResultPath = "$"
        Retry      = local.lambda_retry
        Catch      = local.spine_catch
        Next       = "SelectTier"
      }
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

# A failed escalation must reach a person, not a dashboard. Step Functions puts every
# execution's end on the default event bus; this rule takes the ones that did not end
# in Done and mails the operator. The Fail state, RecordFailure's row and metric, and
# this email are three views of the same fact.
resource "aws_sns_topic" "failures" {
  name         = "${var.prefix}-failures"
  display_name = "Pukaar" # the From line of the operator's mail
}

resource "aws_sns_topic_subscription" "operator" {
  count     = var.operator_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.failures.arn
  protocol  = "email"
  endpoint  = var.operator_email
}

resource "aws_sns_topic_policy" "failures" {
  arn = aws_sns_topic.failures.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EventBridge"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.failures.arn
        Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.failed.arn } }
      },
      {
        Sid       = "CloudWatch"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.failures.arn
        Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_metric_alarm.web_errors.arn } }
      },
      {
        Sid       = "SES"
        Effect    = "Allow"
        Principal = { Service = "ses.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.failures.arn
        Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
      },
    ]
  })
}

resource "aws_cloudwatch_event_rule" "failed" {
  name        = "${var.prefix}-escalation-failed"
  description = "An escalation ended without help. Tell the operator."
  event_pattern = jsonencode({
    source      = ["aws.states"]
    detail-type = ["Step Functions Execution Status Change"]
    detail = {
      stateMachineArn = [aws_sfn_state_machine.escalation.arn]
      status          = ["FAILED", "TIMED_OUT", "ABORTED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "failed" {
  rule = aws_cloudwatch_event_rule.failed.name
  arn  = aws_sns_topic.failures.arn
  input_transformer {
    input_paths    = { name = "$.detail.name", status = "$.detail.status" }
    input_template = "\"Pukaar: escalation <name> ended <status>. Nobody was told that nobody is coming. The incident row carries the reason; the execution history the state it stopped in.\""
  }
}

# Two failures the machine cannot see, because they happen before or beside it, reach the
# same topic. Her button's own Lambda returning errors: a CloudWatch alarm, five-minute
# window. A page that bounces or is marked as spam: SES events on the sending identity.
resource "aws_cloudwatch_metric_alarm" "web_errors" {
  alarm_name          = "${var.prefix}-web-errors"
  alarm_description   = "pukaar-web returned errors in the last five minutes. Her button may not be working."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.web.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.failures.arn]
  ok_actions          = [aws_sns_topic.failures.arn]
}

resource "aws_sesv2_configuration_set" "pages" {
  configuration_set_name = var.prefix
}

# The sending identity was verified before the event (DNS); it is in Terraform so the
# configuration set below is its default and every page carries it without a code change.
resource "aws_sesv2_email_identity" "sender" {
  email_identity         = var.sender_domain
  configuration_set_name = aws_sesv2_configuration_set.pages.configuration_set_name
}

resource "aws_sesv2_configuration_set_event_destination" "operator" {
  configuration_set_name = aws_sesv2_configuration_set.pages.configuration_set_name
  event_destination_name = "operator"
  depends_on             = [aws_sns_topic_policy.failures] # SES checks the topic's policy when the destination is created
  event_destination {
    enabled              = true
    matching_event_types = ["BOUNCE", "COMPLAINT", "REJECT"]
    sns_destination {
      topic_arn = aws_sns_topic.failures.arn
    }
  }
}

resource "aws_sesv2_configuration_set_event_destination" "metrics" {
  configuration_set_name = aws_sesv2_configuration_set.pages.configuration_set_name
  event_destination_name = "metrics"
  event_destination {
    enabled              = true
    matching_event_types = ["SEND", "DELIVERY", "BOUNCE", "COMPLAINT", "REJECT"]
    cloud_watch_destination {
      dimension_configuration {
        dimension_name          = "ses:configuration-set"
        default_dimension_value = var.prefix
        dimension_value_source  = "MESSAGE_TAG"
      }
    }
  }
}

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

output "sender" {
  value = var.sender # verify.py sends one page to the SES bounce simulator from it
}

output "record_key" {
  value = aws_kms_alias.record.name
}

output "her_key" {
  value     = random_password.her_key.result # read by verify.sh and shoot.sh to cancel as her page would
  sensitive = true
}

output "telegram_chat_ids" {
  value = var.telegram_chat_ids # read by seed.sh, so the ids never sit in the repo
}
