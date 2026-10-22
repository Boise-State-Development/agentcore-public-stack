import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory, ALARM_PERIOD } from './alarm-factory';

export interface AlarmedTable {
  /** Unprefixed table name, used in the alarm name so a notification names the
   *  table. */
  name: string;
  table: dynamodb.ITable;
}

export interface DynamoDbAlarmsConstructProps {
  config: AppConfig;
  tables: AlarmedTable[];
  alarmTopic?: sns.ITopic;
}

/**
 * Throttle alarms per table, plus one account-level request-error alarm.
 *
 * Read and write throttles share one alarm rather than getting one each: all
 * tables are on-demand, and a live check found zero metric streams for
 * ReadThrottleEvents, WriteThrottleEvents and SystemErrors — none has ever
 * fired. The alarm description names both metrics so the read-vs-write
 * distinction is still recoverable.
 *
 * SystemErrors is not alarmed per table for the same reason; account-level
 * UserErrors is, because it had real data and nothing watching it. UserErrors is
 * published account-wide only, with no TableName dimension.
 */
export class DynamoDbAlarmsConstruct extends Construct {
  constructor(scope: Construct, id: string, props: DynamoDbAlarmsConstructProps) {
    super(scope, id);

    const { config, tables } = props;
    const alarms = new AlarmFactory(this, config, props.alarmTopic);
    const threshold = config.observability.dynamoThrottleThreshold;

    for (const { name, table } of tables) {
      const id = name
        .split('-')
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join('');

      // table.metric(), not metricThrottledRequests() — CDK deprecates the
      // latter as returning an invalid metric.
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
          `DynamoDB throttling on ${name}. Compare ReadThrottleEvents and `
          + `WriteThrottleEvents on this table: reads point at a query pattern or a hot `
          + `partition being read, writes at a hot key or a write burst. On-demand table, `
          + `so this is a partition hot spot or an account limit, not under-provisioning.`,
        expression: new cloudwatch.MathExpression({
          expression: 'reads + writes',
          usingMetrics: { reads: readThrottles, writes: writeThrottles },
          period: ALARM_PERIOD,
        }),
        threshold,
        evaluationPeriods: 2,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }

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
