variable "name_prefix" {
  description = "Prefix used for all resource names, e.g. 'myapp-prod'."
  type        = string
}

variable "ecr_arn" {
  description = "ARN of the ECR repository the Lambda image is pulled from."
  type        = string
}

variable "image_uri" {
  description = "ECR image URI for the Lambda container, e.g. '123456789012.dkr.ecr.us-east-1.amazonaws.com/stevedore:latest'."
  type        = string
}

variable "ecs_cluster_name" {
  description = "Name of the ECS cluster to consolidate."
  type        = string
}

variable "ecs_cluster_arn" {
  description = "ARN of the ECS cluster to consolidate."
  type        = string
}

variable "vpc_id" {
  description = "VPC ID for the Lambda security group."
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs the Lambda function runs in."
  type        = list(string)
}

variable "kms_key_arn" {
  description = "KMS key ARN used to encrypt the CloudWatch log group. Set to null to skip encryption."
  type        = string
  default     = null
}

variable "kms_key_alias" {
  description = "KMS key alias name (e.g. 'alias/my-key'). Used in the IAM condition when kms_key_arn is set."
  type        = string
  default     = null
}

variable "schedule_expression" {
  description = "EventBridge Scheduler expression for how often consolidation runs."
  type        = string
  default     = "rate(5 minutes)"
}

variable "disruption_budget_percent" {
  description = "Maximum percentage of active instances that can be drained per run."
  type        = number
  default     = 30
}

variable "dry_run" {
  description = "When true the Lambda logs intended actions but does not drain any instances."
  type        = bool
  default     = false
}

variable "max_instance_age_days" {
  description = "Drain instances older than this many days (Strategy 2: Expired)."
  type        = number
  default     = 30
}

variable "min_instance_age_minutes" {
  description = "Skip instances newer than this many minutes (warmup guard)."
  type        = number
  default     = 15
}

variable "min_instances_per_az" {
  description = "Minimum number of active instances to retain per availability zone."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  description = "CloudWatch log group retention in days."
  type        = number
  default     = 30
}

variable "region" {
  description = "AWS region. Defaults to the provider region when empty."
  type        = string
  default     = ""
}

variable "account_id" {
  description = "AWS account ID. Resolved automatically when empty."
  type        = string
  default     = ""
}
