import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { TurnLatencyObservabilityConstruct } from '../lib/constructs/observability/turn-latency-observability-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

// Runtime *id* with its AWS-assigned suffix + endpoint qualifier, not the
// project prefix — a prefix-named group exists in no account and returns zero
// rows silently, which reads as "no traffic". Same fixture shape as the
// prompt-cache suite, guarding the same mistake.
const MOCK_RUNTIME_LOG_GROUP =
  `/aws/bedrock-agentcore/runtimes/${MOCK_PREFIX}_agentcore_runtime-AbC123XyZ0-DEFAULT`;

function synth(): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  new TurnLatencyObservabilityConstruct(stack, 'TurnLatencyObservability', {
    config: createMockConfig(),
    runtimeLogGroupName: MOCK_RUNTIME_LOG_GROUP,
  });
  return Template.fromStack(stack);
}

describe('TurnLatencyObservabilityConstruct', () => {
  let t: Template;
  let body: string;

  beforeAll(() => {
    t = synth();
    const dashboards = t.findResources('AWS::CloudWatch::Dashboard');
    body = JSON.stringify(Object.values(dashboards)[0].Properties.DashboardBody);
  });

  it('creates the dashboard with the conventional name', () => {
    t.hasResourceProperties('AWS::CloudWatch::Dashboard', {
      DashboardName: `${MOCK_PREFIX}-turn-latency-observability`,
    });
  });

  it('graphs every stage turn_timing.py emits', () => {
    // A name that drifts from `_metric_name()` renders an EMPTY graph rather
    // than erroring — indistinguishable from "that stage never ran".
    for (const metric of [
      'PreludeTotalMs',
      'PreambleMs',
      'PreambleOwnershipMs',
      'PreambleSkillsMs',
      'PreambleFilesMs',
      'PreambleSessionStateMs',
      'PreambleQuotaMs',
      'RagMs',
      'ToolsMs',
      'AgentBuildMs',
      // Sub-stages of the build (docs/specs/turn-latency-preamble.md PR-4).
      'AgentBuildPromptMs',
      'AgentBuildRegistryMs',
      'AgentBuildSessionMgrMs',
      'AgentBuildToolsMs',
      'AgentBuildHooksMs',
      'AgentBuildPluginsMs',
      'AgentBuildFinalizeMs',
      'AgentBuildRestMs',
    ]) {
      expect(body).toContain(metric);
    }
  });

  it('uses the namespace turn_timing.py defaults to', () => {
    // CDK does not set TURN_LATENCY_EMF_NAMESPACE (dev and prod are separate
    // accounts), so its default IS the contract between the two sides.
    expect(body).toContain('AgentCoreStack/TurnLatency');
  });

  it('plots percentiles, never Sum — a summed latency is meaningless', () => {
    expect(body).toContain('p50');
    expect(body).toContain('p90');
    expect(body).toContain('p99');
    expect(body).not.toContain('"stat":"Sum"');
  });

  it('queries the runtime log group for the slices dimensions would have made', () => {
    expect(body).toContain(MOCK_RUNTIME_LOG_GROUP);
    // isResume / deferredBuild ride as EMF log properties rather than metric
    // dimensions: dimensions multiply metric streams and invite reading a p99
    // off a slice too thin to have one.
    expect(body).toContain('isResume');
    expect(body).toContain('deferredBuild');
  });

  it('keeps an unaccounted-time widget, so an incomplete decomposition shows', () => {
    expect(body).toContain('unaccountedMs');
  });

  it('carries the "this cannot see the app-api hop" caveat on the dashboard itself', () => {
    // The caveat has to live where the number is read. A reader who sees
    // PreambleMs fall and concludes the user waits less is wrong by up to
    // ~1.5s of routing this namespace is structurally blind to, and a comment
    // in the CDK source does not reach them.
    expect(body).toContain('handler entry');
    expect(body).toContain('tests/load');
  });

  it('points at the AgentCore board for the AWS-measured comparison', () => {
    // PreludeTotalMs vs AWS's data-plane Latency is how the routing overhead
    // becomes visible; a reader who does not know the companion exists cannot
    // make that comparison.
    expect(body).toContain(`${MOCK_PREFIX}-agentcore-observability`);
  });

  it('creates no alarms — there is no baseline to threshold against yet', () => {
    // Deliberate. Inventing a threshold before the dashboard has run is the
    // guessing docs/specs/turn-latency-preamble.md exists to prevent.
    t.resourceCountIs('AWS::CloudWatch::Alarm', 0);
  });
});
