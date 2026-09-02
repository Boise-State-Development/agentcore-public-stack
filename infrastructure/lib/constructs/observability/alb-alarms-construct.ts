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
 * AlbAlarmsConstruct — golden-signal alarms for the platform's front door.
 *
 * ## The streaming problem, and why latency is not the headline signal here
 *
 * The chat path is Server-Sent Events. The ALB does not consider a request
 * complete until the stream closes, so `TargetResponseTime` for a healthy agent
 * turn is legitimately tens of seconds — and a *fast* response can mean the
 * agent failed early. Latency on this load balancer is therefore a weak health
 * signal in both directions, and a tight threshold on it produces noise that
 * gets the alarm muted, at which point it is worth less than nothing.
 *
 * So the reliable signals here are the discrete ones: 5xx counts, unhealthy
 * hosts, rejected connections, and connection errors. A p99 latency alarm is
 * included, but with a deliberately high floor
 * (`observability.albP99LatencyMs`, default 120s) chosen to sit above a normal
 * long turn and below a hung one.
 *
 * ## ELB 5xx vs Target 5xx are different incidents
 *
 * `HTTPCode_ELB_5XX_Count` is the load balancer failing — no healthy target,
 * or a request it could not hand off. `HTTPCode_Target_5XX_Count` is the
 * application returning an error while perfectly reachable. They are alarmed
 * separately because the first response is "check whether anything is running"
 * and the second is "read the application logs".
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

    // The load balancer itself failing to serve. Most commonly: no healthy
    // target to route to. NOT_BREACHING on missing data because a period with
    // no traffic emits nothing, and silence here is genuinely fine.
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

    // The application erroring while reachable. Bound to the target group, so
    // this counts only app-api's responses.
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

    /**
     * BREACHING on missing data, and this is the one alarm in the stack where
     * that is essential.
     *
     * `UnHealthyHostCount` is only reported while targets are registered. If
     * the service scales to zero, the task definition fails to launch, or the
     * whole service is deleted, the metric stops arriving entirely — and with
     * NOT_BREACHING (the sensible default everywhere else) the alarm would sit
     * quietly in INSUFFICIENT_DATA reporting nothing wrong while the platform
     * is completely down. Absence of data IS the outage here.
     */
    alarms.alarm('AlbUnhealthyHostAlarm', {
      name: 'alb-unhealthy-hosts',
      alarmDescription:
        'One or more App API targets are failing their health check, or no targets are reporting at all',
      metric: targetGroup.metrics.unhealthyHostCount({
        period: ALARM_PERIOD,
        statistic: 'Maximum',
      }),
      threshold: 0,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });

    // The ALB could not open a connection to a target at all — a security-group
    // or network-path fault rather than an application error, so it is worth
    // separating from the 5xx alarms above.
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

    // Non-zero means the ALB hit its connection limit and turned users away at
    // the door. Threshold 0: any rejection at all is worth knowing about,
    // because it is invisible from inside the application — the request never
    // arrives, so nothing is logged.
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
      // CloudWatch reports TargetResponseTime in SECONDS; the config value is
      // in milliseconds so it reads consistently with the AgentCore latency
      // knob. Converting here rather than storing seconds keeps one unit in
      // config and avoids a 1000x threshold error at the call site.
      threshold: obs.albP99LatencyMs / 1000,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }
}
