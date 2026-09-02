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
  /**
   * Skip the error alarm for this function because its own construct already
   * defines one with a deliberately tuned threshold.
   *
   * kb-sync and scheduled-runs both do this: their dispatchers alarm at 1 error
   * (the dispatcher is the only initiator of scheduled work, so any failure
   * stalls the whole pipeline) while their workers tolerate 3 (one document or
   * one run failing is recoverable). Re-alarming them here at a single shared
   * threshold would either duplicate the notification or quietly contradict it.
   */
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
 * LambdaAlarmsConstruct — error and throttle alarms for every Lambda in the
 * stack, plus dead-letter-queue depth.
 *
 * ## Which functions were unmonitored before this
 *
 * Only kb-sync and scheduled-runs had error alarms. `artifact-render`,
 * `rag-ingestion`, the four kb-migration functions, and `token-enrichment` had
 * none.
 *
 * `token-enrichment` is the interesting one. It is a Cognito
 * pre-token-generation trigger, and the handler is deliberately **fail-open** —
 * on any error it returns the event unchanged, so a failure never blocks a
 * login. That is good design and it is exactly why the alarm matters: the
 * failure mode is not an outage anyone reports, it is MCP tools silently losing
 * the user-identity claims they were configured to receive, indefinitely, with
 * no symptom an operator would notice. Fail-open turns a loud failure into a
 * quiet one, so the alarm is the only thing that makes it visible.
 *
 * `rag-cors-updater` is deliberately EXCLUDED. It is a deploy-time custom
 * resource that updates S3 CORS once per deploy; if it fails, CloudFormation
 * fails the deploy and says so immediately. An alarm would add two resources to
 * report something already reported louder elsewhere.
 *
 * ## No duration alarms, deliberately
 *
 * The plan originally carried a third alarm per function comparing duration
 * against a percentage of its configured timeout. It was dropped to reclaim 12
 * of the stack's 500-resource CloudFormation budget, on the reasoning that a
 * function which actually exceeds its timeout is killed and records an
 * `Errors` datapoint — so the failure that matters is already covered, and the
 * duration alarm mostly reports "slower than usual", which is a dashboard
 * question rather than a page.
 *
 * ## Throttles are separate from errors
 *
 * A throttle is not the function failing; it is concurrency exhaustion, and the
 * fix is a reserved-concurrency or account-limit change rather than a code fix.
 * Threshold 0, because a throttled invocation is either lost or retried later
 * and neither is visible from inside the function.
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
          + `exceeding their timeout, which is why there is no separate duration alarm.`,
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
          `${name} Lambda invocations are being throttled — concurrency is `
          + `exhausted. This is a reserved-concurrency or account-limit problem, not a `
          + `code problem, and the throttled invocation is invisible from inside the `
          + `function.`,
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
      // Threshold 0: a message on a DLQ is work the platform accepted and then
      // failed to complete after every retry. There is no healthy number of
      // those, and unlike a Lambda error it does not resolve itself — the
      // message sits there until someone drains or replays it.
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
