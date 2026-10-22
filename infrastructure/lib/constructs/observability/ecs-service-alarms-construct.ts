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
 * Saturation and capacity alarms for the app-api service.
 *
 * Uses the service's own metric helpers so ClusterName + ServiceName come from
 * the resource — a dimension-less AWS/ECS alarm silently averages every service
 * in the account.
 *
 * RunningTaskCount complements the ALB's UnHealthyHostCount: that one catches
 * tasks running but failing health checks, this one catches tasks not running.
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

    // BREACHING: a service at zero tasks stops publishing rather than
    // publishing zero. LESS_THAN desiredCount, so autoscaling up never trips it.
    alarms.alarm('AppApiRunningTaskAlarm', {
      name: 'app-api-running-tasks-low',
      alarmDescription:
        `Fewer than the desired ${desiredCount} App API task(s) are running — tasks are failing to start or being killed, which the ALB health-check alarm would not catch`,
      metric: new cloudwatch.Metric({
        // Container Insights: no service.metric* helper, so dimensions come
        // explicitly from the service resource.
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
