import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory, ALARM_PERIOD, chunk, MAX_METRICS_PER_MATH_ALARM } from './alarm-factory';

export interface AlarmedTable {
  /** Unprefixed table name, used in the alarm description so a notification
   *  names the tables it covers. */
  name: string;
  table: dynamodb.ITable;
}

export interface DynamoDbAlarmsConstructProps {
  config: AppConfig;
  tables: AlarmedTable[];
  alarmTopic?: sns.ITopic;
}

/** Each table contributes two metrics: read and write throttle events. */
const METRICS_PER_TABLE = 2;

/** Tables per grouped throttle alarm, under CloudWatch's 10-metric cap. */
export const TABLES_PER_THROTTLE_ALARM = MAX_METRICS_PER_MATH_ALARM / METRICS_PER_TABLE;

/**
 * Throttle alarms over groups of tables, plus one account-level request-error
 * alarm.
 *
 * One alarm covers up to five tables instead of one alarm per table, because
 * PlatformStack sits against CloudFormation's 500-resource limit and these
 * alarms were its largest discretionary consumer (29 of them). The expression
 * is `MAX` over each table's `reads + writes`, so the threshold still applies to
 * every table on its own, not to the group's total.
 *
 * What grouping costs: the notification names the group, not the table (the
 * description lists the members, and each table's metrics show which one), and
 * while one table holds the alarm in ALARM, a second table in the same group
 * starting to throttle sends no new notification. Both are acceptable for these
 * alarms in particular: all tables are on-demand, and a live check found zero
 * metric streams for ReadThrottleEvents and WriteThrottleEvents, so none has
 * ever fired.
 *
 * Each series is wrapped in `FILL(…, 0)` before the sum. Metric math drops a
 * timestamp where either operand is missing, so an unfilled `reads + writes`
 * on a table that only ever throttled reads would never breach.
 *
 * SystemErrors is not alarmed per table: it had no streams either.
 * Account-level UserErrors is, because it had real data and nothing watching
 * it. UserErrors is published account-wide only, with no TableName dimension.
 */
export class DynamoDbAlarmsConstruct extends Construct {
  constructor(scope: Construct, id: string, props: DynamoDbAlarmsConstructProps) {
    super(scope, id);

    const { config, tables } = props;
    const alarms = new AlarmFactory(this, config, props.alarmTopic);
    const threshold = config.observability.dynamoThrottleThreshold;

    chunk(tables, TABLES_PER_THROTTLE_ALARM).forEach((group, index) => {
      const usingMetrics: Record<string, cloudwatch.IMetric> = {};
      const perTable: string[] = [];

      group.forEach(({ table }, i) => {
        // table.metric(), not metricThrottledRequests() — CDK deprecates the
        // latter as returning an invalid metric.
        usingMetrics[`r${i}`] = table.metric('ReadThrottleEvents', {
          period: ALARM_PERIOD,
          statistic: 'Sum',
        });
        usingMetrics[`w${i}`] = table.metric('WriteThrottleEvents', {
          period: ALARM_PERIOD,
          statistic: 'Sum',
        });
        perTable.push(`FILL(r${i}, 0) + FILL(w${i}, 0)`);
      });

      const members = group.map(({ name }) => name).join(', ');
      const groupNumber = index + 1;

      alarms.expressionAlarm(`ThrottleGroup${groupNumber}Alarm`, {
        name: `ddb-throttle-group-${groupNumber}`,
        alarmDescription:
          `DynamoDB throttling on at least one of: ${members}. Find the table by `
          + `comparing ReadThrottleEvents and WriteThrottleEvents across those tables: `
          + `reads point at a query pattern or a hot partition being read, writes at a `
          + `hot key or a write burst. On-demand tables, so this is a partition hot spot `
          + `or an account limit, not under-provisioning.`,
        expression: new cloudwatch.MathExpression({
          expression: `MAX([${perTable.join(', ')}])`,
          usingMetrics,
          period: ALARM_PERIOD,
        }),
        threshold,
        evaluationPeriods: 2,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    });

    // DynamoDB 4xx across all tables: validation failures, missing keys,
    // malformed requests — application code misusing the API.
    alarms.alarm('DynamoDbUserErrorAlarm', {
      name: 'ddb-user-errors',
      alarmDescription:
        'DynamoDB rejected requests account-wide (4xx: validation, missing key, '
        + 'malformed request). Application code, not an AWS fault. No dimensions are '
        + 'available, so use CloudTrail or application logs to find the caller.',
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
