import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import * as sns from 'aws-cdk-lib/aws-sns';

import { TurnLatencyObservabilityConstruct } from '../lib/constructs/observability/turn-latency-observability-construct';
import {
  AppConfig,
  OBSERVABILITY_DEFAULT_TURN_PRELUDE_MIN_TURNS,
  OBSERVABILITY_DEFAULT_TURN_PRELUDE_P50_MS,
} from '../lib/config';
import { createMockConfig, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

// Runtime *id* with its AWS-assigned suffix + endpoint qualifier, not the
// project prefix — a prefix-named group exists in no account and returns zero
// rows silently, which reads as "no traffic". Same fixture shape as the
// prompt-cache suite, guarding the same mistake.
const MOCK_RUNTIME_LOG_GROUP =
  `/aws/bedrock-agentcore/runtimes/${MOCK_PREFIX}_agentcore_runtime-AbC123XyZ0-DEFAULT`;

function synth(observability: Partial<AppConfig['observability']> = {}): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const config = createMockConfig();
  config.observability = { ...config.observability, ...observability };
  new TurnLatencyObservabilityConstruct(stack, 'TurnLatencyObservability', {
    config,
    runtimeLogGroupName: MOCK_RUNTIME_LOG_GROUP,
    alarmTopic: new sns.Topic(stack, 'Topic'),
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
      // Inside `tools` (docs/specs/turn-path-ttft.md P1a).
      'AgentBuildToolsFilterMs',
      'AgentBuildToolsGatewayMs',
      'AgentBuildToolsMcpMs',
      'AgentBuildToolsExtraMs',
      // Past the prelude (docs/specs/turn-path-ttft.md P1b).
      'FirstTokenMs',
      'HeadOfTurnMs',
      'HeadOfTurnHistoryCountMs',
      'PreModelMs',
      'ModelMs',
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

  it('leads with the client-measured first token, split by turn kind and prewarm', () => {
    // The only metrics that see the app-api hop, Runtime routing and session
    // start (docs/specs/turn-path-ttft.md §6). First turns and follow-ups are
    // separate names: a first turn starts its Runtime session.
    for (const name of [
      'ClientFirstTurnWarmedFirstTokenMs',
      'ClientFirstTurnColdFirstTokenMs',
      'ClientFirstTurnFirstTokenMs',
      'ClientFollowUpFirstTokenMs',
    ]) {
      expect(body).toContain(name);
    }
    // First graph on the board: before the server-side prelude row.
    expect(body.indexOf('User-perceived first token')).toBeGreaterThan(-1);
    expect(body.indexOf('User-perceived first token')).toBeLessThan(body.indexOf('Prelude total ('));
  });

  it('has a 30-day daily trend, the view that shows drift', () => {
    expect(body).toContain('-P30D');
    expect(body).toMatch(/period\\*":86400/);
  });

  it('points at the AgentCore board for the AWS-measured comparison', () => {
    // PreludeTotalMs vs AWS's data-plane Latency is how the routing overhead
    // becomes visible; a reader who does not know the companion exists cannot
    // make that comparison.
    expect(body).toContain(`${MOCK_PREFIX}-agentcore-observability`);
  });

  it('creates one alarm: the prelude p50, guarded by a turn floor', () => {
    // A gross-regression detector on harness time before the first token
    // (docs/specs/turn-path-ttft.md §6E). One resource against the 500 limit.
    t.resourceCountIs('AWS::CloudWatch::Alarm', 1);
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: `${MOCK_PREFIX}-turn-prelude-p50`,
      Threshold: OBSERVABILITY_DEFAULT_TURN_PRELUDE_P50_MS,
      ComparisonOperator: 'GreaterThanThreshold',
      EvaluationPeriods: 4,
      DatapointsToAlarm: 2,
      // A quiet period is not a slow one.
      TreatMissingData: 'notBreaching',
    });
  });

  it('counts a period only when it has enough turns, and reads the p50 over 6 hours', () => {
    const alarm = Object.values(t.findResources('AWS::CloudWatch::Alarm'))[0];
    const metrics = alarm.Properties.Metrics as Array<Record<string, any>>;
    const expression = metrics.find(m => m.Expression);
    // A quiet period's p50 is its cold starts (a dev day of 11 turns read 7.4 s).
    expect(expression?.Expression).toBe(`IF(turns >= ${OBSERVABILITY_DEFAULT_TURN_PRELUDE_MIN_TURNS}, p50, 0)`);
    const stats = Object.fromEntries(
      metrics.filter(m => m.MetricStat).map(m => [m.Id, m.MetricStat]),
    );
    expect(stats.p50.Stat).toBe('p50');
    expect(stats.turns.Stat).toBe('SampleCount');
    for (const stat of Object.values(stats) as Array<Record<string, any>>) {
      expect(stat.Metric.MetricName).toBe('PreludeTotalMs');
      expect(stat.Metric.Namespace).toBe('AgentCoreStack/TurnLatency');
      expect(stat.Period).toBe(6 * 3600);
    }
  });

  it('routes the alarm to the topic, firing and recovering', () => {
    const alarm = Object.values(t.findResources('AWS::CloudWatch::Alarm'))[0];
    expect(alarm.Properties.AlarmActions).toHaveLength(1);
    expect(alarm.Properties.OKActions).toHaveLength(1);
  });

  it('puts the alarm on the dashboard, threshold and all', () => {
    expect(body).toContain('Alarm: prelude p50 over 6 h');
    expect(body).toContain(`vs ${OBSERVABILITY_DEFAULT_TURN_PRELUDE_P50_MS} ms`);
  });

  it('honours a deployment threshold and floor', () => {
    const tuned = synth({ turnPreludeP50Ms: 1250, turnPreludeMinTurns: 150 });
    tuned.hasResourceProperties('AWS::CloudWatch::Alarm', { Threshold: 1250 });
    const alarm = Object.values(tuned.findResources('AWS::CloudWatch::Alarm'))[0];
    const expression = (alarm.Properties.Metrics as Array<Record<string, any>>).find(m => m.Expression);
    expect(expression?.Expression).toBe('IF(turns >= 150, p50, 0)');
  });

  it('creates no alarm when the threshold is 0', () => {
    synth({ turnPreludeP50Ms: 0 }).resourceCountIs('AWS::CloudWatch::Alarm', 0);
  });
});
