"""Karpenter-inspired ECS consolidation engine.

Continuously reconciles cluster state by draining underutilized, empty, and
expired instances. Uses bin-pack simulation (FFD) to verify tasks can be
relocated before draining.

Strategies (executed in order):
  1. Empty     — instances with 0 running tasks
  2. Expired   — instances older than MAX_INSTANCE_AGE_DAYS
  3. Multi     — greedy set of underutilized instances (bin-pack validated)
  4. Single    — fallback: one underutilized instance (bin-pack validated)
"""

import json
import logging
import math
import os
from collections import Counter
from datetime import datetime, timezone

import boto3

# ============================================================================
# Configuration
# ============================================================================

CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "default-cluster")
DISRUPTION_BUDGET_PERCENT = int(os.environ.get("DISRUPTION_BUDGET_PERCENT", "30"))
MIN_INSTANCE_AGE_MINUTES = int(os.environ.get("MIN_INSTANCE_AGE_MINUTES", "15"))
MAX_INSTANCE_AGE_DAYS = int(os.environ.get("MAX_INSTANCE_AGE_DAYS", "30"))
MIN_INSTANCES_PER_AZ = int(os.environ.get("MIN_INSTANCES_PER_AZ", "1"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# ============================================================================
# Logging
# ============================================================================

COMPONENT = "stevedore"


class JsonFormatter(logging.Formatter):
    EXTRA_KEYS = (
        "strategy", "instance_id", "task_count", "cpu_util", "memory_util",
        "drain_count", "budget", "event_type", "age_days", "reason",
    )

    def format(self, record):
        log = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "component": COMPONENT,
            "message": record.getMessage(),
        }
        for key in self.EXTRA_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                log[key] = val
        return json.dumps(log, default=str)


logger = logging.getLogger(COMPONENT)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    logger.propagate = False

# ============================================================================
# AWS Client
# ============================================================================

ecs = boto3.client("ecs")
ec2 = boto3.client("ec2")

# ============================================================================
# Discovery
# ============================================================================


def _paginate_container_instances(cluster, status):
    """List all container instance ARNs with pagination."""
    arns = []
    kwargs = {"cluster": cluster, "status": status}
    while True:
        resp = ecs.list_container_instances(**kwargs)
        arns.extend(resp.get("containerInstanceArns", []))
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return arns


def _paginate_tasks(cluster, container_instance_arn):
    """List all task ARNs for a container instance with pagination."""
    arns = []
    kwargs = {
        "cluster": cluster,
        "containerInstance": container_instance_arn,
        "desiredStatus": "RUNNING",
    }
    while True:
        resp = ecs.list_tasks(**kwargs)
        arns.extend(resp.get("taskArns", []))
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return arns


def _get_resource(resources, name):
    """Extract integer value from ECS resource list by name."""
    for r in resources:
        if r["name"] == name:
            return r.get("integerValue", 0)
    return 0


def discover_cluster_state(cluster):
    """Discover all instances and their tasks with full pagination."""
    active_arns = _paginate_container_instances(cluster, "ACTIVE")
    draining_arns = _paginate_container_instances(cluster, "DRAINING")

    if not active_arns:
        return {"active_instances": [], "draining_count": len(draining_arns)}

    # Describe active instances in batches of 100
    instances = []
    for i in range(0, len(active_arns), 100):
        batch = active_arns[i:i + 100]
        resp = ecs.describe_container_instances(
            cluster=cluster, containerInstances=batch
        )
        instances.extend(resp.get("containerInstances", []))

    # Filter out EXTERNAL (ECS Anywhere) instances — they are not managed by
    # auto-scaling and must never be drained by consolidation.
    # agentType may be missing from API response, so also check ec2InstanceId
    # prefix: "mi-" = managed instance (ECS Anywhere), "i-" = EC2.
    instances = [ci for ci in instances
                 if ci.get("agentType", "ec2") != "EXTERNAL"
                 and not ci.get("ec2InstanceId", "").startswith("mi-")]

    # Fetch AZ for all EC2 instances in one batched call
    ec2_ids = [ci["ec2InstanceId"] for ci in instances]
    az_map = {}
    for i in range(0, len(ec2_ids), 200):
        resp = ec2.describe_instances(InstanceIds=ec2_ids[i:i + 200])
        for reservation in resp["Reservations"]:
            for inst_info in reservation["Instances"]:
                az_map[inst_info["InstanceId"]] = inst_info["Placement"]["AvailabilityZone"]

    # Build instance records with their tasks
    active_instances = []
    for ci in instances:
        arn = ci["containerInstanceArn"]
        az = az_map.get(ci["ec2InstanceId"], "unknown")

        # Get tasks for this instance
        task_arns = _paginate_tasks(cluster, arn)
        tasks = []
        for j in range(0, len(task_arns), 100):
            batch = task_arns[j:j + 100]
            resp = ecs.describe_tasks(cluster=cluster, tasks=batch)
            for t in resp.get("tasks", []):
                tasks.append({
                    "arn": t["taskArn"],
                    "cpu": int(t.get("cpu", "0")),
                    "memory": int(t.get("memory", "0")),
                    "group": t.get("group", ""),
                    "az": az,
                })

        active_instances.append({
            "arn": arn,
            "ec2_instance_id": ci["ec2InstanceId"],
            "az": az,
            "registered_at": ci["registeredAt"],
            "registered_cpu": _get_resource(ci["registeredResources"], "CPU"),
            "registered_memory": _get_resource(ci["registeredResources"], "MEMORY"),
            "remaining_cpu": _get_resource(ci["remainingResources"], "CPU"),
            "remaining_memory": _get_resource(ci["remainingResources"], "MEMORY"),
            "registered_eni": _get_resource(ci["registeredResources"], "ENI"),
            "remaining_eni": _get_resource(ci["remainingResources"], "ENI"),
            "tasks": tasks,
        })

    return {
        "active_instances": active_instances,
        "draining_count": len(draining_arns),
    }


# ============================================================================
# Metrics
# ============================================================================


def compute_instance_metrics(instance):
    """Add utilization percentages and derived fields."""
    reg_cpu = instance["registered_cpu"]
    reg_mem = instance["registered_memory"]

    instance["cpu_utilization"] = (
        ((reg_cpu - instance["remaining_cpu"]) / reg_cpu * 100) if reg_cpu else 0
    )
    instance["memory_utilization"] = (
        ((reg_mem - instance["remaining_memory"]) / reg_mem * 100) if reg_mem else 0
    )
    instance["task_count"] = len(instance["tasks"])

    registered_at = instance["registered_at"]
    if registered_at.tzinfo is None:
        registered_at = registered_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - registered_at
    instance["age_minutes"] = age.total_seconds() / 60
    instance["age_days"] = age.days


# ============================================================================
# Disruption Budget
# ============================================================================


def calculate_disruption_budget(active_count, draining_count):
    """How many instances can be drained this run."""
    max_drainable = max(1, math.floor(active_count * DISRUPTION_BUDGET_PERCENT / 100))
    return max(0, max_drainable - draining_count)


# ============================================================================
# AZ Safety Guard
# ============================================================================


def az_safe(remaining, drain_candidates):
    """Return True if remaining instances keep every drained AZ at >= MIN_INSTANCES_PER_AZ."""
    if MIN_INSTANCES_PER_AZ <= 0:
        return True
    az_counts = Counter(i.get("az", "default") for i in remaining)
    for inst in drain_candidates:
        az = inst.get("az", "default")
        if az_counts.get(az, 0) < MIN_INSTANCES_PER_AZ:
            return False
    return True


# ============================================================================
# Candidate Scoring
# ============================================================================


def score_candidate(instance):
    """Lower score = better drain candidate."""
    return (
        instance["task_count"],
        instance["cpu_utilization"] + instance["memory_utilization"],
        -instance["age_minutes"],
    )


# ============================================================================
# Bin-Pack Simulation (First Fit Decreasing)
# ============================================================================


def can_bin_pack(tasks, target_instances):
    """Check if tasks fit on target instances (CPU + memory + ENI), respecting AZ boundaries.

    Tasks are only placed on instances in the same AZ. In a single-AZ cluster all
    instances share one AZ so the behaviour is identical to the previous version.
    Falls back to az="default" for any record that lacks the field (unit tests).
    """
    if not tasks:
        return True
    if not target_instances:
        return False

    sorted_tasks = sorted(tasks, key=lambda t: t["cpu"] + t["memory"], reverse=True)

    # Build per-AZ mutable capacity pools (FFD within each zone).
    # eni_constrained=False when registered_eni==0: instance doesn't track ENI
    # slots (bridge/host network mode) so the constraint doesn't apply.
    by_az: dict = {}
    for inst in target_instances:
        az = inst.get("az", "default")
        # ENI is only a constraint when the instance actually registers ENI slots
        # (awsvpc mode). In bridge/host mode registered_eni==0 and the check
        # must be skipped. Unit-test targets may omit registered_eni; fall back
        # to remaining_eni>0 as a "this target tracks ENI" signal.
        reg_eni = inst.get("registered_eni", inst.get("remaining_eni", 0))
        by_az.setdefault(az, []).append({
            "remaining_cpu": inst["remaining_cpu"],
            "remaining_memory": inst["remaining_memory"],
            "remaining_eni": inst["remaining_eni"],
            "eni_constrained": reg_eni > 0,
        })

    for task in sorted_tasks:
        task_az = task.get("az", "default")
        placed = False
        for target in by_az.get(task_az, []):
            eni_ok = (not target["eni_constrained"]) or (target["remaining_eni"] >= 1)
            if (target["remaining_cpu"] >= task["cpu"]
                    and target["remaining_memory"] >= task["memory"]
                    and eni_ok):
                target["remaining_cpu"] -= task["cpu"]
                target["remaining_memory"] -= task["memory"]
                if target["eni_constrained"]:
                    target["remaining_eni"] -= 1
                placed = True
                break
        if not placed:
            return False

    return True


# ============================================================================
# Strategy 1: Empty Instances
# ============================================================================


def find_empty_instances(candidates):
    """Instances with zero running tasks."""
    return [inst for inst in candidates if inst["task_count"] == 0]


# ============================================================================
# Strategy 2: Expired Instances
# ============================================================================


def find_expired_instances(candidates):
    """Instances older than MAX_INSTANCE_AGE_DAYS. No bin-pack check needed —
    ECS managed scaling provisions replacement capacity when tasks are relocated."""
    return sorted(
        [inst for inst in candidates if inst["age_days"] >= MAX_INSTANCE_AGE_DAYS],
        key=lambda i: -i["age_days"],  # oldest first
    )


# ============================================================================
# Strategy 3: Multi-Instance Consolidation
# ============================================================================


def find_consolidation_set(candidates, all_active):
    """Greedy multi-instance: add candidates to drain set while bin-pack and AZ safety pass."""
    sorted_candidates = sorted(candidates, key=score_candidate)
    drain_set = []
    drain_tasks = []

    for candidate in sorted_candidates:
        trial_drain = drain_set + [candidate]
        trial_tasks = drain_tasks + candidate["tasks"]

        drain_arns = {inst["arn"] for inst in trial_drain}
        remaining = [inst for inst in all_active if inst["arn"] not in drain_arns]

        if can_bin_pack(trial_tasks, remaining) and az_safe(remaining, trial_drain):
            drain_set.append(candidate)
            drain_tasks = trial_tasks

    return drain_set


# ============================================================================
# Strategy 4: Single-Instance Consolidation
# ============================================================================


def find_single_drain_candidate(candidates, all_active):
    """Evaluate each candidate independently — drain the first whose tasks fit and AZ is safe."""
    for candidate in sorted(candidates, key=score_candidate):
        remaining = [inst for inst in all_active if inst["arn"] != candidate["arn"]]
        if can_bin_pack(candidate["tasks"], remaining) and az_safe(remaining, [candidate]):
            return [candidate]
    return []


# ============================================================================
# Drain Execution
# ============================================================================


def drain_instances(cluster, instances, dry_run):
    """Set instances to DRAINING. Returns count of successfully drained."""
    drained = 0
    for inst in instances:
        extra = {
            "instance_id": inst["ec2_instance_id"],
            "task_count": inst["task_count"],
            "cpu_util": round(inst["cpu_utilization"], 1),
            "memory_util": round(inst["memory_utilization"], 1),
            "age_days": inst["age_days"],
        }
        if dry_run:
            logger.info("DRY RUN: would drain %s", inst["ec2_instance_id"], extra=extra)
            drained += 1
        else:
            try:
                ecs.update_container_instances_state(
                    cluster=cluster,
                    containerInstances=[inst["arn"]],
                    status="DRAINING",
                )
                logger.info("Drained %s", inst["ec2_instance_id"], extra=extra)
                drained += 1
            except Exception as e:
                logger.error(
                    "Failed to drain %s: %s", inst["ec2_instance_id"], e, extra=extra
                )
    return drained


# ============================================================================
# Handler
# ============================================================================


def handler(event, context):
    logger.info(
        "Starting consolidation",
        extra={
            "event_type": "start",
            "strategy": "all",
            "budget": DISRUPTION_BUDGET_PERCENT,
        },
    )

    state = discover_cluster_state(CLUSTER_NAME)
    active = state["active_instances"]

    if not active:
        logger.info("No active instances", extra={"event_type": "skip"})
        return {"statusCode": 200, "body": "No active instances"}

    # Compute metrics
    for inst in active:
        compute_instance_metrics(inst)

    # Budget
    budget = calculate_disruption_budget(len(active), state["draining_count"])
    logger.info(
        "Budget: %d (active=%d, draining=%d)",
        budget, len(active), state["draining_count"],
        extra={"event_type": "budget", "budget": budget},
    )
    if budget <= 0:
        logger.info("Budget exhausted", extra={"event_type": "skip"})
        return {"statusCode": 200, "body": "Budget exhausted"}

    # Filter young instances
    candidates = [
        inst for inst in active if inst["age_minutes"] >= MIN_INSTANCE_AGE_MINUTES
    ]

    to_drain = []
    strategy_used = None

    # Strategy 1: Empty
    empty = find_empty_instances(candidates)
    if empty:
        # Greedy AZ-safe filter: commit each drain before evaluating the next
        # so we never drop an AZ below MIN_INSTANCES_PER_AZ in the same batch.
        sim_active = [i for i in active if i["arn"] not in {x["arn"] for x in to_drain}]
        safe_empty = []
        for e in empty:
            remaining = [i for i in sim_active if i["arn"] != e["arn"]]
            if az_safe(remaining, [e]):
                safe_empty.append(e)
                sim_active = remaining
        if safe_empty:
            take = safe_empty[:budget]
            to_drain.extend(take)
            strategy_used = "empty"
            for inst in take:
                logger.info(
                    "S1 empty: %s", inst["ec2_instance_id"],
                    extra={"strategy": "empty", "instance_id": inst["ec2_instance_id"]},
                )

    # Strategy 2: Expired
    remaining_budget = budget - len(to_drain)
    if remaining_budget > 0:
        expired = find_expired_instances(
            [c for c in candidates if c not in to_drain]
        )
        if expired:
            sim_active = [i for i in active if i["arn"] not in {x["arn"] for x in to_drain}]
            safe_expired = []
            for e in expired:
                remaining = [i for i in sim_active if i["arn"] != e["arn"]]
                if az_safe(remaining, [e]):
                    safe_expired.append(e)
                    sim_active = remaining
            if safe_expired:
                take = safe_expired[:remaining_budget]
                to_drain.extend(take)
                strategy_used = strategy_used or "expired"
                for inst in take:
                    logger.info(
                        "S2 expired: %s (%d days)",
                        inst["ec2_instance_id"], inst["age_days"],
                        extra={
                            "strategy": "expired",
                            "instance_id": inst["ec2_instance_id"],
                            "age_days": inst["age_days"],
                        },
                    )

    # Strategy 3: Multi-instance consolidation
    # All non-empty, non-expired instances are candidates — bin-pack + AZ safety
    # are the sole gates; no CPU/memory threshold pre-filter.
    remaining_budget = budget - len(to_drain)
    multi = []
    consolidation_candidates = []
    if remaining_budget > 0:
        consolidation_candidates = [
            c for c in candidates
            if c["task_count"] > 0
            and c["age_days"] < MAX_INSTANCE_AGE_DAYS
            and c not in to_drain
        ]
        if consolidation_candidates:
            drain_arns = {inst["arn"] for inst in to_drain}
            active_for_sim = [i for i in active if i["arn"] not in drain_arns]
            multi = find_consolidation_set(consolidation_candidates, active_for_sim)
            if multi:
                take = multi[:remaining_budget]
                to_drain.extend(take)
                strategy_used = strategy_used or "multi"
                for inst in take:
                    logger.info(
                        "S3 multi: %s", inst["ec2_instance_id"],
                        extra={
                            "strategy": "multi",
                            "instance_id": inst["ec2_instance_id"],
                            "task_count": inst["task_count"],
                        },
                    )

    # Strategy 4: Single-instance fallback (only if S3 found nothing)
    remaining_budget = budget - len(to_drain)
    if remaining_budget > 0 and not multi and consolidation_candidates:
        drain_arns = {inst["arn"] for inst in to_drain}
        active_for_sim = [i for i in active if i["arn"] not in drain_arns]
        single = find_single_drain_candidate(consolidation_candidates, active_for_sim)
        if single:
            take = single[:remaining_budget]
            to_drain.extend(take)
            strategy_used = strategy_used or "single"
            for inst in take:
                logger.info(
                    "S4 single: %s", inst["ec2_instance_id"],
                    extra={
                        "strategy": "single",
                        "instance_id": inst["ec2_instance_id"],
                        "task_count": inst["task_count"],
                    },
                )

    # Drain
    drained = drain_instances(CLUSTER_NAME, to_drain, DRY_RUN)

    logger.info(
        "Consolidation complete: drained=%d strategy=%s dry_run=%s",
        drained, strategy_used or "none", DRY_RUN,
        extra={
            "event_type": "summary",
            "drain_count": drained,
            "budget": budget,
            "strategy": strategy_used or "none",
        },
    )

    return {"statusCode": 200, "body": {"drained": drained, "dry_run": DRY_RUN}}


if __name__ == "__main__":
    handler({}, None)
