# stevedore

[![Test](https://github.com/advailo/stevedore/actions/workflows/test.yml/badge.svg)](https://github.com/advailo/stevedore/actions/workflows/test.yml)
[![Build](https://github.com/advailo/stevedore/actions/workflows/build.yml/badge.svg)](https://github.com/advailo/stevedore/actions/workflows/build.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Karpenter-inspired bin-packing engine for ECS EC2 clusters.**

Continuously right-sizes your cluster by draining underutilised EC2 instances so ECS managed scaling can terminate them, reducing cost without manual tuning. Ships as a Lambda function or a plain container — runs anywhere Python and boto3 run.

---

## Background

[Karpenter](https://karpenter.sh) (for Kubernetes) popularised the idea of a continuous consolidation loop: periodically find nodes whose workloads can be bin-packed onto other nodes, drain them gracefully, and let the cluster autoscaler reclaim the capacity. ECS has no equivalent built-in — this project brings the same pattern to ECS EC2 clusters.

**Prior art and related work**

- [Karpenter — Consolidation](https://karpenter.sh/docs/concepts/disruption/#consolidation) — the Kubernetes consolidation design this is modelled on.
- [AWS ECS Capacity Provider](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cluster-capacity-providers.html) — managed scaling handles scale-out and terminates fully-drained instances; stevedore handles the draining decision.
- [Amazon ECS Cluster Auto Scaling](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cluster-auto-scaling.html) — complementary: stevedore drains, CAS terminates.
- [Amazon ECS Managed Instances — Underutilized Instance Detection](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/managed-instances-infrastructure-optimization.html#underutilized-instance-detection) — AWS's native equivalent, but requires opting into the ECS Managed Instances provisioning model. Stevedore brings the same consolidation loop to standard EC2 capacity providers, with explicit controls over disruption budget, AZ safety, and age-based expiry.

---

## How it works

Each run executes four strategies in order, stopping as soon as one finds work to do:

| # | Strategy | Trigger | Bin-pack check | AZ check |
|---|----------|---------|----------------|----------|
| S1 | **Empty** | 0 running tasks | — | yes |
| S2 | **Expired** | instance age ≥ `MAX_INSTANCE_AGE_DAYS` | — | yes |
| S3 | **Multi** | any non-empty, non-expired instance | yes (greedy) | yes |
| S4 | **Single** | fallback when S3 finds nothing | yes (first fit) | yes |

S1 and S2 don't need a bin-pack check because ECS managed scaling provisions replacement capacity automatically when an instance is drained. S3 and S4 simulate task placement before committing.

### Bin-packing (First Fit Decreasing)

Tasks are sorted largest-first (CPU + memory), then placed on the first target instance with sufficient CPU + memory + ENI capacity.

- **AZ-aware** — tasks may only land on instances in the same availability zone, preventing placement failures after a drain.
- **ENI-aware** — instances that don't register ENI slots (bridge/host network mode) are treated as unconstrained on the ENI dimension.

### AZ safety guard

Before committing a drain, every AZ that would lose an instance must retain at least `MIN_INSTANCES_PER_AZ` (default 1) active instances. The check is applied greedily for batch drains: each candidate is evaluated against the already-committed drain set for that run.

### Disruption budget

`DISRUPTION_BUDGET_PERCENT` caps how many instances can be drained in a single run:

```
budget = max(1, floor(active_count × DISRUPTION_BUDGET_PERCENT / 100)) − currently_draining
```

If the budget is already exhausted by instances currently draining from a prior run, the engine skips.

### ECS Anywhere exclusion

Instances with `agentType=EXTERNAL` or an `ec2InstanceId` starting with `mi-` (SSM-managed instances) are always excluded — they are not managed by auto-scaling and must never be drained.

### Candidate scoring

When multiple instances qualify for S3/S4, they are sorted by:

1. Fewest running tasks — minimises task disruption
2. Lowest combined CPU + memory utilisation
3. Oldest instance age — prefer rotating stale instances

---

## Recommended cluster settings

Stevedore only handles the draining decision — these capacity provider and ASG settings are required for the full consolidation loop to work:

### Capacity provider

```hcl
resource "aws_ecs_capacity_provider" "ec2" {
  name = "my-capacity-provider"
  auto_scaling_group_provider {
    managed_draining               = "ENABLED"
    managed_termination_protection = "ENABLED"
    managed_scaling {
      status          = "ENABLED"
      target_capacity = 100
    }
  }
}
```

- **`managed_draining = ENABLED`** — when stevedore sets an instance to `DRAINING`, ECS waits for all tasks to stop before allowing the instance to be terminated.
- **`managed_termination_protection = ENABLED`** — prevents the ASG from terminating instances that still have running tasks. ECS automatically releases the protection once an instance is fully drained.
- **`target_capacity = 100`** — instructs CAS to scale in as soon as freed capacity allows; without this, drained instances may linger.

### Auto Scaling Group

```hcl
resource "aws_autoscaling_group" "ecs" {
  protect_from_scale_in = true   # pairs with managed_termination_protection
  max_instance_lifetime = 2592000  # 30 days — rotate stale instances (pairs with MAX_INSTANCE_AGE_DAYS)
}
```

- **`protect_from_scale_in = true`** — required alongside `managed_termination_protection`; the ASG will not scale in any instance unless ECS explicitly removes its protection.
- **`max_instance_lifetime`** — ASG attempts instance replacement after this period, but with `managed_termination_protection` enabled the ASG cannot actually terminate an instance that still has running tasks — it will stall indefinitely. Stevedore's S2 (Expired) strategy is what breaks this deadlock: it drains tasks off the aged instance first, which releases the termination protection and lets the ASG complete the replacement. Set `max_instance_lifetime` *longer* than `MAX_INSTANCE_AGE_DAYS` (converted to seconds) — for example, `MAX_INSTANCE_AGE_DAYS + 1 day` — so stevedore reliably gets there first. Setting them equal creates a race condition.

### ENI trunking (recommended)

In standard `awsvpc` mode each task consumes a full ENI, so an `m7g.large` (3 ENIs total, 1 reserved for the instance) can run at most 2 tasks regardless of available CPU and memory. ENI trunking replaces that constraint with a trunk ENI and per-task branch ENIs, raising the task limit [significantly depending on instance type](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/eni-trunking-supported-instance-types.html). This lets stevedore pack far more tasks onto surviving instances, making meaningful consolidation possible on clusters with many small tasks.

Enable it once at the account level — newly launched instances pick it up automatically:

```bash
aws ecs put-account-setting --name awsvpcTrunking --value enabled
```

Verify which instances have a trunk interface:

```bash
aws ecs list-attributes \
  --target-type container-instance \
  --attribute-name ecs.awsvpc-trunk-id \
  --cluster <cluster_name>
```

> **Note:** only instances launched *after* enabling the setting get the trunk interface. Recycle existing instances (e.g. via an instance refresh) to apply it cluster-wide. Also requires resource-based IPv4 DNS requests to be disabled on the launch template — see [AWS docs](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/container-instance-eni.html) for full prerequisites.

---

## Configuration

All settings are environment variables:

| Variable | Default | Description |
|---|---|---|
| `CLUSTER_NAME` | — | ECS cluster name (**required**) |
| `DISRUPTION_BUDGET_PERCENT` | `30` | Max % of active instances to drain per run |
| `MIN_INSTANCE_AGE_MINUTES` | `15` | Skip instances newer than this (warmup guard) |
| `MAX_INSTANCE_AGE_DAYS` | `30` | Drain instances older than this (S2 Expired) |
| `MIN_INSTANCES_PER_AZ` | `1` | Minimum live instances to keep per AZ |
| `DRY_RUN` | `true` | Log only, no actual drains |
| `LOG_LEVEL` | `INFO` | Python log level |

---

## Usage

### Container image

Pre-built multi-arch images (`linux/amd64` + `linux/arm64`) are published to Amazon ECR Public on every release:

```
public.ecr.aws/y8v9n2g8/stevedore:latest        # latest release
public.ecr.aws/y8v9n2g8/stevedore:v1            # latest v1.x
public.ecr.aws/y8v9n2g8/stevedore:v1.2          # latest v1.2.x
public.ecr.aws/y8v9n2g8/stevedore:v1.2.3        # exact version
```

> **Lambda users:** Lambda only supports private ECR repositories. Mirror the image to your own ECR before deploying — see [Mirroring to private ECR](#mirroring-to-private-ecr) below.

Pull and run a dry-run against your cluster:

```bash
docker run --rm \
  -e CLUSTER_NAME=my-cluster \
  -e DRY_RUN=true \
  -e AWS_REGION=us-east-1 \
  -v ~/.aws:/root/.aws:ro \
  --entrypoint python \
  public.ecr.aws/y8v9n2g8/stevedore:latest index.py
```

### Terraform module

Point at the GitHub source and pin to a release tag:

```hcl
module "stevedore" {
  source = "github.com/advailo/stevedore//terraform?ref=v1.2.3"

  name_prefix      = "myapp-prod"
  image_uri        = "123456789012.dkr.ecr.<region>.amazonaws.com/stevedore:v1.2.3"
  ecs_cluster_name = aws_ecs_cluster.main.name
  ecs_cluster_arn  = aws_ecs_cluster.main.arn
  vpc_id           = module.vpc.vpc_id
  subnet_ids       = module.vpc.private_subnets
}
```

Full working example: [`examples/terraform/`](examples/terraform/)

### CloudFormation

```bash
aws cloudformation deploy \
  --template-file examples/cloudformation/template.yaml \
  --stack-name stevedore \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    NamePrefix=myapp-prod \
    ImageUri=123456789012.dkr.ecr.<region>.amazonaws.com/stevedore:v1.2.3 \
    EcsClusterName=my-ecs-cluster \
    EcsClusterArn=arn:aws:ecs:us-east-1:123456789012:cluster/my-ecs-cluster \
    VpcId=vpc-abc123 \
    SubnetIds=subnet-aaa,subnet-bbb \
    DryRun=false
```

Full template: [`examples/cloudformation/template.yaml`](examples/cloudformation/template.yaml)

### Kubernetes CronJob

```bash
kubectl apply -f examples/container/k8s-cronjob.yaml
```

Update the `image` field to your private ECR URI (see [Mirroring to private ECR](#mirroring-to-private-ecr)) and set `CLUSTER_NAME` to your cluster. Full manifest: [`examples/container/k8s-cronjob.yaml`](examples/container/k8s-cronjob.yaml)

---

## Mirroring to private ECR

AWS Lambda only supports container images from private ECR repositories in the same region as the function. Mirror the image from ECR Public before deploying:

```bash
REGION=<region>
ACCOUNT=123456789012
VERSION=v1.2.3
ARCH=amd64  # or arm64 — match your Lambda architecture

aws ecr create-repository --repository-name stevedore --region $REGION 2>/dev/null || true
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker pull public.ecr.aws/y8v9n2g8/stevedore:$VERSION-$ARCH
docker tag public.ecr.aws/y8v9n2g8/stevedore:$VERSION-$ARCH $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/stevedore:$VERSION
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/stevedore:$VERSION
```

Then pass the private URI as `image_uri`:

```hcl
image_uri = "123456789012.dkr.ecr.<region>.amazonaws.com/stevedore:v1.2.3"
```

---

## Deployment (self-hosted image)

If you want to host the image in your own registry (e.g. ECR), build and push:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t 123456789012.dkr.ecr.us-east-1.amazonaws.com/stevedore:latest \
  --push .
```

---

## Running tests

No AWS credentials needed — the test suite uses an in-process mock.

```bash
pip install pytest
python -m pytest test_index.py -v
```

63 tests covering discovery, metrics, bin-pack, AZ safety, all four strategies, and integration scenarios.

---

## Known gaps

### Task slot limit (max tasks per instance)

The bin-pack simulation checks CPU + memory but not the maximum number of tasks an instance can run simultaneously. This limit varies by networking mode:

| Mode | Limit source | Tracked? |
|---|---|---|
| Standard awsvpc | Per-instance ENI count | Yes — handled via `registered_eni` |
| Trunk mode | Branch ENI limit per instance type | No |
| Bridge/host | `ECS_MAX_TASKS_PER_CONTAINER_INSTANCE` (default 120) | No |

Trunk mode is active when the container instance has the `ecs.awsvpc-trunk-id` attribute. Branch ENI limits are not queryable via any AWS API and would require a static lookup table per instance type. Because stevedore cannot verify ENI headroom on trunk-mode instances, bin-pack simulations may be optimistic — the target instance could have fewer available branch ENI slots than assumed. To compensate, reduce `DISRUPTION_BUDGET_PERCENT` (e.g. `10–15`) and raise `MIN_INSTANCES_PER_AZ` (e.g. `2`) so consolidation is more conservative and a mis-placed task has room to reschedule elsewhere.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
