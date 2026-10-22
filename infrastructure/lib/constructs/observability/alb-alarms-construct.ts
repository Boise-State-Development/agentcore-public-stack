import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory, ALARM_PERIOD } from './alarm-factory';

export interface AlbAlarmsConstructProps {
  config: AppConfig;
  /** The application load balancer. Alarm dimensions are taken from it rather
   *  than constructed by hand. */
  loadBalancer: elbv2.IApplicationLoadBalancer;
  /** The app-api target group. */
  targetGroup: elbv2.IApplicationTargetGroup;
  /** Platform alarm topic. Undefined leaves these alarms console-only. */
  alarmTopic?: sns.ITopic;
}

/**
 * Front-door alarms.
 *
 * The chat path is SSE, so TargetResponseTime is legitimately tens of seconds and
 * latency is a weak signal in both directions — the discrete metrics (5xx counts,
 * unhealthy hosts, rejected connections) are the reliable ones.
 *
 * ELB 5xx and target 5xx are separate alarms because the first response differs:
 * "is anything running" versus "read the application logs".
 */
export class AlbAlarmsConstruct extends Construct {
  constructor(scope: Construct, id: string, props: AlbAlarmsConstructProps) {
    super(scope, id);

    const { config, loadBalancer, targetGroup } = props;
    const alarms = new AlarmFactory(this, config, props.alarmTopic);
    const obs = config.observability;

    // ============================================================
    // Errors
    // ============================================================

    alarms.alarm('AlbElb5xxAlarm', {
      name: 'alb-elb-5xx',
      alarmDescription:
        'ALB returned 5xx responses of its own (not the application) — usually no healthy target to route to',
      metric: loadBalancer.metrics.httpCodeElb(
        elbv2.HttpCodeElb.ELB_5XX_COUNT,
        { period: ALARM_PERIOD, statistic: 'Sum' },
      ),
      threshold: obs.albTarget5xxThreshold,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    alarms.alarm('AlbTarget5xxAlarm', {
      name: 'alb-target-5xx',
      alarmDescription:
        'App API returned 5xx responses through the ALB — the service is reachable but failing',
      metric: targetGroup.metrics.httpCodeTarget(
        elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
        { period: ALARM_PERIOD, statistic: 'Sum' },
      ),
      threshold: obs.albTarget5xxThreshold,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ============================================================
    // Availability
    // ============================================================

    // BREACHING: UnHealthyHostCount stops being published entirely when no
    // targets are registered, so absent data is the outage, not health.
    //
    // Sustained window, not a raised threshold. A single App API target that
    // fails one health-check probe is replaced by ECS within ~5 min while the
    // other targets keep serving — a benign self-healing blip that was paging
    // ~daily at evaluationPeriods:2 (10 min). Requiring the condition to hold
    // for a full 20 min (4 consecutive 5-min periods) filters those transient
    // replacements while still firing on the two states that matter: a target
    // stuck unhealthy, and a total outage (metric goes missing -> BREACHING).
    // This mirrors the window-not-threshold approach documented for the
    // runtime-active-sessions alarm in .kiro/steering/observability.md §11 —
    // raising the threshold instead would blind us to a single stuck target.
    alarms.alarm('AlbUnhealthyHostAlarm', {
      name: 'alb-unhealthy-hosts',
      alarmDescription:
        'One or more App API targets have been failing their health check for a '
        + 'sustained period (20 min), or no targets are reporting at all. A brief '
        + 'single-target blip that ECS self-heals does not trip this.',
      metric: targetGroup.metrics.unhealthyHostCount({
        period: ALARM_PERIOD,
        statistic: 'Maximum',
      }),
      threshold: 0,
      evaluationPeriods: 4,
      datapointsToAlarm: 4,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });

    alarms.alarm('AlbTargetConnectionErrorAlarm', {
      name: 'alb-target-connection-errors',
      alarmDescription:
        'ALB could not establish connections to App API targets — network path or security group fault',
      metric: loadBalancer.metrics.targetConnectionErrorCount({
        period: ALARM_PERIOD,
        statistic: 'Sum',
      }),
      threshold: obs.albTarget5xxThreshold,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ============================================================
    // Saturation
    // ============================================================

    // Threshold 0: a rejected connection never reaches the app, so it appears
    // in no application log.
    alarms.alarm('AlbRejectedConnectionAlarm', {
      name: 'alb-rejected-connections',
      alarmDescription:
        'ALB rejected connections after reaching its limit — users were turned away before reaching the application, so nothing appears in application logs',
      metric: loadBalancer.metrics.rejectedConnectionCount({
        period: ALARM_PERIOD,
        statistic: 'Sum',
      }),
      threshold: 0,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ============================================================
    // Latency (streaming-aware — see the class docstring)
    // ============================================================

    alarms.alarm('AlbTargetLatencyAlarm', {
      name: 'alb-target-p99-latency',
      alarmDescription:
        'App API p99 response time through the ALB exceeded the configured floor. '
        + 'NOTE: the chat path is SSE, so a healthy agent turn legitimately takes '
        + 'tens of seconds — this floor is set high on purpose and a breach means '
        + 'requests are hanging, not merely slow.',
      metric: targetGroup.metrics.targetResponseTime({
        period: ALARM_PERIOD,
        statistic: 'p99',
      }),
      // CloudWatch reports this metric in SECONDS; config is in ms.
      threshold: obs.albP99LatencyMs / 1000,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }
}
