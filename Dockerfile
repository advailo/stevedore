# syntax=docker/dockerfile:1
# ──────────────────────────────────────────────────────────────────────────────
# Multi-arch build — supports linux/amd64 and linux/arm64.
#
# Build:
#   docker buildx build --platform linux/amd64,linux/arm64 -t stevedore .
#
# Deployment modes (change only the CMD / command):
#
#   Lambda container image (default):
#     CMD ["index.handler"]
#     Push to ECR and point a Lambda function at the image URI.
#
#   ECS Scheduled Task / plain container:
#     Override CMD: python index.py
#     The handler runs once and exits — schedule it with EventBridge Scheduler
#     or an ECS Scheduled Rule.
#
#   Kubernetes CronJob:
#     command: ["python", "index.py"]
#     Schedule via a CronJob spec.
# ──────────────────────────────────────────────────────────────────────────────

# ── Build stage: install dependencies into /build ────────────────────────────
# Pinned by digest (Dependabot's docker ecosystem update keeps this current) —
# the tag alone is mutable and floats to whatever AWS republishes under 3.14.
FROM public.ecr.aws/lambda/python:3.14@sha256:75d28f3dc8491bb39ac6e47e5e64baabc1690bc116cd9a46b5cbd9ae38c7bd59 AS build

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --target .

# ── Runtime image ─────────────────────────────────────────────────────────────
FROM public.ecr.aws/lambda/python:3.14@sha256:75d28f3dc8491bb39ac6e47e5e64baabc1690bc116cd9a46b5cbd9ae38c7bd59

WORKDIR ${LAMBDA_TASK_ROOT}

COPY --from=build --chown=65532:65532 /build .
COPY --chown=65532:65532 index.py .

USER 65532

# One-shot container — no long-running process to health-check
HEALTHCHECK NONE

CMD ["index.handler"]
