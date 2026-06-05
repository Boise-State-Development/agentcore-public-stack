---
title: Architecture Overview
description: High-level map of the AgentCore Public Stack — SPA, APIs, agent runtime, and the single CDK PlatformStack.
sidebar:
  order: 2
---

The platform is a multi-agent conversational AI system built on **AWS Bedrock
AgentCore** and **Strands Agents**. A request flows from the Angular single-page
app, through an edge tier and the App API, into the **AgentCore Runtime** that
runs the agent loop — which in turn uses Amazon Bedrock models plus the AgentCore
Memory, Gateway, Code Interpreter, and Browser primitives.

![AWS reference architecture for the AgentCore multi-agent chat platform, from SPA to agent runtime.](../../../assets/agentcore-platform-architecture.svg)

## How to read the diagram

The numbered steps trace a single chat request end to end:

1. **User access** — the browser loads the Angular SPA over HTTPS; Route 53 resolves the custom domain to CloudFront (TLS via ACM).
2. **SPA delivery** — CloudFront serves the SPA bundle from S3 over an Origin Access Control.
3. **API proxy** — CloudFront's `/api/*` behavior strips the prefix and proxies to the ALB; cookies, CSRF, and SSE pass through uncached.
4. **Authentication** — Cognito backs the BFF session-cookie flow and authorizes the Runtime invocation (JWT).
5. **Agent invocation** — the App API (ECS Fargate) forwards the chat to the Runtime's `/invocations` endpoint.
6. **Model inference** — the agent loop calls Amazon Bedrock (Claude); responses stream back to the SPA as SSE.
7. **Agent capabilities** — inside the **Amazon Bedrock AgentCore** boundary the Runtime orchestrates Memory (context), the Gateway (MCP tool Lambdas), Code Interpreter, and Browser.
8. **State & retrieval** — DynamoDB holds sessions, cost, quota, and settings; S3 Vectors serves RAG similarity search.
9. **Knowledge ingestion** — uploads to the knowledge-base bucket trigger a Lambda that embeds (Titan) and writes to S3 Vectors.
10. **Artifacts** — the App API drives a render Lambda that builds HTML, stored in S3 and served from a sandboxed CloudFront iframe.

CloudWatch, X-Ray, and IAM provide observability and least-privilege access across every component.

## Codebase map

| Layer | Where it lives |
| --- | --- |
| Angular SPA | `frontend/ai.client` |
| App API (BFF + REST + SSE) | `backend/src/apis/app_api` |
| Inference API (the agent loop; runs inside the AgentCore Runtime) | `backend/src/apis/inference_api` |
| Agent + tools | `backend/src/agents/main_agent` |
| Shared backend code | `backend/src/apis/shared` |
| Infrastructure (single CDK `PlatformStack`) | `infrastructure/lib/platform-stack.ts` |

:::note
The diagram's editable source (draw.io) and this SVG live in the repository under
`docs/architecture/`. To update this page, regenerate or edit the diagram there,
then copy the refreshed SVG into `docs-site/src/assets/`.
:::
