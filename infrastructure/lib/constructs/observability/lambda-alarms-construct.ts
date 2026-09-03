import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory, ALARM_PERIOD } from './alarm-factory';

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
 * No duration alarms: a function that exceeds its timeout is killed and records
 * an Errors datapoint, so the failure that matters is already covered.
 *
 * token-enrichment is worth alarming precisely because its handler is fail-open —
 * an error means MCP tools silently lose user-identity claims rather than a
 * visible login failure.
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

    for (const { name, fn, errorThreshold, throttleOnly } of functions) {
      const id = toId(name);

      if (!throttleOnly) alarms.alarm(`${id}ErrorAlarm`, {
        name: `lambda-${name}-errors`,
        alarmDescription:
          `${name} Lambda is returning errors. Includes invocations killed for `
          + `exceeding their timeout.`,
        metric: fn.metricErrors({ period: ALARM_PERIOD, statistic: 'Sum' }),
        threshold: errorThreshold ?? defaultErrorThreshold,
        evaluationPeriods: 2,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        // A function that is not invoked publishes nothing, and most of these
        // are event-driven and idle for long stretches.
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });

      alarms.alarm(`${id}ThrottleAlarm`, {
        name: `lambda-${name}-throttles`,
        alarmDescription:
          `${name} Lambda invocations are being throttled — concurrency exhausted. `
          + `A reserved-concurrency or account-limit problem, not a code problem.`,
        metric: fn.metricThrottles({ period: ALARM_PERIOD, statistic: 'Sum' }),
        threshold: 0,
        evaluationPeriods: 2,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }

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
