import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface PromptCacheObservabilityConstructProps {
  config: AppConfig;
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

    const { config } = props;

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

    const wastedUsdMetric = new cloudwatch.Metric({
      namespace,
      metricName: 'WastedUsd',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
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
        title: 'Avoidable Cache Misses',
        left: [avoidableMissMetric],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Wasted USD (avoidable re-writes)',
        left: [wastedUsdMetric],
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.LogQueryWidget({
        title: 'Model Calls by cacheStatus (inference-api)',
        // The runtime log group is defined by the inference-api
        // construct with this deterministic name; referenced by name
        // (not typed ref) since a dashboard widget creates no CFN
        // dependency. app-api's ECS log group is auto-named, so its
        // (much smaller) share of emissions isn't queried here.
        logGroupNames: [`/aws/bedrock-agentcore/runtimes/${config.projectPrefix}`],
        queryLines: [
          'filter ispresent(cacheStatus)',
          'stats count(*) as calls by cacheStatus',
          'sort calls desc',
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

    new cloudwatch.Alarm(this, 'PromptCacheWastedUsdAlarm', {
      alarmName: getResourceName(config, 'prompt-cache-wasted-usd'),
      alarmDescription:
        'Dollars wasted on avoidable prompt-cache re-writes exceeded threshold',
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
