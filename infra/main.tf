# Mock LLM API (Anthropic + OpenAI) on a Lambda Function URL, for seeing what an AI Gateway forwards upstream. Local state by default:
# copy a backend block into backend.tf (gitignored) for remote state.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = ">= 6.46.0, < 7.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.0" }
    random  = { source = "hashicorp/random", version = "~> 3.0" }
  }
}

variable "aws_region" {
  type    = string
  default = "us-west-2"
}

variable "name" {
  type    = string
  default = "aigw-mock-llm"
}

# Extra strings to flag in forwarded headers and body, comma-separated (e.g. a JWT's sub). Set in terraform.tfvars.
variable "leak_needles" {
  type    = string
  default = ""
}

# Lambda timeout in seconds; must exceed the longest MOCK_DELAY / -delay-<ms> you test with.
variable "timeout" {
  type    = number
  default = 60
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { project = "aigw-mock-llm", managed_by = "terraform" }
  }
}

# Secret first path segment: the gateway's custom_host is <url><token>/v1
resource "random_password" "token" {
  length  = 32
  special = false
}

data "archive_file" "mock" {
  type        = "zip"
  source_file = "${path.module}/../handler/handler.py"
  output_path = "${path.module}/.build/mock.zip"
}

resource "aws_iam_role" "mock" {
  name = var.name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.mock.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "mock" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = 3
}

resource "aws_lambda_function" "mock" {
  function_name    = var.name
  role             = aws_iam_role.mock.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.mock.output_path
  source_code_hash = data.archive_file.mock.output_base64sha256
  timeout          = var.timeout
  memory_size      = 128

  environment {
    variables = {
      TOKEN        = random_password.token.result
      LEAK_NEEDLES = var.leak_needles
    }
  }

  depends_on = [aws_cloudwatch_log_group.mock, aws_iam_role_policy_attachment.logs]
}

# Public HTTPS endpoint the SaaS gateway can reach. Auth is the secret path segment, checked in the handler.
resource "aws_lambda_function_url" "mock" {
  function_name      = aws_lambda_function.mock.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.mock.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# Since Oct 2025 public function URLs also need lambda:InvokeFunction scoped to URL invocations.
resource "aws_lambda_permission" "url_invoke" {
  statement_id             = "FunctionURLInvokeAllowPublicAccess"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.mock.function_name
  principal                = "*"
  invoked_via_function_url = true
}

output "url" {
  value = aws_lambda_function_url.mock.function_url
}

output "token" {
  value     = random_password.token.result
  sensitive = true
}

output "log_group" {
  value = aws_cloudwatch_log_group.mock.name
}
