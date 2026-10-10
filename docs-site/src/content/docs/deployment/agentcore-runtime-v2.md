---
title: AgentCore Runtime V2
description: What the V2 Runtime changes, what has to be true before you switch an environment to it, and how to switch back.
sidebar:
  order: 9
---

The inference-api runs in an [AgentCore Runtime](/agentcore-public-stack/configuration/agentcore-services/).
AWS offers two platform versions of that Runtime. **V1** is the default and
what every deployment runs unless it opts in. **V2** starts sessions from a
snapshot of an already-booted container (about 2 s cold start regardless of
image size) and bills memory the session uses rather than the peak it
reserves, at a higher per-GB-hour rate.

The version is a per-environment setting, `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION`
(`V1` or `V2`). Switching is an in-place update of the Runtime that keeps its
id and endpoint. The engineering plan, with the measurements behind it, is
[`docs/specs/agentcore-runtime-v2.md`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/docs/specs/agentcore-runtime-v2.md).

:::caution[Do not set V2 on a release before the environment-variable change ships]
V2 limits a runtime's environment variables to **2,560 bytes**. Every
deployment of this stack through release 1.27.0 sends more than that (a
45-variable production stack is about 2,600 bytes by AWS's count; dev was
3,007), so `UpdateAgentRuntime` is rejected. CloudFormation's rollback then
fails the same way, and the stack is left `UPDATE_ROLLBACK_FAILED` with every
platform and backend deploy blocked until someone follows the recovery
runbook below. Chat keeps working throughout, because the `DEFAULT` endpoint
keeps serving the last `READY` version.

From the release after 1.27.0, `cdk synth` refuses a V2 deployment whose
payload would exceed the limit, so the failure cannot reach CloudFormation.
The release notes will say when the payload itself has been reduced and V2 is
safe to select.
:::

## Prerequisites

Before `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION=V2` in any environment:

1. **A release whose notes say V2 is selectable.** Two code changes have to be
   deployed first, and both arrive through ordinary releases:
   - the inference-api derives most resource names from `PROJECT_PREFIX` at
     process start, so the Runtime no longer has to be sent them (a no-op while
     it still is), and
   - CDK stops sending those variables, taking the payload to roughly half the
     limit, and adds a synth-time check that fails any V2 deployment over it.
2. **The payload check passes for your values.** It runs in `npx jest` and
   during `cdk synth`, so a Platform Stack run that gets as far as the deploy
   step has passed it. To see your own number before flipping anything:

   ```bash
   cd infrastructure && npx jest test/runtime-environment-payload-guard.test.ts --silent=false --verbose 2>&1 | grep env-payload
   ```

   The estimate errs high on purpose. If it is under 2,560 bytes, AWS will
   accept the update.
3. **AWS CLI 2.36.46 or later on the runner.** The backend deploy script
   refuses (exit 7) to roll an image on a CLI that does not know
   `platformVersion`, because an older CLI would silently put the runtime back
   on V1. GitHub's `ubuntu-24.04` image is fine.
4. **A resource-naming check with no drift warnings.** After the first of the
   two releases, app-api and the Runtime log one line at startup comparing the
   names they were sent with the names `PROJECT_PREFIX` derives. Look for
   `derived environment (prefix=…): … 0 drifted`. A `WARNING` naming a
   variable means one of your resources is not named `{prefix}-{suffix}`; do
   not proceed until it is understood.

## Switching an environment

1. Set `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION=V2` in the GitHub environment.
2. Run `platform.yml`. Synth validates the payload; the deploy updates the
   Runtime in place. Wait for the Runtime and its `DEFAULT` endpoint to report
   `READY`.
3. Confirm:

   ```bash
   aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id> --query 'platformVersion'
   ```

4. Run the next `backend.yml` as usual and check the same field again. The
   deploy script asserts the version did not change during the image roll
   (exit 8 if it did).
5. Watch first turns for `424` responses or doubled cold starts, and the
   inference-api log for `treating this as a snapshot restore`, which is the
   expected sign that a session was restored from a snapshot.

Switch one environment at a time, dev first, and give it a week before prod.

## Switching back

Set the variable to `V1` and run `platform.yml`. The property is always written
explicitly, V1 included, so this is a deterministic in-place update.

## Recovery runbook

If a Runtime update leaves the stack in `UPDATE_ROLLBACK_FAILED`:

1. Set `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION` back to `V1` (or the last
   working value) in the GitHub environment, so the next deploy does not repeat
   the failure.
2. Restore the runtime directly. Read the last `READY` version with
   `get-agent-runtime --agent-runtime-version <n>`, keep the fields
   `UpdateAgentRuntime` accepts, set `platformVersion` to `V1`, and call
   `update-agent-runtime`. Wait for the runtime and the `DEFAULT` endpoint to
   report `READY`.
3. Tell CloudFormation to skip the runtime, because step 2 already put it in a
   good state:

   ```bash
   aws cloudformation continue-update-rollback --stack-name <stack> --resources-to-skip <runtime logical id>
   ```

4. Re-run the failed Platform Stack deploy. It writes the explicit
   `PlatformVersion` and the current image, and brings CloudFormation's record
   back in line with the runtime.

On dev this took about ten minutes.

## Upgrading across the variable change

The release that stops sending the Runtime its resource names needs an
inference-api image that can derive them, which means one from the previous
release or later. CloudFormation re-registers the Runtime with whatever image
is live, so the order of the two deploys matters once.

You don't have to get it right by hand. Before `cdk deploy`, the Platform Stack
deploy reads a label off the live inference-api image. If the image is too old,
it stops without changing anything:

```
Runtime image cannot derive its resource names: ... Run the Backend Deploy workflow (backend.yml) first ...
```

Run `backend.yml`, then re-run `platform.yml`. If your deploy credentials can't
read ECR, or you run a custom image, the error says so; after confirming the
backend deploy has landed, set the GitHub variable
`SKIP_RUNTIME_ENV_CONTRACT_CHECK=true` for one run.

## What changes for operators of the Runtime's environment

Starting with the release after 1.27.0, the inference-api fills in any
resource-name variable the Runtime does not send, from `PROJECT_PREFIX` (and,
for three S3 buckets, the AWS account id). The list is
[`derived_environment.json`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/backend/src/apis/shared/config/derived_environment.json).
A variable that **is** set always wins, so:

- a local `backend/src/.env` can still point one name anywhere;
- a deployment that has renamed one of these resources must keep sending that
  variable explicitly (the startup drift warning will name it);
- nothing changes for app-api, which keeps every variable (ECS has no payload
  limit).
