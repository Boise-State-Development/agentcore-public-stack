import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory, ALARM_PERIOD } from './alarm-factory';

export interface EcsServiceAlarmsConstructProps {
  config: AppConfig;
  /** The app-api Fargate service. Alarm dimensions are derived from it. */
  service: ecs.FargateService;
  /** Desired task count, for the "fewer tasks running than asked for" alarm. */
  desiredCount: number;
  /** Platform alarm topic. Undefined leaves these alarms console-only. */
  alarmTopic?: sns.ITopic;
}

/**
 * EcsServiceAlarmsConstruct — saturation and capacity alarms for the app-api
 * Fargate service.
 *
 * ## Dimensions are the whole game here
 *
 * `AWS/ECS` metrics are published at several dimension granularities, and a
 * `CPUUtilization` alarm with NO dimensions is a valid CloudWatch alarm that
 * silently watches the average across every ECS service in the account. It
 * deploys, it evaluates, it never fires for the thing you meant. This construct
 * uses the CDK service's own `metricCpuUtilization()` helpers precisely so the
 * ClusterName + ServiceName dimensions come from the service resource and
 * cannot be forgotten — and the test asserts both are present.
 *
 * ## Why running-task count matters more than CPU here
 *
 * CPU and memory tell you the service is under strain. `RunningTaskCount`
 * below desired tells you capacity has actually been lost — a task that keeps
 * crashing on startup, an image that will not pull, a subnet that ran out of
 * IPs. The ALB's UnHealthyHostCount alarm catches the case where tasks are
 * running but failing health checks; this catches the case where they are not
 * running at all.
 */
export class EcsServiceAlarmsConstruct extends Construct {
  constructor(scope: Construct, id: string, props: EcsServiceAlarmsConstructProps) {
    super(scope, id);

    const { config, service, desiredCount } = props;
    const alarms = new AlarmFactory(this, config, props.alarmTopic);
    const obs = config.observability;

    // ============================================================
    // Saturation
    // ============================================================

    alarms.alarm('AppApiCpuAlarm', {
      name: 'app-api-cpu-high',
      alarmDescription:
        'App API service CPU utilisation is sustained above the configured percentage',
      metric: service.metricCpuUtilization({
        period: ALARM_PERIOD,
        statistic: 'Average',
      }),
      threshold: obs.ecsCpuPercent,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Memory gets a higher default threshold than CPU (85 vs 80): a Fargate
    // task that exhausts memory is killed outright, whereas high CPU merely
    // slows down, so the memory signal needs less headroom to be actionable but
    // more headroom to avoid firing on normal steady-state usage.
    alarms.alarm('AppApiMemoryAlarm', {
      name: 'app-api-memory-high',
      alarmDescription:
        'App API service memory utilisation is sustained above the configured percentage — a Fargate task that exhausts memory is killed, not throttled',
      metric: service.metricMemoryUtilization({
        period: ALARM_PERIOD,
        statistic: 'Average',
      }),
      threshold: obs.ecsMemoryPercent,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ============================================================
    // Capacity
    // ============================================================

    /**
     * Fewer tasks running than desired.
     *
     * BREACHING on missing data, for the same reason as the ALB's unhealthy-host
     * alarm: if the service is deleted or has zero tasks, the metric stops
     * being published rather than reporting zero. NOT_BREACHING would render
     * this alarm silent in exactly the total-outage case it exists to catch.
     *
     * Comparison is LESS_THAN against desiredCount, so a service scaled up by
     * autoscaling does not trip it — only one that has fallen below the floor
     * it was asked to hold.
     */
    alarms.alarm('AppApiRunningTaskAlarm', {
      name: 'app-api-running-tasks-low',
      alarmDescription:
        `Fewer than the desired ${desiredCount} App API task(s) are running — tasks are failing to start or being killed, which the ALB health-check alarm would not catch`,
      metric: new cloudwatch.Metric({
        // Container Insights metric, already enabled on the cluster
        // (containerInsightsV2). Not available from the service.metric* helpers,
        // so the dimensions are supplied explicitly from the service resource —
        // never hardcoded.
        namespace: 'ECS/ContainerInsights',
        metricName: 'RunningTaskCount',
        dimensionsMap: {
          ClusterName: service.cluster.clusterName,
          ServiceName: service.serviceName,
        },
        period: ALARM_PERIOD,
        statistic: 'Minimum',
      }),
      threshold: desiredCount,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
  }
}
