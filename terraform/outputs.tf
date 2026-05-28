output "lambda_function_arn" {
  description = "ARN of the stevedore Lambda function."
  value       = aws_lambda_function.ecs_consolidation.arn
}

output "lambda_function_name" {
  description = "Name of the stevedore Lambda function."
  value       = aws_lambda_function.ecs_consolidation.function_name
}

output "scheduler_schedule_arn" {
  description = "ARN of the EventBridge Scheduler schedule."
  value       = aws_scheduler_schedule.ecs_consolidation.arn
}

output "security_group_id" {
  description = "ID of the Lambda security group."
  value       = aws_security_group.ecs_consolidation.id
}
