import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface TurnLatencyObservabilityConstructProps {
  config: AppConfig;
  /**
   * The log group the AgentCore Runtime actually writes to, from
   * `InferenceAgentCoreConstruct.runtimeLogGroupName`.
   *
   * Must be passed, not derived — the group is service-created and named after
   * the runtime *id*. The prompt-cache construct once guessed it, and its Logs
   * Insights widgets silently returned nothing, which reads as "no traffic"
   * rather than "wrong query".
   */
  runtimeLogGroupName: string;
}

/**
 * TurnLatencyObservabilityConstruct — percentiles over the pre-stream stages
 * emitted by `backend/src/apis/inference_api/chat/turn_timing.py`.
 *
 * WHY THIS EXISTS
 * ---------------
 * `docs/specs/turn-latency-preamble.md` proposes making the preamble faster.
 * Nothing in the platform could previously tell whether such a change worked:
 * the only instrument was a `turn_prelude` log line read by grep, and the
 * recorded baseline is four turns. Against this path's real variance — a cold
 * container is 6.7s against a warm 3.75s, an agent-cache miss 1478ms against a
 * hit's 0ms — four samples cannot resolve a 100ms improvement.
 *
 * THE FOURTH DASHBOARD IS A DELIBERATE COST
 * -----------------------------------------
 * CloudWatch's free tier is three dashboards and charges $3/month beyond it;
 * `observability-platform-dashboard.test.ts` guards the count so the trade is
 * always conscious. It was taken here on purpose: this board is read while
 * shipping a latency change, side by side with the load-test output, and
 * folding it into the AgentCore Runtime dashboard (the first attempt) buried
 * the stage breakdown under runtime health widgets that answer an unrelated
 * question. $3/month against a 450-900ms wait on every turn is not a close
 * call. The AgentCore dashboard's AWS-reported `Latency` graph remains the
 * natural companion — it measures at the data plane, so the gap between it and
 * `PreludeTotalMs` is another read on the routing overhead no server-side stage
 * can see — and the platform dashboard links to both.
 *
 * WHAT IT DELIBERATELY DOES NOT DO
 * --------------------------------
 * These are percentile widgets, not sums. The metrics are dimension-less
 * (matching every other EMF caller here); `isResume` / `deferredBuild` /
 * `sessionId` ride as queryable log properties, so the Logs Insights widgets
 * below do the slicing instead of dimension fan-out. That is not only about
 * metric-stream cost: a dimension invites reading a p99 off a slice too thin to
 * have one.
 *
 * **No alarms.** An alarm needs a threshold, there is no baseline yet, and
 * inventing one is the exact guessing the spec this supports exists to prevent.
 * Add them once the dashboard has run long enough to say what normal is.
 *
 * **These metrics start at handler entry.** They cannot see the app-api hop,
 * auth, or Runtime routing — ~478ms warm, ~1.5s cold, up to 20% of the turn.
 * Only a client-side measurement covers that, which is what `tests/load` is
 * for. A dashboard that falls while the user's wait does not is a real
 * possibility, and the header widget says so on the dashboard itself rather
 * than only here.
 */
export class TurnLatencyObservabilityConstruct extends Construct {
  public readonly dashboard: cloudwatch.Dashboard;

  constructor(
    scope: Construct,
    id: string,
    props: TurnLatencyObservabilityConstructProps,
  ) {
    super(scope, id);

    const { config, runtimeLogGroupName } = props;

    // Must match TURN_LATENCY_EMF_NAMESPACE's default in turn_timing.py.
    // Deliberately not set from CDK, for the same reason the prompt-cache
    // namespace is not: dev and prod are separate AWS accounts, so an unscoped
    // namespace cannot collide. The default IS the contract, and a drift here
    // renders empty graphs — which look exactly like "no traffic".
    const namespace = 'AgentCoreStack/TurnLatency';

    const PERIOD = cdk.Duration.minutes(5);

    /** One stage at one statistic. Latency wants percentiles, never Sum. */
    const stage = (metricName: string, statistic: string, label?: string) =>
      new cloudwatch.Metric({
        namespace,
        metricName,
        statistic,
        period: PERIOD,
        label: label ?? `${metricName} ${statistic}`,
      });

    const percentiles = (metricName: string) => [
      stage(metricName, 'p50'),
      stage(metricName, 'p90'),
      stage(metricName, 'p99'),
    ];

    const preambleSubStages = (statistic: string) => [
      stage('PreambleOwnershipMs', statistic, 'ownership'),
      stage('PreambleSkillsMs', statistic, 'skills'),
      stage('PreambleFilesMs', statistic, 'files'),
      stage('PreambleSessionStateMs', statistic, 'session_state'),
      stage('PreambleQuotaMs', statistic, 'quota'),
    ];

    this.dashboard = new cloudwatch.Dashboard(this, 'TurnLatencyDashboard', {
      dashboardName: getResourceName(config, 'turn-latency-observability'),
      defaultInterval: cdk.Duration.hours(3),
    });

    this.dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: [
          '# Turn Latency — pre-stream stages',
          `**Project:** ${config.projectPrefix} | **Region:** ${config.awsRegion} | `
          + `**Namespace:** \`${namespace}\``,
          '',
          '_Every metric here starts at **inference-api handler entry**. None of them sees '
          + 'the app-api hop, auth, or AgentCore Runtime routing — ~478ms on a warm path and '
          + '~1.5s on a cold one. A stage falling here is necessary but **not sufficient** '
          + 'evidence that the user waits less; the client-side check is `tests/load` '
          + '(Locust TTFB + TTFT). See `docs/specs/turn-latency-preamble.md`._',
          '',
          '_`PreambleMs` is the sum of the five `Preamble*Ms` sub-stages, kept so the '
          + 'pre-split baseline stays comparable. AWS\'s own data-plane `Latency` for the '
          + `runtime is on the **${getResourceName(config, 'agentcore-observability')}** `
          + 'dashboard — the gap between it and `PreludeTotalMs` is the routing overhead._',
        ].join('\n'),
        width: 24,
        height: 5,
      }),
    );

    // Row 1 — the two numbers that decide whether a change worked.
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Prelude total (handler entry → agent ready)',
        left: percentiles('PreludeTotalMs'),
        leftYAxis: { min: 0 },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Preamble (all five sub-stages) — PR-2 moves this or it did not work',
        left: percentiles('PreambleMs'),
        leftYAxis: { min: 0 },
        width: 12,
        height: 6,
      }),
    );

    // Row 2 — which sub-stage owns the preamble. Split by statistic rather than
    // stacking fifteen lines on one axis, which is a picture nobody reads.
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Preamble breakdown (p50) — which sub-stage owns it',
        left: preambleSubStages('p50'),
        leftYAxis: { min: 0 },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Preamble breakdown (p90) — where the tail lives',
        left: preambleSubStages('p90'),
        leftYAxis: { min: 0 },
        width: 12,
        height: 6,
      }),
    );

    // Row 3 — the stages after the preamble. `AgentBuildMs` is bimodal by
    // construction (cache hit ≈ 0, miss ≈ 1.5-2.7s), so its p50 and p99
    // describe two different populations and the gap between them is the
    // agent-cache hit rate showing through.
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Agent build — the p50/p99 spread is the cache hit/miss split',
        left: percentiles('AgentBuildMs'),
        leftYAxis: { min: 0 },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'RAG + tool assembly (p90)',
        left: [stage('RagMs', 'p90', 'rag'), stage('ToolsMs', 'p90', 'tools')],
        leftYAxis: { min: 0 },
        width: 12,
        height: 6,
      }),
    );

    // Row 3b — inside the agent build. `agent_build` is the largest number
    // left in the prelude (~2950ms cold against 1-47ms warm) and nothing said
    // which part of it that was; these are the same decomposition move that
    // opened the preamble. `AgentBuildMs` above is their sum, so the pre-split
    // series stays comparable.
    //
    // p90 rather than p50: a warm build is ~0 across the board, so p50 would
    // be a row of flat lines. The cold builds — the ones worth fixing — live
    // in the upper percentiles by construction.
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Agent build breakdown (p90) — which part of a cold build is slow',
        left: [
          stage('AgentBuildPromptMs', 'p90', 'system prompt'),
          stage('AgentBuildRegistryMs', 'p90', 'tool registry'),
          stage('AgentBuildSessionMgrMs', 'p90', 'session manager (memory restore)'),
          stage('AgentBuildToolsMs', 'p90', 'tools (incl. MCP pre-flight)'),
        ],
        leftYAxis: { min: 0 },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Agent build breakdown, part 2 (p90)',
        left: [
          stage('AgentBuildHooksMs', 'p90', 'hooks'),
          stage('AgentBuildPluginsMs', 'p90', 'plugins'),
          stage('AgentBuildFinalizeMs', 'p90', 'finalize'),
          stage('AgentBuildRestMs', 'p90', 'remainder'),
        ],
        leftYAxis: { min: 0 },
        width: 12,
        height: 6,
      }),
    );

    // Row 4 — the slicing that dimensions would have done, done in Logs
    // Insights instead. A resume skips most of the preamble, so mixing the two
    // populations is what would make a traffic-mix shift look like a latency
    // win.
    this.dashboard.addWidgets(
      new cloudwatch.LogQueryWidget({
        title: 'Preamble by turn shape (resume turns skip most of it)',
        logGroupNames: [runtimeLogGroupName],
        queryLines: [
          'filter ispresent(PreambleMs)',
          'stats count(*) as turns, pct(PreambleMs, 50) as p50, '
            + 'pct(PreambleMs, 90) as p90 by isResume, deferredBuild',
          'sort turns desc',
        ],
        width: 12,
        height: 6,
      }),
      // The honesty check on the decomposition itself. If the stages do not
      // account for the total, time is being spent between them, in code the
      // spec has not modelled — and that gap is the next thing to chase.
      new cloudwatch.LogQueryWidget({
        title: 'Unaccounted prelude time (total − preamble − rag − tools − agent_build)',
        logGroupNames: [runtimeLogGroupName],
        queryLines: [
          'filter ispresent(PreludeTotalMs)',
          'fields PreludeTotalMs - (PreambleMs + RagMs + ToolsMs + AgentBuildMs) as unaccountedMs',
          'stats count(*) as turns, avg(unaccountedMs) as avgUnaccounted, '
            + 'pct(unaccountedMs, 90) as p90Unaccounted',
        ],
        width: 12,
        height: 6,
      }),
    );
  }
}
