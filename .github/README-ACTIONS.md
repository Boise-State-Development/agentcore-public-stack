# Deploy AgentCore Public Stack with GitHub Actions

Deploy a production-ready multi-agent AI platform to your AWS account in about 45 minutes. This guide walks you through every step.

> **TL;DR — Ready to begin?**
>
> ### 👉 [Start here — Step 1: Prerequisites](./docs/deploy/step-01-prerequisites.md)

## What You'll Deploy

| Component | Description |
|-----------|-------------|
| **VPC + ALB + ECS** | Networking, load balancer, and container orchestration |
| **Fine-Tuning** *(optional)* | SageMaker training/inference infrastructure, S3 artifact storage, DynamoDB job tracking |
| **Artifacts** *(optional)* | Iframe-isolated artifact rendering (DDB metadata, S3 content, CloudFront at `artifacts.{domain}`, Lambda render service) |
| **RAG Ingestion** | Document ingestion pipeline for retrieval-augmented generation |
| **Inference API** | Strands Agent runtime powered by AWS Bedrock AgentCore |
| **App API** | Backend REST API for chat, sessions, admin, and auth |
| **Frontend** | Angular SPA served via CloudFront CDN |
| **Gateway** | Lambda-based MCP tool endpoints (Wikipedia, ArXiv, Finance, etc.) |
| **Bootstrap Data** | Auth provider config, default models, roles, and tools |

---

## Deployment Steps

Follow each step in order. Click a step to open its guide.

| | Step | Time | Difficulty |
|---|------|------|------------|
| **1** | [Prerequisites](./docs/deploy/step-01-prerequisites.md) | ~5 min | Easy |
| **2** | [AWS Setup](./docs/deploy/step-02-aws-setup.md) | ~15 min | Moderate |
| **3** | [Configure GitHub](./docs/deploy/step-03-github-config.md) | ~10 min | Moderate |
| **4** | [Deploy Workflows](./docs/deploy/step-04-deploy.md) | ~20 min | Easy |
| **5** | [Verify Deployment](./docs/deploy/step-05-verify.md) | ~5 min | Easy |

> [!TIP]
> Most of the time is spent waiting for AWS resources to provision. The actual hands-on work is straightforward.

---

## Quick Links

- [Troubleshooting](./docs/deploy/troubleshooting.md) — common issues and fixes
- [Full Configuration Reference](./ACTIONS-REFERENCE.md) — every available variable and secret
