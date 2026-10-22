import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory, ALARM_PERIOD, chunk, MAX_METRICS_PER_MATH_ALARM } from './alarm-factory';

/** One Lambda to alarm on, with the short name used in the alarm name. */
export interface AlarmedFunction {
  /** Unprefixed short name, e.g. 'artifact-render'. */
  name: string;
  fn: lambda.IFunction;
  /** Error threshold override. Defaults to `observability.lambdaErrorThreshold`. */
  errorThreshold?: number;
  /** Skip the error alarm: this function's own construct defines one with a
   *  tuned threshold (kb-sync and scheduled-runs use 1 for dispatchers, 3 for
   *  workers). */
  throttleOnly?: boolean;
}

/** A dead-letter queue whose depth should be alarmed. */
export interface AlarmedDlq {
  name: string;
  queue: sqs.IQueue;
}

export interface LambdaAlarmsConstructProps {
  config: AppConfig;
  functions: AlarmedFunction[];
  /** Dead-letter queues to watch. Any message here is work that was lost. */
  dlqs?: AlarmedDlq[];
  /** Platform alarm topic. Undefined leaves these alarms console-only. */
  alarmTopic?: sns.ITopic;
}

/**
 * Error and throttle alarms for the stack's Lambdas, plus DLQ depth.
 *
 * Errors and throttles are each alarmed per group of up to ten functions, one
 * metric-math alarm per group, rather than once per function: PlatformStack
 * sits against CloudFormation's 500-resource limit, and per-function pairs
 * were 25 of its resources. The expression is `MAX` across the group's
 * functions, so a threshold still applies to each function on its own; error
 * groups are formed per threshold so an override is never averaged away.
 *
 * What grouping costs: the notification names the group (the description lists
 * its members, and each function's own metric shows which one), and while one
 * function holds the alarm in ALARM, a second one in the same group failing
 * sends no new notification.
 *
 * No duration alarms: a function that exceeds its timeout is killed and records
 * an Errors datapoint, so the failure that matters is already covered.
 *
 * token-enrichment is worth alarming precisely because its handler is fail-open —
 * an error means MCP tools silently lose user-identity claims rather than a
 * visible login failure. Its group's description says so.
 *
 * rag-cors-updater is excluded: a deploy-time custom resource whose failure fails
 * the CloudFormation deploy directly.
 */
export class LambdaAlarmsConstruct extends Construct {
  constructor(scope: Construct, id: string, props: LambdaAlarmsConstructProps) {
    super(scope, id);

    const { config, functions, dlqs = [] } = props;
    const alarms = new AlarmFactory(this, config, props.alarmTopic);
    const defaultErrorThreshold = config.observability.lambdaErrorThreshold;

    /** 'artifact-render' -> 'ArtifactRender' */
    const toId = (name: string) => name
      .split('-')
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join('');

    /** `MAX([m0, m1, ...])` over one metric per function in the group. */
    const maxAcross = (
      group: AlarmedFunction[],
      metricFor: (fn: lambda.IFunction) => cloudwatch.IMetric,
    ): cloudwatch.MathExpression => {
      const usingMetrics: Record<string, cloudwatch.IMetric> = {};
      group.forEach(({ fn }, i) => { usingMetrics[`m${i}`] = metricFor(fn); });
      return new cloudwatch.MathExpression({
        expression: `MAX([${Object.keys(usingMetrics).join(', ')}])`,
        usingMetrics,
        period: ALARM_PERIOD,
      });
    };

    const membersOf = (group: AlarmedFunction[]) => group.map(({ name }) => name).join(', ');

    const failOpenNote = (group: AlarmedFunction[]) => (
      group.some(({ name }) => name === 'token-enrichment')
        ? ' If it is token-enrichment, logins still work but MCP tools are silently '
          + 'losing user-identity claims: its handler is fail-open.'
        : ''
    );

    // Error groups are formed per threshold, so an override never shares an
    // alarm with functions at a different threshold.
    const byThreshold = new Map<number, AlarmedFunction[]>();
    for (const entry of functions) {
      if (entry.throttleOnly) continue;
      const threshold = entry.errorThreshold ?? defaultErrorThreshold;
      byThreshold.set(threshold, [...(byThreshold.get(threshold) ?? []), entry]);
    }

    let errorGroupNumber = 0;
    for (const [threshold, entries] of byThreshold) {
      for (const group of chunk(entries, MAX_METRICS_PER_MATH_ALARM)) {
        errorGroupNumber += 1;
        alarms.expressionAlarm(`ErrorGroup${errorGroupNumber}Alarm`, {
          name: `lambda-errors-group-${errorGroupNumber}`,
          alarmDescription:
            `At least one of these Lambdas is returning errors: ${membersOf(group)}. `
            + `Includes invocations killed for exceeding their timeout. Each function's `
            + `Errors metric shows which one.${failOpenNote(group)}`,
          expression: maxAcross(group, (fn) => fn.metricErrors({ period: ALARM_PERIOD, statistic: 'Sum' })),
          threshold,
          evaluationPeriods: 2,
          comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
          // A function that is not invoked publishes nothing, and most of these
          // are event-driven and idle for long stretches.
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        });
      }
    }

    chunk(functions, MAX_METRICS_PER_MATH_ALARM).forEach((group, index) => {
      const groupNumber = index + 1;
      alarms.expressionAlarm(`ThrottleGroup${groupNumber}Alarm`, {
        name: `lambda-throttles-group-${groupNumber}`,
        alarmDescription:
          `At least one of these Lambdas is being throttled (concurrency exhausted): `
          + `${membersOf(group)}. A reserved-concurrency or account-limit problem, not a `
          + `code problem. Each function's Throttles metric shows which one.`,
        expression: maxAcross(group, (fn) => fn.metricThrottles({ period: ALARM_PERIOD, statistic: 'Sum' })),
        threshold: 0,
        evaluationPeriods: 2,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    });

    // ============================================================
    // Dead-letter queues
    // ============================================================

    for (const { name, queue } of dlqs) {
      // Threshold 0 and one evaluation period: DLQ messages persist until
      // drained or replayed, so this must not self-clear.
      alarms.alarm(`${toId(name)}DlqDepthAlarm`, {
        name: `dlq-${name}-not-empty`,
        alarmDescription:
          `The ${name} dead-letter queue is not empty — work was accepted and then `
          + `failed every retry. These messages persist until drained or replayed, so `
          + `this alarm does not clear on its own.`,
        metric: queue.metricApproximateNumberOfMessagesVisible({
          period: ALARM_PERIOD,
          statistic: 'Maximum',
        }),
        threshold: 0,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }
  }
}
