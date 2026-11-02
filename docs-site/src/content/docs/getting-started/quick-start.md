---
title: Quick Start
description: The fastest path from a fork to a running stack.
sidebar:
  order: 3
---

This platform runs on AWS resources — DynamoDB, Cognito, Bedrock, and AgentCore —
so there's no offline "run it in five minutes" mode. The fastest real path is to
**deploy once, then develop locally against that environment.**

## 1. Deploy a stack

Fork the repository, wire up your AWS credentials and a handful of GitHub
variables, and run the deploy workflows. The
[Deployment](/agentcore-public-stack/deployment/overview/) section walks the
whole sequence — most of the ~45 minutes is AWS provisioning, not hands-on work.

When it's done you'll have a working environment: a frontend URL, an API, and the
SSM parameters that local development reads its configuration from.

## 2. Run locally against it

With a stack deployed (yours or a shared development environment), run the
backend and the Angular SPA on your machine, pointed at that environment's
resources. The [Local Development](/agentcore-public-stack/local-development/)
page covers installing dependencies, configuring `.env` from SSM, and starting
the services.

## 3. Use it from a terminal (optional)

Every release attaches `aps-tui`, a terminal client for the same assistant.
Install it with [uv](https://docs.astral.sh/uv/) from the release page of your
deployment and run `aps-tui`; the first launch asks for your deployment's
address and signs you in through the browser. See
[Terminal Client](/agentcore-public-stack/features/terminal-client/).

## Where to go next

- [Architecture Overview](/agentcore-public-stack/getting-started/architecture-overview/) — how the agents, APIs, and infrastructure fit together.
- [Deployment Overview](/agentcore-public-stack/deployment/overview/) — stand up your own stack.
- [Local Development](/agentcore-public-stack/local-development/) — develop against a deployed environment.
- [Terminal Client](/agentcore-public-stack/features/terminal-client/) — the assistant in your shell, with tools that run on your machine.
