"""Tests for Karpenter-inspired ECS consolidation engine."""
import os
from datetime import datetime, timezone, timedelta

import pytest

from conftest import (
    mock_ecs, mock_ec2, make_container_instance, make_task, make_instance_arn,
    make_task_arn, setup_cluster, add_draining_instances,
)

import index


# ============================================================================
# Discovery
# ============================================================================


class TestDiscovery:
    def test_empty_cluster(self):
        state = index.discover_cluster_state("test-cluster")
        assert state["active_instances"] == []
        assert state["draining_count"] == 0

    def test_single_instance_with_tasks(self):
        setup_cluster([{
            "id": "aaa",
            "remaining_cpu": 1792,
            "remaining_memory": 7168,
            "remaining_eni": 2,
            "tasks": [{"id": "t1", "cpu": 256, "memory": 512}],
        }])
        state = index.discover_cluster_state("test-cluster")
        assert len(state["active_instances"]) == 1
        inst = state["active_instances"][0]
        assert inst["ec2_instance_id"] == "i-aaa"
        assert len(inst["tasks"]) == 1
        assert inst["tasks"][0]["cpu"] == 256
        assert inst["tasks"][0]["memory"] == 512

    def test_draining_count(self):
        setup_cluster([{"id": "aaa", "tasks": []}])
        add_draining_instances(3)
        state = index.discover_cluster_state("test-cluster")
        assert state["draining_count"] == 3
        assert len(state["active_instances"]) == 1

    def test_multiple_instances(self):
        setup_cluster([
            {"id": "aaa", "tasks": [{"id": "t1"}]},
            {"id": "bbb", "tasks": [{"id": "t2"}, {"id": "t3"}]},
        ])
        state = index.discover_cluster_state("test-cluster")
        assert len(state["active_instances"]) == 2

    def test_instance_without_tasks(self):
        setup_cluster([{"id": "aaa", "tasks": []}])
        state = index.discover_cluster_state("test-cluster")
        assert state["active_instances"][0]["tasks"] == []


# ============================================================================
# Metrics
# ============================================================================


class TestMetrics:
    def test_cpu_memory_utilization(self):
        inst = {
            "registered_cpu": 2048, "remaining_cpu": 1024,
            "registered_memory": 7680, "remaining_memory": 3840,
            "registered_at": datetime.now(timezone.utc) - timedelta(hours=2),
            "tasks": [{"arn": "t1"}],
        }
        index.compute_instance_metrics(inst)
        assert inst["cpu_utilization"] == pytest.approx(50.0)
        assert inst["memory_utilization"] == pytest.approx(50.0)
        assert inst["task_count"] == 1

    def test_zero_utilization(self):
        inst = {
            "registered_cpu": 2048, "remaining_cpu": 2048,
            "registered_memory": 7680, "remaining_memory": 7680,
            "registered_at": datetime.now(timezone.utc),
            "tasks": [],
        }
        index.compute_instance_metrics(inst)
        assert inst["cpu_utilization"] == 0
        assert inst["memory_utilization"] == 0
        assert inst["task_count"] == 0

    def test_age_calculation(self):
        inst = {
            "registered_cpu": 2048, "remaining_cpu": 2048,
            "registered_memory": 7680, "remaining_memory": 7680,
            "registered_at": datetime.now(timezone.utc) - timedelta(days=31, hours=5),
            "tasks": [],
        }
        index.compute_instance_metrics(inst)
        assert inst["age_days"] == 31
        assert inst["age_minutes"] > 31 * 24 * 60


# ============================================================================
# Disruption Budget
# ============================================================================


class TestBudget:
    def test_basic_budget(self):
        # 30% of 10 = 3
        assert index.calculate_disruption_budget(10, 0) == 3

    def test_budget_with_draining(self):
        # 30% of 10 = 3, minus 2 draining = 1
        assert index.calculate_disruption_budget(10, 2) == 1

    def test_budget_minimum_one(self):
        # 30% of 2 = 0.6, floor = 0, but min is 1
        assert index.calculate_disruption_budget(2, 0) == 1

    def test_budget_exhausted(self):
        # 30% of 5 = 1, minus 2 draining = -1, clamped to 0
        assert index.calculate_disruption_budget(5, 2) == 0

    def test_budget_single_instance(self):
        assert index.calculate_disruption_budget(1, 0) == 1


# ============================================================================
# Bin-Pack Simulation
# ============================================================================


class TestBinPack:
    def test_tasks_fit(self):
        tasks = [{"cpu": 256, "memory": 512}]
        targets = [{"remaining_cpu": 1024, "remaining_memory": 2048, "remaining_eni": 2}]
        assert index.can_bin_pack(tasks, targets) is True

    def test_cpu_insufficient(self):
        tasks = [{"cpu": 2048, "memory": 512}]
        targets = [{"remaining_cpu": 1024, "remaining_memory": 4096, "remaining_eni": 2}]
        assert index.can_bin_pack(tasks, targets) is False

    def test_memory_insufficient(self):
        tasks = [{"cpu": 256, "memory": 4096}]
        targets = [{"remaining_cpu": 2048, "remaining_memory": 2048, "remaining_eni": 2}]
        assert index.can_bin_pack(tasks, targets) is False

    def test_eni_insufficient(self):
        tasks = [{"cpu": 256, "memory": 512}, {"cpu": 256, "memory": 512}]
        targets = [{"remaining_cpu": 2048, "remaining_memory": 4096, "remaining_eni": 1}]
        assert index.can_bin_pack(tasks, targets) is False

    def test_empty_tasks(self):
        targets = [{"remaining_cpu": 1024, "remaining_memory": 2048, "remaining_eni": 2}]
        assert index.can_bin_pack([], targets) is True

    def test_no_targets(self):
        tasks = [{"cpu": 256, "memory": 512}]
        assert index.can_bin_pack(tasks, []) is False

    def test_exact_fit(self):
        tasks = [{"cpu": 1024, "memory": 2048}]
        targets = [{"remaining_cpu": 1024, "remaining_memory": 2048, "remaining_eni": 1}]
        assert index.can_bin_pack(tasks, targets) is True

    def test_ffd_ordering_matters(self):
        """Large task placed first prevents fragmentation."""
        tasks = [
            {"cpu": 256, "memory": 256},   # small
            {"cpu": 1024, "memory": 2048},  # large
        ]
        targets = [
            {"remaining_cpu": 1024, "remaining_memory": 2048, "remaining_eni": 1},
            {"remaining_cpu": 512, "remaining_memory": 512, "remaining_eni": 1},
        ]
        # FFD sorts large first → places on target[0], then small on target[1]
        assert index.can_bin_pack(tasks, targets) is True

    def test_multiple_tasks_across_targets(self):
        tasks = [
            {"cpu": 512, "memory": 1024},
            {"cpu": 512, "memory": 1024},
            {"cpu": 256, "memory": 512},
        ]
        targets = [
            {"remaining_cpu": 1024, "remaining_memory": 2048, "remaining_eni": 2},
            {"remaining_cpu": 512, "remaining_memory": 1024, "remaining_eni": 1},
        ]
        assert index.can_bin_pack(tasks, targets) is True


# ============================================================================
# Strategy 1: Empty Instances
# ============================================================================


class TestEmpty:
    def test_finds_empty(self):
        candidates = [
            {"task_count": 0, "arn": "a"},
            {"task_count": 2, "arn": "b"},
            {"task_count": 0, "arn": "c"},
        ]
        result = index.find_empty_instances(candidates)
        assert len(result) == 2
        assert result[0]["arn"] == "a"
        assert result[1]["arn"] == "c"

    def test_no_empty(self):
        candidates = [{"task_count": 1, "arn": "a"}, {"task_count": 3, "arn": "b"}]
        assert index.find_empty_instances(candidates) == []

    def test_all_empty(self):
        candidates = [{"task_count": 0, "arn": "a"}, {"task_count": 0, "arn": "b"}]
        assert len(index.find_empty_instances(candidates)) == 2


# ============================================================================
# Strategy 2: Expired Instances
# ============================================================================


class TestExpired:
    def test_finds_old_instances(self):
        candidates = [
            {"age_days": 31, "arn": "a"},
            {"age_days": 10, "arn": "b"},
            {"age_days": 45, "arn": "c"},
        ]
        result = index.find_expired_instances(candidates)
        assert len(result) == 2
        assert result[0]["arn"] == "c"  # oldest first
        assert result[1]["arn"] == "a"

    def test_no_expired(self):
        candidates = [{"age_days": 5, "arn": "a"}, {"age_days": 29, "arn": "b"}]
        assert index.find_expired_instances(candidates) == []

    def test_exact_boundary(self):
        candidates = [{"age_days": 30, "arn": "a"}]
        assert len(index.find_expired_instances(candidates)) == 1


# ============================================================================
# Strategy 3: Multi-Instance Consolidation
# ============================================================================


class TestMultiInstance:
    def _make_inst(self, arn, tasks, remaining_cpu=1024, remaining_memory=4096,
                   remaining_eni=2, cpu_util=10.0, memory_util=10.0,
                   task_count=None, age_minutes=60, az="eu-north-1a"):
        return {
            "arn": arn,
            "az": az,
            "tasks": [dict(t, az=az) for t in tasks],
            "remaining_cpu": remaining_cpu,
            "remaining_memory": remaining_memory,
            "remaining_eni": remaining_eni,
            "cpu_utilization": cpu_util,
            "memory_utilization": memory_util,
            "task_count": task_count if task_count is not None else len(tasks),
            "age_minutes": age_minutes,
        }

    def test_drains_two_when_tasks_fit(self):
        # 2 underutilized instances, 1 well-utilized with spare capacity
        inst_a = self._make_inst("a", [{"cpu": 256, "memory": 512}])
        inst_b = self._make_inst("b", [{"cpu": 256, "memory": 512}])
        inst_c = self._make_inst("c", [], remaining_cpu=2048, remaining_memory=8192,
                                 remaining_eni=3, cpu_util=0, memory_util=0, task_count=5)
        result = index.find_consolidation_set([inst_a, inst_b], [inst_a, inst_b, inst_c])
        assert len(result) == 2

    def test_no_drain_if_tasks_dont_fit(self):
        inst_a = self._make_inst("a", [{"cpu": 2048, "memory": 4096}])
        inst_b = self._make_inst("b", [{"cpu": 2048, "memory": 4096}],
                                 remaining_cpu=256, remaining_memory=256, remaining_eni=0)
        result = index.find_consolidation_set([inst_a], [inst_a, inst_b])
        assert len(result) == 0

    def test_greedy_stops_when_full(self):
        # Target can fit 1 task but not 2
        inst_a = self._make_inst("a", [{"cpu": 512, "memory": 1024}], cpu_util=5)
        inst_b = self._make_inst("b", [{"cpu": 512, "memory": 1024}], cpu_util=10)
        inst_c = self._make_inst("c", [], remaining_cpu=512, remaining_memory=1024,
                                 remaining_eni=1, cpu_util=0, memory_util=0, task_count=5)
        result = index.find_consolidation_set([inst_a, inst_b], [inst_a, inst_b, inst_c])
        assert len(result) == 1
        assert result[0]["arn"] == "a"  # lower util, added first

    def test_no_candidates(self):
        result = index.find_consolidation_set([], [])
        assert result == []

    def test_single_candidate_that_fits(self):
        inst_a = self._make_inst("a", [{"cpu": 256, "memory": 512}])
        inst_b = self._make_inst("b", [], remaining_cpu=1024, remaining_memory=2048,
                                 remaining_eni=2, task_count=3)
        result = index.find_consolidation_set([inst_a], [inst_a, inst_b])
        assert len(result) == 1


# ============================================================================
# Strategy 4: Single-Instance Consolidation
# ============================================================================


class TestSingleInstance:
    def _make_inst(self, arn, tasks, remaining_cpu=1024, remaining_memory=4096,
                   remaining_eni=2, cpu_util=10.0, memory_util=10.0,
                   task_count=None, age_minutes=60, az="eu-north-1a"):
        return {
            "arn": arn,
            "az": az,
            "tasks": [dict(t, az=az) for t in tasks],
            "remaining_cpu": remaining_cpu,
            "remaining_memory": remaining_memory,
            "remaining_eni": remaining_eni,
            "cpu_utilization": cpu_util,
            "memory_utilization": memory_util,
            "task_count": task_count if task_count is not None else len(tasks),
            "age_minutes": age_minutes,
        }

    def test_drains_first_fitting_candidate(self):
        inst_a = self._make_inst("a", [{"cpu": 256, "memory": 512}], cpu_util=5)
        inst_b = self._make_inst("b", [{"cpu": 256, "memory": 512}], cpu_util=10)
        inst_c = self._make_inst("c", [], remaining_cpu=512, remaining_memory=1024,
                                 remaining_eni=1, task_count=5)
        result = index.find_single_drain_candidate(
            [inst_a, inst_b], [inst_a, inst_b, inst_c]
        )
        assert len(result) == 1
        assert result[0]["arn"] == "a"

    def test_no_fit(self):
        inst_a = self._make_inst("a", [{"cpu": 2048, "memory": 4096}])
        inst_b = self._make_inst("b", [], remaining_cpu=256, remaining_memory=256,
                                 remaining_eni=0, task_count=5)
        result = index.find_single_drain_candidate([inst_a], [inst_a, inst_b])
        assert result == []

    def test_returns_at_most_one(self):
        inst_a = self._make_inst("a", [{"cpu": 256, "memory": 512}], cpu_util=5)
        inst_b = self._make_inst("b", [{"cpu": 256, "memory": 512}], cpu_util=10)
        inst_c = self._make_inst("c", [], remaining_cpu=2048, remaining_memory=8192,
                                 remaining_eni=3, task_count=5)
        result = index.find_single_drain_candidate(
            [inst_a, inst_b], [inst_a, inst_b, inst_c]
        )
        assert len(result) == 1


# ============================================================================
# Scoring
# ============================================================================


class TestScoring:
    def test_fewer_tasks_ranked_first(self):
        a = {"task_count": 1, "cpu_utilization": 50, "memory_utilization": 50, "age_minutes": 60}
        b = {"task_count": 3, "cpu_utilization": 10, "memory_utilization": 10, "age_minutes": 60}
        assert index.score_candidate(a) < index.score_candidate(b)

    def test_lower_utilization_ranked_first_same_tasks(self):
        a = {"task_count": 1, "cpu_utilization": 10, "memory_utilization": 10, "age_minutes": 60}
        b = {"task_count": 1, "cpu_utilization": 40, "memory_utilization": 40, "age_minutes": 60}
        assert index.score_candidate(a) < index.score_candidate(b)

    def test_older_ranked_first_same_util(self):
        a = {"task_count": 1, "cpu_utilization": 10, "memory_utilization": 10, "age_minutes": 120}
        b = {"task_count": 1, "cpu_utilization": 10, "memory_utilization": 10, "age_minutes": 30}
        assert index.score_candidate(a) < index.score_candidate(b)  # -120 < -30


# ============================================================================
# Stability (Min Age)
# ============================================================================


class TestStability:
    def test_young_instances_filtered(self):
        setup_cluster([
            {"id": "young", "age_minutes": 5, "tasks": []},
            {"id": "old", "age_minutes": 60, "tasks": []},
        ])
        result = index.handler({}, None)
        # Only "old" should be considered (young filtered out)
        assert result["statusCode"] == 200
        # In dry run, should see action for "old" only
        assert result["body"]["drained"] == 1

    def test_boundary_age_included(self):
        setup_cluster([
            {"id": "exact", "age_minutes": 15, "tasks": []},
            {"id": "other", "age_minutes": 60, "tasks": [{"id": "t1"}]},
        ])
        result = index.handler({}, None)
        assert result["body"]["drained"] == 1


# ============================================================================
# Dry Run
# ============================================================================


class TestDryRun:
    def test_dry_run_no_mutations(self):
        setup_cluster([
            {"id": "aaa", "age_minutes": 60, "tasks": []},
            {"id": "bbb", "age_minutes": 60, "tasks": [{"id": "t1"}]},
        ])
        # DRY_RUN is true by default in test env
        result = index.handler({}, None)
        assert result["body"]["drained"] == 1
        assert mock_ecs.drain_calls == []  # no actual API calls

    def test_not_dry_run_makes_calls(self, monkeypatch):
        setup_cluster([
            {"id": "aaa", "age_minutes": 60, "tasks": []},
            {"id": "bbb", "age_minutes": 60, "tasks": [{"id": "t1"}]},
        ])
        monkeypatch.setattr(index, "DRY_RUN", False)
        result = index.handler({}, None)
        assert result["body"]["drained"] == 1
        assert len(mock_ecs.drain_calls) == 1
        assert mock_ecs.drain_calls[0]["status"] == "DRAINING"


# ============================================================================
# Integration-Style Tests
# ============================================================================


class TestIntegration:
    def test_realistic_cluster_drains_empty(self):
        """5 instances, 1 empty → drain the empty one."""
        setup_cluster([
            {"id": "a", "remaining_cpu": 1024, "remaining_memory": 5632,
             "remaining_eni": 1, "tasks": [
                {"id": "t1", "cpu": 1024, "memory": 2048},
            ]},
            {"id": "b", "remaining_cpu": 1536, "remaining_memory": 6656,
             "remaining_eni": 2, "tasks": [
                {"id": "t2", "cpu": 512, "memory": 1024},
            ]},
            {"id": "c", "remaining_cpu": 1792, "remaining_memory": 7168,
             "remaining_eni": 2, "tasks": [
                {"id": "t3", "cpu": 256, "memory": 512},
            ]},
            {"id": "d", "remaining_cpu": 0, "remaining_memory": 0,
             "remaining_eni": 0, "tasks": [
                {"id": "t4", "cpu": 1024, "memory": 2048},
                {"id": "t5", "cpu": 512, "memory": 4096},
                {"id": "t6", "cpu": 512, "memory": 1024},
            ]},
            {"id": "e", "tasks": []},  # empty
        ])
        result = index.handler({}, None)
        assert result["body"]["drained"] == 1

    def test_all_well_utilized_no_drains(self):
        """All instances above thresholds → nothing to drain."""
        setup_cluster([
            {"id": "a", "remaining_cpu": 512, "remaining_memory": 1024,
             "remaining_eni": 1, "tasks": [
                {"id": "t1", "cpu": 1024, "memory": 4096},
                {"id": "t2", "cpu": 512, "memory": 2560},
            ]},
            {"id": "b", "remaining_cpu": 256, "remaining_memory": 512,
             "remaining_eni": 0, "tasks": [
                {"id": "t3", "cpu": 1024, "memory": 4096},
                {"id": "t4", "cpu": 512, "memory": 2048},
                {"id": "t5", "cpu": 256, "memory": 1024},
            ]},
        ])
        result = index.handler({}, None)
        assert result["body"]["drained"] == 0

    def test_two_underutilized_pack_onto_remaining(self, monkeypatch):
        """2 instances with 1 small task each, 1 instance with spare capacity → drain both."""
        monkeypatch.setattr(index, "DISRUPTION_BUDGET_PERCENT", 80)

        setup_cluster([
            # 2 underutilized: each has 1 small task using ~12% CPU, ~7% memory
            {"id": "a", "remaining_cpu": 1792, "remaining_memory": 7168,
             "remaining_eni": 2, "tasks": [
                {"id": "t1", "cpu": 256, "memory": 512},
            ]},
            {"id": "b", "remaining_cpu": 1792, "remaining_memory": 7168,
             "remaining_eni": 2, "tasks": [
                {"id": "t2", "cpu": 256, "memory": 512},
            ]},
            # Well-utilized but has spare capacity for 2 more tasks
            {"id": "c", "remaining_cpu": 1024, "remaining_memory": 2048,
             "remaining_eni": 2, "tasks": [
                {"id": "t3", "cpu": 1024, "memory": 4096},
            ]},
        ])
        result = index.handler({}, None)
        assert result["body"]["drained"] == 2

    def test_expired_instance_drained(self, monkeypatch):
        """Instance older than 30 days → drain even if well-utilized."""
        monkeypatch.setattr(index, "DISRUPTION_BUDGET_PERCENT", 100)
        setup_cluster([
            {"id": "old", "age_minutes": 45 * 24 * 60,
             "remaining_cpu": 0, "remaining_memory": 0,
             "remaining_eni": 0, "tasks": [
                {"id": "t1", "cpu": 1024, "memory": 4096},
                {"id": "t2", "cpu": 1024, "memory": 3584},
            ]},
            {"id": "new", "age_minutes": 60,
             "remaining_cpu": 2048, "remaining_memory": 7680,
             "remaining_eni": 3, "tasks": []},
            # Third instance stays alive so both "new" (S1) and "old" (S2) can drain
            {"id": "keep", "age_minutes": 60,
             "remaining_cpu": 2048, "remaining_memory": 7680,
             "remaining_eni": 3, "tasks": [{"id": "t3"}]},
        ])
        result = index.handler({}, None)
        # "new" drained by S1 (empty), "old" drained by S2 (expired), "keep" survives
        assert result["body"]["drained"] == 2

    def test_budget_limits_drains(self, monkeypatch):
        """Budget limits total drains across strategies."""
        monkeypatch.setattr(index, "DISRUPTION_BUDGET_PERCENT", 10)
        # 10% of 3 = 0.3, floor = 0, min = 1 → budget = 1
        setup_cluster([
            {"id": "a", "tasks": []},  # empty
            {"id": "b", "tasks": []},  # empty
            {"id": "c", "remaining_cpu": 0, "remaining_memory": 0,
             "remaining_eni": 0, "tasks": [
                {"id": "t1", "cpu": 2048, "memory": 7680},
            ]},
        ])
        result = index.handler({}, None)
        assert result["body"]["drained"] == 1  # budget caps at 1

    def test_no_active_instances(self):
        result = index.handler({}, None)
        assert result["statusCode"] == 200
        assert result["body"] == "No active instances"

    def test_budget_exhausted_with_draining(self, monkeypatch):
        """Already draining instances exhaust budget."""
        monkeypatch.setattr(index, "DISRUPTION_BUDGET_PERCENT", 30)
        setup_cluster([{"id": "a", "tasks": []}])
        add_draining_instances(1)  # budget = max(1, floor(1*0.3)) - 1 = 0
        result = index.handler({}, None)
        assert result["body"] == "Budget exhausted"


# ============================================================================
# AZ Safety
# ============================================================================


class TestAZSafe:
    def _make_inst(self, arn, az="eu-north-1a"):
        return {"arn": arn, "az": az}

    def test_safe_when_az_retains_minimum(self):
        remaining = [self._make_inst("b"), self._make_inst("c")]
        drain = [self._make_inst("a")]
        assert index.az_safe(remaining, drain) is True

    def test_unsafe_when_az_drops_below_minimum(self):
        remaining = []  # draining the only instance in the AZ
        drain = [self._make_inst("a")]
        assert index.az_safe(remaining, drain) is False

    def test_safe_when_min_is_zero(self, monkeypatch):
        monkeypatch.setattr(index, "MIN_INSTANCES_PER_AZ", 0)
        assert index.az_safe([], [self._make_inst("a")]) is True

    def test_multi_az_safe_when_each_az_retains_minimum(self):
        remaining = [
            self._make_inst("b", "eu-north-1a"),
            self._make_inst("d", "eu-north-1b"),
        ]
        drain = [
            self._make_inst("a", "eu-north-1a"),
            self._make_inst("c", "eu-north-1b"),
        ]
        assert index.az_safe(remaining, drain) is True

    def test_multi_az_unsafe_when_one_az_emptied(self):
        remaining = [self._make_inst("b", "eu-north-1a")]
        drain = [
            self._make_inst("a", "eu-north-1a"),
            self._make_inst("c", "eu-north-1b"),  # last in 1b
        ]
        assert index.az_safe(remaining, drain) is False


class TestAZBinPack:
    def test_cross_az_task_cannot_be_placed(self):
        """Task in AZ-a must not land on AZ-b instance."""
        tasks = [{"cpu": 256, "memory": 512, "az": "eu-north-1a"}]
        targets = [{"remaining_cpu": 2048, "remaining_memory": 8192,
                    "remaining_eni": 3, "az": "eu-north-1b"}]
        assert index.can_bin_pack(tasks, targets) is False

    def test_same_az_task_placed_normally(self):
        tasks = [{"cpu": 256, "memory": 512, "az": "eu-north-1a"}]
        targets = [{"remaining_cpu": 2048, "remaining_memory": 8192,
                    "remaining_eni": 3, "az": "eu-north-1a"}]
        assert index.can_bin_pack(tasks, targets) is True

    def test_multi_az_tasks_placed_in_correct_zones(self):
        """Two tasks in different AZs each need a target in their own AZ."""
        tasks = [
            {"cpu": 256, "memory": 512, "az": "eu-north-1a"},
            {"cpu": 256, "memory": 512, "az": "eu-north-1b"},
        ]
        targets = [
            {"remaining_cpu": 1024, "remaining_memory": 4096, "remaining_eni": 2, "az": "eu-north-1a"},
            {"remaining_cpu": 1024, "remaining_memory": 4096, "remaining_eni": 2, "az": "eu-north-1b"},
        ]
        assert index.can_bin_pack(tasks, targets) is True

    def test_multi_az_fails_when_one_az_has_no_capacity(self):
        tasks = [
            {"cpu": 256, "memory": 512, "az": "eu-north-1a"},
            {"cpu": 256, "memory": 512, "az": "eu-north-1b"},
        ]
        targets = [
            {"remaining_cpu": 1024, "remaining_memory": 4096, "remaining_eni": 2, "az": "eu-north-1a"},
            # no eu-north-1b target
        ]
        assert index.can_bin_pack(tasks, targets) is False


class TestAZConstraintsIntegration:
    def test_single_az_last_instance_not_drained(self, monkeypatch):
        """Last instance in a single-AZ cluster must never be drained even if empty."""
        monkeypatch.setattr(index, "MIN_INSTANCES_PER_AZ", 1)
        setup_cluster([{"id": "a", "tasks": []}])
        result = index.handler({}, None)
        assert result["body"]["drained"] == 0

    def test_single_az_second_empty_instance_drained(self, monkeypatch):
        """With 2 instances in same AZ, draining one empty instance is safe."""
        monkeypatch.setattr(index, "MIN_INSTANCES_PER_AZ", 1)
        setup_cluster([
            {"id": "a", "tasks": []},
            {"id": "b", "tasks": [{"id": "t1"}]},
        ])
        result = index.handler({}, None)
        assert result["body"]["drained"] == 1

    def test_multi_az_last_instance_per_az_blocked(self, monkeypatch):
        """One instance per AZ: neither can be drained (would leave an AZ empty)."""
        monkeypatch.setattr(index, "MIN_INSTANCES_PER_AZ", 1)
        setup_cluster([
            {"id": "a", "az": "eu-north-1a", "tasks": []},
            {"id": "b", "az": "eu-north-1b", "tasks": []},
        ])
        result = index.handler({}, None)
        assert result["body"]["drained"] == 0

    def test_multi_az_drains_from_az_with_spare(self, monkeypatch):
        """AZ-a has 2 instances, AZ-b has 1 — only the AZ-a empty instance should drain."""
        monkeypatch.setattr(index, "MIN_INSTANCES_PER_AZ", 1)
        setup_cluster([
            {"id": "a1", "az": "eu-north-1a", "tasks": []},
            {"id": "a2", "az": "eu-north-1a", "tasks": [{"id": "t1"}]},
            {"id": "b1", "az": "eu-north-1b", "tasks": []},
        ])
        result = index.handler({}, None)
        assert result["body"]["drained"] == 1
