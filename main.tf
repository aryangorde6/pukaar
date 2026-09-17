terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
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

locals {
  # handler name -> file under lambdas/. Each becomes one function.
  functions = {
    create_incident = "create_incident"
  }
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
    resources = [
      aws_dynamodb_table.incidents.arn,
    ]
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
      INCIDENTS_TABLE = aws_dynamodb_table.incidents.name
      WAIT_S          = tostring(var.wait_s)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# --- Outputs -----------------------------------------------------------------

output "incidents_table" {
  value = aws_dynamodb_table.incidents.name
}

output "create_incident_fn" {
  value = aws_lambda_function.fn["create_incident"].function_name
}
