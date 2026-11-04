---
title: AWS Limits
description: Monitor the AWS account's service quotas for this stack and request AWS limit increases.
sidebar:
  order: 4
---

Usage & Spend group. **AWS Limits** lists the AWS account's applied service
quotas for the services this stack depends on, with current usage where AWS
publishes a usage metric, and lets an admin request an increase. Managed under
`/admin/aws-limits`, scope `admin.aws_limits`.

These are limits on the **AWS account**, not the per-user spending limits on the
[Quotas](/agentcore-public-stack/admin/quotas/) page.

Off by default while in development: set `CDK_AWS_LIMITS_ENABLED=true` (app-api,
and the Service Quotas IAM grants) and build the SPA with `features.awsLimits: true`.

## What the page shows

Quotas are per account and per Region. The page reads the Region the stack is
deployed in. Limits are grouped by the part of the stack that depends on them:

| Group | Service | Shown when |
|---|---|---|
| Model inference | Bedrock requests and tokens per minute (on-demand, cross-Region, global, and the bedrock-mantle endpoint) | Always |
| Agent runtime | AgentCore Runtime sessions, Memory, Code Interpreter, Browser, Gateway | Always |
| Background jobs | Lambda concurrent executions | Always |
| App API | Fargate On-Demand vCPU, ECS tasks per service | Always |
| Data | DynamoDB tables and per-table throughput | Always |
| Knowledge bases (RAG) | A link to the S3 Vectors limits, which are not in Service Quotas | Always |
| Fine-tuning | SageMaker training and Batch Transform quotas for the G5, G6 and G6e instance types | Fine-tuning is on |
| Dictation | Amazon Transcribe concurrent streams | Dictation is on |

By default, Bedrock rows are limited to the models in the managed-model catalog
(the admin **Models** page). **Show all Bedrock quotas** lists every Bedrock quota.

Each row shows the applied value (and the AWS default, when it differs), usage,
utilization and a link to the quota in the Service Quotas console. Utilization
turns amber at 80% and red at 95%.

### How usage is measured

Usage is the **high point of the last 7 days**, so a quiet day still shows where
the account normally peaks. Hover a value to see the CloudWatch metric behind it.

- **Rates and tokens** (requests per second, tokens per minute) use the busiest
  minute. A per-second quota shows that minute's average rate, so a burst inside
  the minute can run higher.
- **Counts** (concurrent sessions, Lambda concurrency, tables) use the highest
  value recorded.
- **Bedrock model quotas** have no usage metric in Service Quotas, so they are
  measured from Bedrock's own per-model metrics: `EstimatedTPMQuotaUsage` for
  tokens per minute (it counts output tokens at their quota burndown rate, as
  AWS does) and `Invocations` for requests per minute. A cross-Region quota
  counts calls through the geographic profile (`us.…`), a global quota calls
  through `global.…`, and an on-demand quota calls to the model itself. A model
  nobody called that way this week shows 0%.
- **Other quotas without a Service Quotas metric** are measured from the
  service's own metrics: Code Interpreter and Browser sessions from AgentCore's
  `ActiveSessionCount`, long-term memory extraction from AgentCore's input and
  output token usage (both counted, so it errs high), the gateway tool-call rate
  from gateway `Invocations`, and DynamoDB per-table throughput from consumed
  capacity on the busiest table.
- **ECS tasks per service** is the busiest service's `RunningTaskCount`, which
  needs Container Insights. Without it the row says so.
- **Counts of what exists** (Memories, Gateways, Targets per gateway) are read
  from the AgentCore APIs and show the **current** number, since a count has no
  history. The column heading says which kind a group shows: "7-day peak",
  "Current count", or "Usage" with each row labelled when a group has both.
- **Rows without a number say why**: "Limit is 0. Request an increase to use
  this." (many SageMaker instance types start at 0), the bedrock-mantle endpoint
  (not published to CloudWatch), long-context variants ("1M Context Length",
  which Bedrock reports with the base model), or "Not measured by AWS".

**Hide unused** hides rows with no use in the last 7 days: a peak or count of 0,
or nothing measured. **Adjustable only** hides quotas Service Quotas cannot
raise. Both are on when the page opens; clear them to see every monitored quota.

AWS data is cached for about ten minutes. **Refresh** reads it again.

The first load and each refresh read about 2,000 quotas from Service Quotas
(Bedrock and SageMaker alone are about 26 pages each) and a week of CloudWatch
data, so they take about 10 to 30 seconds. That time depends on how many quotas
AWS lists, not on traffic, so production takes as long as development. The page
says so while it loads. The cache belongs to each App API task, so with more
than one task a later load can land on a task that has not read AWS yet.

## Requesting AWS limit increases

**Request AWS increase** appears on adjustable quotas for admins who may submit
increases. Enter a value higher than the applied one, review it, and submit. The
app asks Service Quotas for the increase and shows the status AWS returns.

Every submission is recorded in the audit log as `aws_limit.increase_requested`
with the admin, the quota, the current and requested values and the AWS request
ID. AWS itself records the App API's role as the requester, so the audit record is
the only place that says which admin asked. **View all** on the *Open increase
requests* card opens every request AWS still lists for the account and Region,
with that admin, **Outside this app** for requests made in the console or CLI, or
**Automatic Management** for requests AWS made itself. Approved requests are
hidden by default; clear **Hide approved requests** to see them.

### Lead times

Small increases are often approved automatically within minutes. Larger ones go
to a person at AWS and can take several business days. Model tokens-per-minute
increases are reviewed against regional capacity: plan about two weeks for
high-demand models, and request ahead of a launch rather than during it.

### Support cases

When an increase needs manual review, Service Quotas opens an AWS Support case
for it. The app sends `SupportCaseAllowed=true`; with `false`, AWS would mark such
a request `NOT_APPROVED` instead of opening a case. The case ID appears in the
history once AWS assigns it. Follow up on the case in the AWS Support console.

The App API role has no `support:*` permissions. Service Quotas opens the case
through its own service-linked role.

### Regions and cross-Region inference

Each Region has its own quotas. Cross-Region and global inference profiles have
their own quotas, separate from on-demand ones, counted in the Region the request
is sent from (the stack's Region). Raise the quota for the inference type the
platform calls: a managed model with a `us.` or `global.` prefix uses the
cross-Region or global quota, not the on-demand one.

### The first request on a fresh account

The first increase anyone requests in an account creates the Service Quotas
service-linked role, `AWSServiceRoleForServiceQuotas`. The App API role may create
that one role (`AwsLimitsServiceLinkedRole`). If the request still fails with
`DependencyAccessDeniedException`, the page says so; create the role once by
requesting any increase from the Service Quotas console, then retry.

### Non-adjustable quotas

Some quotas cannot be raised through Service Quotas, and their rows have no
request button. To ask for an exception, open a case in the AWS Support Center
(Service limit increase), naming the quota and the Region, or use the console
link on the row to check whether the quota has become adjustable. A non-adjustable
limit is usually a design constraint: the AWS documentation for the service
describes how to work within it.

## Automatic Management

The banner shows whether Service Quotas Automatic Management is on for the
account. When on, AWS watches utilization and notifies you before a quota runs
out. Turn it on in the Service Quotas console; the app does not configure it.
