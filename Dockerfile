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
FROM public.ecr.aws/lambda/python:3.14@sha256:252cbb0879717b1c049b9f909d86d06d88e129f3208386d7ba1189d535f5f981 AS build

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --target .

# ── Runtime image ─────────────────────────────────────────────────────────────
FROM public.ecr.aws/lambda/python:3.14@sha256:252cbb0879717b1c049b9f909d86d06d88e129f3208386d7ba1189d535f5f981

WORKDIR ${LAMBDA_TASK_ROOT}

# The base image pins dnf to the AL2023 release it was built from, so a plain
# upgrade finds nothing — --releasever=latest pulls current security fixes.
# pip is build-time only; dropping it also drops its vendored certifi/urllib3/
# msgpack/setuptools, which lag upstream and trip the image scan.
RUN microdnf -y --releasever=latest upgrade \
    && microdnf clean all \
    && pip uninstall -y --root-user-action=ignore pip

COPY --from=build --chown=65532:65532 /build .
COPY --chown=65532:65532 index.py .

USER 65532

# One-shot container — no long-running process to health-check
HEALTHCHECK NONE

CMD ["index.handler"]
