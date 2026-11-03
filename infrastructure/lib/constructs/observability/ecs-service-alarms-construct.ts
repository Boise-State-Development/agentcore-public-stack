import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory, ALARM_PERIOD } from './alarm-factory';

/**
 * Where app-api's event-loop watchdog writes `EventLoopLagMs`
 * (`backend/src/apis/app_api/telemetry/event_loop_lag.py`). The same
 * namespace as app-api's other latency telemetry; the metric is
 * dimension-less like every EMF metric in this stack.
 */
export const EVENT_LOOP_LAG_NAMESPACE = 'AgentCoreStack/TurnLatency';
export const EVENT_LOOP_LAG_METRIC = 'EventLoopLagMs';
/** A loop late by this much held every request on the task, including /health. */
export const EVENT_LOOP_LAG_THRESHOLD_MS = 2000;

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
    // Responsiveness
    // ============================================================

    // app-api runs one uvicorn worker, so synchronous work on the event loop
    // stalls every request on that task at once — the ALB health probe
    // included, which is how a 3.5-minute S3 Vectors scan on 2026-10-30 got a
    // healthy task killed. The watchdog in app-api samples asyncio.sleep drift
    // once a second and reports the worst lag every 10 s, so Maximum over a
    // 1-minute period is the longest single stall in that minute. 2 of 3
    // periods: one stall is a lead to chase in the logs (the watchdog WARNs at
    // 1 s), two in three minutes is a task that is not serving.
    //
    // NOT_BREACHING: the metric is only absent when no app-api task is up,
    // which app-api-running-tasks-low already pages for.
    alarms.alarm('AppApiEventLoopLagAlarm', {
      name: 'app-api-event-loop-lag',
      alarmDescription:
        `An App API task's event loop was blocked for more than ${EVENT_LOOP_LAG_THRESHOLD_MS} ms `
        + 'in two of the last three minutes — synchronous work is holding the loop and every '
        + 'request on that task, including the ALB health check, waits with it. Find the '
        + '"Event loop blocked" WARNING in the app-api log group for what ran at the time',
      metric: new cloudwatch.Metric({
        namespace: EVENT_LOOP_LAG_NAMESPACE,
        metricName: EVENT_LOOP_LAG_METRIC,
        period: cdk.Duration.minutes(1),
        statistic: 'Maximum',
      }),
      threshold: EVENT_LOOP_LAG_THRESHOLD_MS,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
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
