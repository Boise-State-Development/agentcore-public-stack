---
title: Governance
description: How the platform governs models, tools, connectors, spend, administration and change, each rule linked to the code that enforces it, and what it does not do.
sidebar:
  order: 0
---

Every rule on this page is enforced by code in this repository, and each links to it. Read it, test it, or change it in your fork: governance here is something you can check, not a vendor's promise.

## What you govern, and where it is enforced

| Area | The rule | Enforced in |
|---|---|---|
| **Models** | A role grant decides who may use which model. Every service calls one access rule, and a model an administrator disables cannot run, even for a role holding every model. | [`rbac/model_access.py`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/develop/backend/src/apis/shared/rbac/model_access.py), [RBAC](/agentcore-public-stack/configuration/rbac/), [Models](/agentcore-public-stack/admin/models/) |
| **Tools** | Each tool is a role grant. An administrator can require the user's approval before a tool runs. Code the agent executes is checked by a policy that does not depend on the prompt, so no instruction can talk its way past it. | [`hooks/tool_approval.py`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/develop/backend/src/agents/main_agent/session/hooks/tool_approval.py), [`security/python_ast_policy.py`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/develop/backend/src/apis/shared/security/python_ast_policy.py), [Tools](/agentcore-public-stack/admin/tools/) |
| **Connectors** | Administrators register MCP servers and sign-in providers (25 built in, plus any custom OAuth provider). Each user consents to a provider inside the conversation, and the agent acts with that user's own account. A user who disconnects must consent again. | [`shared/oauth/`](https://github.com/Boise-State-Development/agentcore-public-stack/tree/develop/backend/src/apis/shared/oauth), [Connectors](/agentcore-public-stack/admin/connectors/) |
| **Spend** | Quota tiers by role, department or person, each set to warn or to block at its limit. Every model call is attributed to its user, session, agent and model. The cost reports read a fixed list of fields that holds no conversation text, and a test fails if they ever return one. | [`observability/content_policy.py`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/develop/backend/src/apis/shared/observability/content_policy.py), [Quotas](/agentcore-public-stack/admin/quotas/), [Cost analytics](/agentcore-public-stack/admin/cost-analytics/) |
| **Administration** | Admin areas are delegated from a closed list with no wildcard. Role administration and identity-provider setup can never be delegated, because whoever controls either controls every grant. | [`rbac/admin_scopes.py`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/develop/backend/src/apis/shared/rbac/admin_scopes.py) |
| **Audit** | Every admin change is recorded with the values before and after, kept for 365 days. A denied attempt to escalate is recorded too. | [`audit/models.py`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/develop/backend/src/apis/shared/audit/models.py) |
| **Change** | New capabilities ship switched off, and a deployment turns on what it chooses. The flags page marks which ones cost money. | [`feature_flags.py`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/develop/backend/src/apis/shared/feature_flags.py), [Feature flags](/agentcore-public-stack/configuration/feature-flags/) |
| **Quality** | Every release is scored against committed per-model baselines, and the results are published, failures included. | [`backend/evals/`](https://github.com/Boise-State-Development/agentcore-public-stack/tree/develop/backend/evals), [Benchmarks](/agentcore-public-stack/benchmarks/) |

## Where it runs

The platform deploys into your own AWS account, in your region, as one CDK stack. Conversations, documents and knowledge bases stay there, under your IAM, CloudTrail and service control policies. Model usage bills through your AWS account and the agreement you already have.

## What it does not do

Governance claims are only useful if their limits are stated too.

- **No content filtering.** The platform does not screen prompts or answers with a content filter such as Amazon Bedrock Guardrails. A model's own refusals apply, and when a provider declines a request the user sees a notice saying so.
- **No conversation audit or eDiscovery.** The audit log covers administrative changes and project changes, not every conversation. Conversations are kept for a year in your account, but there is no hold, search or export built for records requests.
- **No certifications of its own.** The platform holds no SOC 2, HIPAA, FERPA or FedRAMP attestation. Compliance comes from your institution's own AWS agreement and how you deploy and operate it.
- **AWS only.** It is built for institutions whose cloud is AWS and does not run elsewhere.
