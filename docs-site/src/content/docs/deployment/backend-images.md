---
title: Backend Images
description: Build and ship the service images.
sidebar:
  order: 3
---

`backend.yml` ships application code **without touching CloudFormation**. It
builds each service, pushes the image (or zip), and calls the matching AWS API to
roll the live service over to it. This is the workflow you run most — every
backend code change goes out this way.

## What `backend.yml` does

Each service has a build job followed by a deploy job. The jobs run in parallel,
and content-hash short-circuiting means unchanged services don't rebuild.

| Service | Build | Deploy |
|---------|-------|--------|
| **app-api** | `build-one.sh app-api` (linux/amd64) | `aws ecs register-task-definition` + `update-service` + `wait services-stable` |
| **inference-api** | `build-one.sh inference-api` (linux/arm64, runs on AgentCore Runtime) | `aws bedrock-agentcore-control update-agent-runtime` (full-replacement payload), polled to `READY` |
| **rag-ingestion** | `build-one.sh rag-ingestion` (image Lambda) | `aws lambda update-function-code --image-uri` |
| **artifact-render** | Zips `backend/src/lambdas/artifact_render/` directly — no Docker | `aws lambda update-function-code` |

## Content-hash builds

Each build computes a **SHA-256** of the Dockerfile, the tracked source tree, and
the dependency manifests, then tags the image with that hash. If ECR already has
that tag, `docker build` and `docker push` are skipped entirely. Pushing a change
to one service rebuilds only that service; everything else deploys in seconds.

## The SSM image-tag handoff

After every push, the build pipeline writes the new tag to
`/<prefix>/<service>/image-tag` in SSM. The CDK compute constructs read those
parameters at deploy time, so any later CloudFormation re-registration of the
task definition or Runtime picks up the **latest live image** — never the
bootstrap stub that `platform.yml` first seeded. This is what keeps code and
infrastructure deploys fully decoupled.

## When each job runs

Only the jobs whose source changed actually do work:

| You changed | Jobs that run |
|-------------|---------------|
| `backend/src/apis/app_api/**` | `build-app-api` + `deploy-app-api-code` |
| `backend/src/apis/inference_api/**` or `backend/src/agents/**` | `build-inference-api` + `deploy-inference-api-code` |
| `backend/src/lambdas/rag_ingestion/**` | `build-rag-ingestion` + `deploy-rag-ingestion-code` |
| `backend/src/lambdas/artifact_render/**` | `deploy-artifact-render-code` (the zip is built in the deploy job) |

You don't need to gate `backend.yml` behind `platform.yml` on routine changes —
it only depends on infrastructure that already exists.
