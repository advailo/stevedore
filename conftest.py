"""Shared test fixtures for stevedore tests."""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest


# ============================================================================
# Mock ECS Client
# ============================================================================


class MockECS:
    def __init__(self):
        self.reset()

    def list_container_instances(self, **kwargs):
        status = kwargs.get("status", "ACTIVE")
        arns = self._active_arns if status == "ACTIVE" else self._draining_arns
        return {"containerInstanceArns": list(arns)}

    def describe_container_instances(self, **kwargs):
        arns = set(kwargs.get("containerInstances", []))
        return {
            "containerInstances": [
                ci for ci in self._container_instances
                if ci["containerInstanceArn"] in arns
            ]
        }

    def list_tasks(self, **kwargs):
        ci_arn = kwargs.get("containerInstance", "")
        return {"taskArns": [t["taskArn"] for t in self._tasks.get(ci_arn, [])]}

    def describe_tasks(self, **kwargs):
        task_arns = set(kwargs.get("tasks", []))
        all_tasks = [t for tasks in self._tasks.values() for t in tasks]
        return {"tasks": [t for t in all_tasks if t["taskArn"] in task_arns]}

    def update_container_instances_state(self, **kwargs):
        self.drain_calls.append(kwargs)
        return {}

    def reset(self):
        self._container_instances = []
        self._tasks = {}
        self._active_arns = []
        self._draining_arns = []
        self.drain_calls = []


class MockEC2:
    DEFAULT_AZ = "eu-north-1a"

    def __init__(self):
        self.reset()

    def describe_instances(self, **kwargs):
        instance_ids = kwargs.get("InstanceIds", [])
        reservations = []
        for iid in instance_ids:
            az = self._az_map.get(iid, self.DEFAULT_AZ)
            reservations.append({
                "Instances": [{"InstanceId": iid, "Placement": {"AvailabilityZone": az}}]
            })
        return {"Reservations": reservations}

    def reset(self):
        self._az_map = {}  # ec2_instance_id -> availability_zone


mock_ecs = MockECS()
mock_ec2 = MockEC2()


class MockBoto3:
    @staticmethod
    def client(service, **kwargs):
        if service == "ecs":
            return mock_ecs
        if service == "ec2":
            return mock_ec2
        raise ValueError(f"Unexpected service: {service}")


# Install mock before importing index
sys.modules["boto3"] = MockBoto3

# Set environment variables before import
os.environ.setdefault("CLUSTER_NAME", "test-cluster")
os.environ.setdefault("DISRUPTION_BUDGET_PERCENT", "30")
os.environ.setdefault("MIN_INSTANCE_AGE_MINUTES", "15")
os.environ.setdefault("MAX_INSTANCE_AGE_DAYS", "30")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("LOG_LEVEL", "DEBUG")


# ============================================================================
# Factory Helpers
# ============================================================================

CLUSTER = "test-cluster"
REGION = "eu-north-1"
ACCOUNT = "123456789012"


def make_instance_arn(instance_id):
    return f"arn:aws:ecs:{REGION}:{ACCOUNT}:container-instance/{CLUSTER}/{instance_id}"


def make_task_arn(task_id):
    return f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{CLUSTER}/{task_id}"


def make_container_instance(
    instance_id,
    registered_cpu=2048,
    registered_memory=7680,
    remaining_cpu=2048,
    remaining_memory=7680,
    registered_eni=3,
    remaining_eni=3,
    age_minutes=60,
):
    """Create a mock ECS container instance."""
    arn = make_instance_arn(instance_id)
    registered_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {
        "containerInstanceArn": arn,
        "ec2InstanceId": f"i-{instance_id}",
        "status": "ACTIVE",
        "registeredAt": registered_at,
        "registeredResources": [
            {"name": "CPU", "type": "INTEGER", "integerValue": registered_cpu},
            {"name": "MEMORY", "type": "INTEGER", "integerValue": registered_memory},
            {"name": "ENI", "type": "INTEGER", "integerValue": registered_eni},
        ],
        "remainingResources": [
            {"name": "CPU", "type": "INTEGER", "integerValue": remaining_cpu},
            {"name": "MEMORY", "type": "INTEGER", "integerValue": remaining_memory},
            {"name": "ENI", "type": "INTEGER", "integerValue": remaining_eni},
        ],
    }


def make_task(task_id, cpu=256, memory=512, group="service:test-app"):
    """Create a mock ECS task."""
    return {
        "taskArn": make_task_arn(task_id),
        "cpu": str(cpu),
        "memory": str(memory),
        "group": group,
        "lastStatus": "RUNNING",
    }


def setup_cluster(instances_config):
    """Set up mock cluster from config list.

    Each item: {
        "id": str,
        "az": str,  # optional, defaults to "eu-north-1a"
        "registered_cpu": int, "registered_memory": int,
        "remaining_cpu": int, "remaining_memory": int,
        "registered_eni": int, "remaining_eni": int,
        "age_minutes": int,
        "tasks": [{"id": str, "cpu": int, "memory": int, "group": str}]
    }
    """
    mock_ecs.reset()
    mock_ec2.reset()
    for cfg in instances_config:
        ci = make_container_instance(
            cfg["id"],
            registered_cpu=cfg.get("registered_cpu", 2048),
            registered_memory=cfg.get("registered_memory", 7680),
            remaining_cpu=cfg.get("remaining_cpu", 2048),
            remaining_memory=cfg.get("remaining_memory", 7680),
            registered_eni=cfg.get("registered_eni", 3),
            remaining_eni=cfg.get("remaining_eni", 3),
            age_minutes=cfg.get("age_minutes", 60),
        )
        arn = ci["containerInstanceArn"]
        ec2_id = ci["ec2InstanceId"]
        mock_ecs._container_instances.append(ci)
        mock_ecs._active_arns.append(arn)

        # Register AZ for this EC2 instance (defaults to eu-north-1a)
        if "az" in cfg:
            mock_ec2._az_map[ec2_id] = cfg["az"]

        tasks = []
        for t_cfg in cfg.get("tasks", []):
            tasks.append(make_task(
                t_cfg["id"],
                cpu=t_cfg.get("cpu", 256),
                memory=t_cfg.get("memory", 512),
                group=t_cfg.get("group", "service:test-app"),
            ))
        mock_ecs._tasks[arn] = tasks

    return mock_ecs


def add_draining_instances(count):
    """Add draining instance ARNs to the mock."""
    for i in range(count):
        mock_ecs._draining_arns.append(make_instance_arn(f"draining-{i}"))


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mock state before each test."""
    mock_ecs.reset()
    mock_ec2.reset()
