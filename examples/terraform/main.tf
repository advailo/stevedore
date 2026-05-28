terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

# Fetch existing ECS cluster by name — replace with your own or use a data source.
data "aws_ecs_cluster" "this" {
  cluster_name = "my-ecs-cluster"
}

data "aws_vpc" "this" {
  tags = { Name = "my-vpc" }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.this.id]
  }
  tags = { Tier = "private" }
}

module "ecs_consolidation" {
  source = "github.com/advailo/stevedore//terraform?ref=v1.0.0"

  name_prefix      = "myapp-prod"
  image_uri        = "public.ecr.aws/advailo/stevedore:v1.0.0"
  ecs_cluster_name = data.aws_ecs_cluster.this.cluster_name
  ecs_cluster_arn  = data.aws_ecs_cluster.this.arn
  vpc_id           = data.aws_vpc.this.id
  subnet_ids       = data.aws_subnets.private.ids

  # Tune to your workload
  disruption_budget_percent = 30
  max_instance_age_days     = 30
  min_instances_per_az      = 1
  dry_run                   = false
}

output "lambda_function_name" {
  value = module.ecs_consolidation.lambda_function_name
}
