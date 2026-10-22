import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

/** Alarm props, with `name` (unprefixed) in place of `alarmName`. */
export interface RoutedAlarmProps extends Omit<cloudwatch.AlarmProps, 'alarmName'> {
  name: string;
}

/**
 * Creates alarms wired to the SNS topic.
 *
 * Use this instead of `new cloudwatch.Alarm()` — an alarm without actions looks
 * finished but notifies nobody, so routing is made a property of the tool rather
 * than something to remember. Enforced by observability-alarm-routing.test.ts.
 *
 * `treatMissingData` is deliberately not defaulted; see
 * .kiro/steering/observability.md.
 */
export class AlarmFactory {
  constructor(
    private readonly scope: Construct,
    private readonly config: AppConfig,
    /** Undefined when alarmTopicEnabled is false; alarms stay console-only. */
    private readonly topic?: sns.ITopic,
  ) {}

  public alarm(id: string, props: RoutedAlarmProps): cloudwatch.Alarm {
    const { name, ...rest } = props;

    const alarm = new cloudwatch.Alarm(this.scope, id, {
      ...rest,
      alarmName: getResourceName(this.config, name),
    });

    if (this.topic) {
      const action = new cloudwatchActions.SnsAction(this.topic);
      alarm.addAlarmAction(action);
      // Also notify on recovery, so nobody has to check the console to find out
      // whether the condition cleared.
      alarm.addOkAction(action);
    }

    return alarm;
  }

  /** Same routing guarantee, for metric-math alarms. */
  public expressionAlarm(
    id: string,
    props: Omit<RoutedAlarmProps, 'metric'> & { expression: cloudwatch.IMetric },
  ): cloudwatch.Alarm {
    const { expression, ...rest } = props;
    return this.alarm(id, { ...rest, metric: expression });
  }
}

/** Shared period for count-based alarms; thresholds are chosen against it. */
export const ALARM_PERIOD = cdk.Duration.minutes(5);

/**
 * Every alarm beneath `scope`, for the dashboard's alarm-status widget.
 *
 * Discovered by walking the tree rather than passed in, so an alarm added later
 * cannot go missing from the dashboard. Call after all alarms are constructed.
 */
export function collectAlarms(scope: Construct): cloudwatch.Alarm[] {
  const found: cloudwatch.Alarm[] = [];
  const visit = (node: Construct) => {
    for (const child of node.node.children) {
      if (child instanceof cloudwatch.Alarm) found.push(child);
      visit(child as Construct);
    }
  };
  visit(scope);
  return found;
}
