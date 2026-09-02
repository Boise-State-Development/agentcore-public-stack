import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

/**
 * Everything `cloudwatch.Alarm` accepts, minus the parts this factory decides.
 *
 * `alarmName` is replaced by `name`, which is passed through
 * getResourceName() so every alarm in the stack is prefixed identically.
 */
export interface RoutedAlarmProps extends Omit<cloudwatch.AlarmProps, 'alarmName'> {
  /** Unprefixed alarm name, e.g. 'alb-target-5xx'. getResourceName() applies
   *  the project prefix. */
  name: string;
}

/**
 * AlarmFactory — creates CloudWatch alarms that are wired to the SNS topic as a
 * consequence of being created at all.
 *
 * ## Why a factory instead of a convention
 *
 * Before this existed, the stack had 13 alarms and none of them notified
 * anyone. Three separate constructs carried a comment saying so. Nobody had
 * done anything wrong: `new cloudwatch.Alarm(...)` is the obvious way to make
 * an alarm, and it produces a console-only alarm that looks completely
 * finished. The failure was structural, not careless.
 *
 * So the fix is structural. Routing is not documented here as a rule to
 * remember — it is the default behaviour of the easiest available tool. An
 * unrouted alarm now requires deliberately bypassing this factory, and the
 * "every alarm has an action" test in observability-alarm-routing.test.ts will
 * fail if anyone does.
 *
 * ## treatMissingData is left to the caller
 *
 * Deliberately not defaulted. The correct value is a property of what the
 * metric means, and both answers are right somewhere in this stack:
 * `NOT_BREACHING` for error counts that are simply absent when nothing is
 * failing, but `BREACHING` for `UnHealthyHostCount`, where "no data" means no
 * host is reporting at all — the incident itself. A factory default would
 * silently make one of those wrong.
 */
export class AlarmFactory {
  constructor(
    private readonly scope: Construct,
    private readonly config: AppConfig,
    /** Undefined when observability.alarmTopicEnabled is false, in which case
     *  alarms are still created but stay console-only. */
    private readonly topic?: sns.ITopic,
  ) {}

  /**
   * Create an alarm and attach the SNS action.
   *
   * @param id    CDK logical id. Keep stable across refactors — changing it
   *              replaces the alarm rather than updating it.
   * @param props Alarm properties, with `name` in place of `alarmName`.
   */
  public alarm(id: string, props: RoutedAlarmProps): cloudwatch.Alarm {
    const { name, ...rest } = props;

    const alarm = new cloudwatch.Alarm(this.scope, id, {
      ...rest,
      alarmName: getResourceName(this.config, name),
    });

    if (this.topic) {
      const action = new cloudwatchActions.SnsAction(this.topic);
      alarm.addAlarmAction(action);
      // Also notify on recovery. Without this an operator who received a page
      // has no signal that the condition cleared, and the usual workaround is
      // to go and check the console — which is the behaviour this whole effort
      // exists to remove.
      alarm.addOkAction(action);
    }

    return alarm;
  }

  /**
   * Create an alarm from a metric-math expression.
   *
   * Same routing guarantee; exists so callers doing arithmetic across metrics
   * (error *rates*, aggregate throttles) do not have to reach past the factory
   * and lose the SNS action.
   */
  public expressionAlarm(
    id: string,
    props: Omit<RoutedAlarmProps, 'metric'> & { expression: cloudwatch.IMetric },
  ): cloudwatch.Alarm {
    const { expression, ...rest } = props;
    return this.alarm(id, { ...rest, metric: expression });
  }
}

/**
 * Standard evaluation period for count-based alarms across this stack.
 *
 * Five minutes rather than one: the ALB, DynamoDB, and Lambda thresholds in
 * ObservabilityConfig are all expressed "per 5-minute period", and a shared
 * constant keeps a threshold's meaning attached to the window it was chosen
 * for. A one-minute period with a threshold picked for five minutes is five
 * times more sensitive than intended, which reads as flapping.
 */
export const ALARM_PERIOD = cdk.Duration.minutes(5);

/**
 * Every `cloudwatch.Alarm` anywhere beneath `scope`, in stable creation order.
 *
 * Used by the platform dashboard's alarm-status widget. Discovered by walking
 * the construct tree rather than passed in as a hand-maintained list, because a
 * list is the kind of thing that goes stale silently: an alarm added next year
 * would still be routed to SNS (the factory guarantees that) but would quietly
 * go missing from the one dashboard an on-call engineer actually opens.
 *
 * Call this AFTER all alarms are constructed — in this stack that means late in
 * `wireCompute()`.
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
