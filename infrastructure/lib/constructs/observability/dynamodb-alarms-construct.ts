import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory, ALARM_PERIOD } from './alarm-factory';

/** One table to alarm on, with the short name used in the alarm name. */
export interface AlarmedTable {
  /** Unprefixed table name, e.g. 'sessions-metadata'. Used to build the alarm
   *  name so an operator reading a notification knows the table immediately. */
  name: string;
  table: dynamodb.ITable;
}

export interface DynamoDbAlarmsConstructProps {
  config: AppConfig;
  /** Every table to cover. Built from typed construct refs, never from name
   *  strings — see the class docstring. */
  tables: AlarmedTable[];
  /** Platform alarm topic. Undefined leaves these alarms console-only. */
  alarmTopic?: sns.ITopic;
}

/**
 * DynamoDbAlarmsConstruct — throttle alarms per table, plus one account-level
 * request-error alarm.
 *
 * ## Resource budget shaped this design, and measurement decided it
 *
 * CloudFormation caps a stack at 500 resources. This is a deliberate
 * single-stack architecture, so that ceiling is shared by every feature the
 * platform will ever add — there is no second stack to spill into. The data
 * layer is 26 tables, so the difference between one alarm per table and three
 * is the difference between 26 and 78 resources, or roughly 10% of the entire
 * stack budget.
 *
 * The allocation was therefore decided by checking what the metrics actually
 * do in a live account rather than by covering every documented metric:
 *
 *   - `ReadThrottleEvents` / `WriteThrottleEvents`: **zero streams**. Every
 *     table is on-demand billing, so capacity scales automatically and
 *     throttling has never once occurred. Still worth alarming — when it does
 *     happen it means a hot partition or an account limit, both urgent — but it
 *     does not warrant two alarms per table.
 *   - `SystemErrors`: **zero streams**. AWS-side 5xx, and when DynamoDB does
 *     have a bad day it affects more than one table, so per-table attribution
 *     buys almost nothing. Dropped in favour of the account-level signal below.
 *   - `UserErrors`: **live data** — 3 errors one day and 7 another in the
 *     trailing fortnight. These are 4xx: malformed requests, validation
 *     failures, missing keys. That is our own code misusing DynamoDB, it is
 *     happening right now, and nothing was watching it.
 *
 * So the swap is 26 alarms on a signal that has never fired for 1 alarm on a
 * signal that is firing today.
 *
 * `UserErrors` is published account-wide with NO dimensions (verified: the only
 * dimension set is the empty one), so a per-table version is not available even
 * if the budget allowed it.
 *
 * ## Read and write throttles share one alarm, deliberately
 *
 * They have different causes — read throttling points at query patterns or a hot
 * partition being read, write throttling at a hot key or a write burst. Ideally
 * they would be separate. At 26 resources for the split, against a signal with
 * no recorded occurrences, they are combined into a metric-math sum and the
 * alarm description names both metrics so the first diagnostic step is written
 * down rather than remembered. The alarm still names the table, which is the
 * part that cannot be recovered from a dashboard afterwards.
 *
 * ## Tables arrive as typed refs
 *
 * `AlarmedTable.table` is an `ITable`, so the `TableName` dimension is rendered
 * by CDK from the real resource. Building the dimension from a name string would
 * produce an alarm that looks correct and silently watches a table that may not
 * exist — the same class of bug as the mis-named log group this repo already
 * found, where a guessed group held 0 bytes and every widget read as
 * "no traffic".
 */
export class DynamoDbAlarmsConstruct extends Construct {
  constructor(scope: Construct, id: string, props: DynamoDbAlarmsConstructProps) {
    super(scope, id);

    const { config, tables } = props;
    const alarms = new AlarmFactory(this, config, props.alarmTopic);
    const threshold = config.observability.dynamoThrottleThreshold;

    for (const { name, table } of tables) {
      // A CDK logical-id fragment derived from the table's short name.
      const id = name
        .split('-')
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join('');

      // Combined read + write throttling for this table.
      //
      // Two metrics in the expression, comfortably inside CloudWatch's limit of
      // 10 individual metrics per math-expression alarm. (That limit is not
      // theoretical: `metricSystemErrorsForOperations()` defaults to all 14
      // DynamoDB operations and throws `TooManyMetricsInMathExpression` at
      // synth.)
      //
      // `table.metric(...)` rather than the `metricThrottledRequests()` helper,
      // which CDK deprecates as returning an invalid metric.
      const readThrottles = table.metric('ReadThrottleEvents', {
        period: ALARM_PERIOD,
        statistic: 'Sum',
      });
      const writeThrottles = table.metric('WriteThrottleEvents', {
        period: ALARM_PERIOD,
        statistic: 'Sum',
      });

      alarms.expressionAlarm(`${id}ThrottleAlarm`, {
        name: `ddb-${name}-throttle`,
        alarmDescription:
          `DynamoDB throttling on ${name}. Check ReadThrottleEvents vs `
          + `WriteThrottleEvents on this table to tell the two apart: reads point at `
          + `a query pattern or a hot partition being read, writes at a hot key or a `
          + `write burst. This table is on-demand, so throttling means a partition-level `
          + `hot spot or an account limit, not under-provisioning.`,
        expression: new cloudwatch.MathExpression({
          expression: 'reads + writes',
          usingMetrics: { reads: readThrottles, writes: writeThrottles },
          period: ALARM_PERIOD,
        }),
        threshold,
        evaluationPeriods: 2,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        // DynamoDB publishes nothing for a table that is not throttling, so
        // absent data is the healthy state.
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }

    // ============================================================
    // Account-level request errors
    // ============================================================

    // DynamoDB 4xx across every table: validation failures, missing keys,
    // malformed requests. Our own code misusing the API, and the one DynamoDB
    // signal in this account with live data.
    //
    // No dimensions, because none are available — `UserErrors` is published
    // account-wide only. That makes it a single cheap alarm rather than 26, and
    // it is why this replaced the per-table SystemErrors alarms rather than
    // being added alongside them.
    //
    // Threshold is the same knob as the throttle alarms: both answer "is the
    // data layer rejecting our requests", and a fork tuning one almost always
    // means to tune the other.
    alarms.alarm('DynamoDbUserErrorAlarm', {
      name: 'ddb-user-errors',
      alarmDescription:
        'DynamoDB rejected requests across the account (4xx: validation, missing key, '
        + 'malformed request). This is application code misusing DynamoDB, not an AWS '
        + 'fault. Published account-wide with no dimensions, so use CloudTrail or the '
        + 'application logs to find the caller.',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/DynamoDB',
        metricName: 'UserErrors',
        statistic: 'Sum',
        period: ALARM_PERIOD,
      }),
      threshold,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }
}
