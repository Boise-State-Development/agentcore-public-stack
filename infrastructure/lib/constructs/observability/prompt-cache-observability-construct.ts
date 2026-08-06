import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface PromptCacheObservabilityConstructProps {
  config: AppConfig;
  /**
   * The log group the AgentCore Runtime actually writes to, from
   * `InferenceAgentCoreConstruct.runtimeLogGroupName`.
   *
   * Must be passed, not derived: the group is service-created and named
   * after the runtime *id* (AWS-assigned suffix), which is only knowable
   * from the runtime resource. This construct previously guessed
   * `/aws/bedrock-agentcore/runtimes/<prefix>` — a group nothing writes to,
   * so both Logs Insights widgets below returned empty results and read as
   * "no traffic" rather than "wrong query".
   */
  runtimeLogGroupName: string;
}

/**
 * PromptCacheObservabilityConstruct — fleet-wide dashboard + alarms for
 * the prompt-cache EMF metrics emitted by
 * `backend/src/apis/shared/observability/emf.py` (PR #697).
 *
 * The metrics arrive as raw EMF JSON on stdout from BOTH compute
 * surfaces (inference-api via the AgentCore Runtime log group, app-api
 * via ECS awslogs) into the shared `AgentCoreStack/PromptCache`
 * namespace — the default of the `EMF_NAMESPACE` env var, which is
 * deliberately NOT set in CDK (dev and prod are separate AWS accounts,
 * so the unscoped namespace never collides). All metrics are
 * dimension-less and designed for `Sum`; per-call detail (`modelId`,
 * `sessionId`, `cacheStatus`) rides as queryable EMF log properties,
 * hence the Logs Insights widget below rather than dimension fan-out.
 *
 * This is the first cross-service dashboard, so it lives in its own
 * construct area instead of inside the inference-api construct. The
 * per-session drill-down counterpart is the cost-anatomy admin endpoint
 * (`GET /admin/costs/sessions/{id}/calls`).
 *
 * Alarms are console-only — no SNS topics exist in the stack yet (same
 * posture as kb-sync and scheduled-runs). They use NOT_BREACHING for
 * missing data because the `PROMPT_CACHE_OBSERVABILITY_ENABLED=false`
 * kill switch (or simply zero traffic) makes the metrics absent
 * entirely.
 */
export class PromptCacheObservabilityConstruct extends Construct {
  constructor(
    scope: Construct,
    id: string,
    props: PromptCacheObservabilityConstructProps,
  ) {
    super(scope, id);

    const { config, runtimeLogGroupName } = props;

    // Must match the default of EMF_NAMESPACE in emf.py.
    const namespace = 'AgentCoreStack/PromptCache';

    const cacheReadTokensMetric = new cloudwatch.Metric({
      namespace,
      metricName: 'CacheReadTokens',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    const cacheWriteTokensMetric = new cloudwatch.Metric({
      namespace,
      metricName: 'CacheWriteTokens',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    const avoidableMissMetric = new cloudwatch.Metric({
      namespace,
      metricName: 'AvoidableMiss',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    // A call that read a leading prefix segment and re-wrote the rest against
    // a live cache entry. Its own metric rather than a roll-in to
    // AvoidableMiss so the existing alarm keeps its meaning; its dollars are
    // inside WastedUsd, with PartialMissUsd naming the subset.
    const partialMissMetric = new cloudwatch.Metric({
      namespace,
      metricName: 'PartialMiss',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    const wastedUsdMetric = new cloudwatch.Metric({
      namespace,
      metricName: 'WastedUsd',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    const partialMissUsdMetric = new cloudwatch.Metric({
      namespace,
      metricName: 'PartialMissUsd',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    // One session's *cumulative* partial-miss waste, emitted after each
    // rollup bump. Maximum (not Sum) over a long period is the point: the
    // question is "is any single conversation over the line", which a fleet
    // sum cannot answer — the motivating incident spent $27 over five days
    // at ~$0.43 a turn without ever stepping a fleet-wide number.
    const sessionPartialMissUsdMetric = new cloudwatch.Metric({
      namespace,
      metricName: 'SessionPartialMissUsd',
      statistic: 'Maximum',
      period: cdk.Duration.hours(24),
    });

    // Cache efficiency: fraction of cache traffic served from cache.
    // Healthy steady-state conversations read far more than they write,
    // so this should sit near 100%; a prefix-stability regression shows
    // up as a sustained drop (writes displacing reads).
    const cacheEfficiencyExpression = new cloudwatch.MathExpression({
      expression: '100 * reads / (reads + writes)',
      usingMetrics: {
        reads: cacheReadTokensMetric,
        writes: cacheWriteTokensMetric,
      },
      label: 'Cache efficiency (%)',
      period: cdk.Duration.minutes(5),
    });

    // ============================================================
    // Dashboard
    // ============================================================

    const dashboard = new cloudwatch.Dashboard(this, 'PromptCacheDashboard', {
      dashboardName: getResourceName(config, 'prompt-cache-observability'),
      defaultInterval: cdk.Duration.hours(3),
    });

    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: `# Prompt Cache Observability\n**Project:** ${config.projectPrefix} | **Region:** ${config.awsRegion} — fleet-wide EMF metrics from app-api + inference-api. Per-session drill-down: \`GET /admin/costs/sessions/{id}/calls\`.`,
        width: 24,
        height: 1,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Cache Tokens (Read vs Write)',
        left: [cacheReadTokensMetric, cacheWriteTokensMetric],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Cache Efficiency (reads / total cache traffic)',
        left: [cacheEfficiencyExpression],
        leftYAxis: { min: 0, max: 100 },
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Cache Misses (avoidable vs partial)',
        left: [avoidableMissMetric, partialMissMetric],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Wasted USD (total, and the partial-miss share)',
        left: [wastedUsdMetric, partialMissUsdMetric],
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Worst single session — cumulative partial-miss waste (24h)',
        left: [sessionPartialMissUsdMetric],
        width: 24,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.LogQueryWidget({
        title: 'Model Calls by cacheStatus (inference-api)',
        // Referenced by name (not a typed LogGroup ref) since a dashboard
        // widget creates no CFN dependency. app-api's ECS log group is
        // auto-named, so its (much smaller) share of emissions isn't
        // queried here.
        logGroupNames: [runtimeLogGroupName],
        queryLines: [
          'filter ispresent(cacheStatus)',
          'stats count(*) as calls by cacheStatus',
          'sort calls desc',
        ],
        width: 24,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.LogQueryWidget({
        title: 'Sessions by partial-miss waste (which session did the alarm mean?)',
        logGroupNames: [runtimeLogGroupName],
        // sessionId is a log property, not a metric dimension (unbounded
        // cardinality), so the alarm says *that* a session crossed the line
        // and this widget says *which*.
        queryLines: [
          'filter ispresent(SessionPartialMissUsd)',
          'stats max(SessionPartialMissUsd) as wastedUsd, max(sessionPartialMissCount) as partialMisses by sessionId',
          'sort wastedUsd desc',
          'limit 20',
        ],
        width: 24,
        height: 6,
      }),
    );

    // ============================================================
    // Alarms (console-only; no SNS wiring yet)
    // ============================================================

    // AvoidableMiss is the nominated alarm target (see emf.py): a
    // prefix-stability regression flips a large share of calls to
    // `miss_avoidable`, showing up as a step change in this sum.
    new cloudwatch.Alarm(this, 'PromptCacheAvoidableMissAlarm', {
      alarmName: getResourceName(config, 'prompt-cache-avoidable-miss'),
      alarmDescription:
        'Avoidable prompt-cache misses exceeded threshold — likely a prompt-prefix stability regression',
      metric: avoidableMissMetric,
      threshold: config.production ? 10 : 50,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Session-level accumulation (#833 PR-1). A fleet sum never notices one
    // conversation spending $0.43 a turn for five days — the incident this
    // alarm exists for would have tripped it on day 2, at ~10 turns. The
    // metric is the session's cumulative partial-miss waste and the statistic
    // is Maximum, so this reads as "a session at or over $5 of partial-miss
    // waste was active in the last 24h"; it clears once that session stops.
    new cloudwatch.Alarm(this, 'PromptCacheSessionPartialMissAlarm', {
      alarmName: getResourceName(config, 'prompt-cache-session-partial-miss'),
      alarmDescription:
        'A single session accumulated more than $5 of partial-miss cache waste — one conversation is re-writing its prefix every turn (see the "Sessions by partial-miss waste" dashboard widget for which)',
      metric: sessionPartialMissUsdMetric,
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new cloudwatch.Alarm(this, 'PromptCacheWastedUsdAlarm', {
      alarmName: getResourceName(config, 'prompt-cache-wasted-usd'),
      alarmDescription:
        'Dollars wasted on prompt-cache re-writes of already-cached prefix bytes (avoidable + partial misses) exceeded threshold',
      metric: wastedUsdMetric,
      threshold: config.production ? 1 : 5,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ============================================================
    // Outputs
    // ============================================================

    new cdk.CfnOutput(this, 'PromptCacheDashboardName', {
      value: dashboard.dashboardName,
      description: 'CloudWatch Dashboard for prompt-cache observability',
      exportName: `${config.projectPrefix}-PromptCacheDashboard`,
    });
  }
}
