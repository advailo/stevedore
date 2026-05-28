data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  region     = var.region != "" ? var.region : data.aws_region.current.region
  account_id = var.account_id != "" ? var.account_id : data.aws_caller_identity.current.account_id
  name       = "${var.name_prefix}-stevedore"
}

# ============================================================================
# CloudWatch Log Group
# ============================================================================

resource "aws_cloudwatch_log_group" "ecs_consolidation" {
  name              = "/aws/lambda/${local.name}"
  kms_key_id        = var.kms_key_arn
  retention_in_days = var.log_retention_days
}

# ============================================================================
# IAM — Lambda execution role
# ============================================================================

resource "aws_iam_role" "ecs_consolidation" {
  name = local.name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "ecs_consolidation_ecs" {
  role = aws_iam_role.ecs_consolidation.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "ecs:ListContainerInstances",
        ]
        Effect   = "Allow"
        Resource = [var.ecs_cluster_arn]
      },
      {
        Action = [
          "ecs:DescribeContainerInstances",
          "ecs:ListTasks",
          "ecs:UpdateContainerInstancesState",
        ]
        Effect = "Allow"
        Resource = [
          "arn:aws:ecs:${local.region}:${local.account_id}:container-instance/${var.ecs_cluster_name}/*"
        ]
      },
      {
        Action = [
          "ecs:DescribeTasks",
        ]
        Effect = "Allow"
        Resource = [
          "arn:aws:ecs:${local.region}:${local.account_id}:task/${var.ecs_cluster_name}/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "ecs_consolidation_ec2" {
  role = aws_iam_role.ecs_consolidation.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "ec2:CreateNetworkInterface",
        ]
        Effect = "Allow"
        Resource = [
          "arn:aws:ec2:${local.region}:${local.account_id}:network-interface/*",
          "arn:aws:ec2:${local.region}:${local.account_id}:security-group/*",
          "arn:aws:ec2:${local.region}:${local.account_id}:subnet/*",
        ]
      },
      {
        Action = [
          "ec2:DeleteNetworkInterface",
        ]
        Effect = "Allow"
        Resource = [
          "arn:aws:ec2:${local.region}:${local.account_id}:*/*",
        ]
      },
      {
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeNetworkInterfaces",
        ]
        Effect   = "Allow"
        Resource = ["*"]
      },
    ]
  })
}

resource "aws_iam_role_policy" "ecs_consolidation_logs" {
  role = aws_iam_role.ecs_consolidation.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Effect = "Allow"
        Resource = [
          "${aws_cloudwatch_log_group.ecs_consolidation.arn}:*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "ecs_consolidation_kms" {
  count = var.kms_key_arn != null ? 1 : 0
  role  = aws_iam_role.ecs_consolidation.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        Effect = "Allow"
        Resource = [
          "arn:aws:kms:${local.region}:${local.account_id}:key/*"
        ]
        Condition = {
          "ForAnyValue:StringEquals" = {
            "kms:ResourceAliases" = [var.kms_key_alias]
          }
        }
      }
    ]
  })
}

# ============================================================================
# Security Group
# ============================================================================

resource "aws_security_group" "ecs_consolidation" {
  description = "stevedore Lambda - HTTPS egress only"
  name        = local.name
  tags = {
    Name = local.name
  }
  vpc_id = var.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "ecs_consolidation_https" {
  cidr_ipv4         = "0.0.0.0/0"
  description       = "Allow HTTPS outbound for AWS API calls"
  from_port         = 443
  ip_protocol       = "tcp"
  security_group_id = aws_security_group.ecs_consolidation.id
  to_port           = 443
}

# ============================================================================
# Lambda — container image
# ============================================================================

resource "aws_lambda_function" "ecs_consolidation" {
  function_name = local.name
  architectures = ["arm64"]
  image_uri     = var.image_uri
  memory_size   = 128
  package_type  = "Image"
  role          = aws_iam_role.ecs_consolidation.arn
  timeout       = 60
  environment {
    variables = {
      CLUSTER_NAME              = var.ecs_cluster_name
      DISRUPTION_BUDGET_PERCENT = var.disruption_budget_percent
      DRY_RUN                   = var.dry_run
      MAX_INSTANCE_AGE_DAYS     = var.max_instance_age_days
      MIN_INSTANCE_AGE_MINUTES  = var.min_instance_age_minutes
      MIN_INSTANCES_PER_AZ      = var.min_instances_per_az
    }
  }
  vpc_config {
    security_group_ids = [aws_security_group.ecs_consolidation.id]
    subnet_ids         = var.subnet_ids
  }
}

# ============================================================================
# EventBridge Scheduler
# ============================================================================

resource "aws_iam_role" "ecs_consolidation_scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
          }
        }
        Effect = "Allow"
        Principal = {
          Service = "scheduler.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "ecs_consolidation_scheduler_lambda" {
  role = aws_iam_role.ecs_consolidation_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "lambda:InvokeFunction",
        ]
        Effect = "Allow"
        Resource = [
          aws_lambda_function.ecs_consolidation.arn
        ]
      }
    ]
  })
}

resource "aws_scheduler_schedule" "ecs_consolidation" {
  name                = local.name
  group_name          = "default"
  schedule_expression = var.schedule_expression

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.ecs_consolidation.arn
    role_arn = aws_iam_role.ecs_consolidation_scheduler.arn
  }
}
