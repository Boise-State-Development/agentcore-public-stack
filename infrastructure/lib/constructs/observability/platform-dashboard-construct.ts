import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';
import { ALARM_PERIOD } from './alarm-factory';

export interface PlatformDashboardConstructProps {
  config: AppConfig;
  loadBalancer: elbv2.IApplicationLoadBalancer;
  targetGroup: elbv2.IApplicationTargetGroup;
  service: ecs.FargateService;
  /** AgentCore Runtime ARN, for the runtime metric dimensions. */
  runtimeArn: string;
  /** `{agentRuntimeName}::DEFAULT`, the runtime metrics' `Name` dimension. */
  runtimeMetricName: string;
  /** Every alarm in the stack, for the alarm-status widget. */
  alarms: cloudwatch.IAlarm[];
}

/**
 * The on-call dashboard: "is the platform healthy right now".
 *
 * Rows follow triage order — is traffic being served, then why not, then which
 * alarms are already firing.
 *
 * Links to the two existing dashboards rather than restating their widgets, which
 * keeps the stack at three (CloudWatch charges $3/month beyond that).
 */
export class PlatformDashboardConstruct extends Construct {
  public readonly dashboard: cloudwatch.Dashboard;

  constructor(scope: Construct, id: string, props: PlatformDashboardConstructProps) {
    super(scope, id);

    const {
      config, loadBalancer, targetGroup, service, runtimeArn, runtimeMetricName, alarms,
    } = props;

    const agentCoreNamespace = 'AWS/Bedrock-AgentCore';
    const runtimeDimensions = {
      Resource: runtimeArn,
      Operation: 'InvokeAgentRuntime',
      Name: runtimeMetricName,
    };

    const runtimeMetric = (metricName: string) => new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName,
      dimensionsMap: runtimeDimensions,
      statistic: 'Sum',
      period: ALARM_PERIOD,
    });

    this.dashboard = new cloudwatch.Dashboard(this, 'PlatformDashboard', {
      dashboardName: getResourceName(config, 'platform-health'),
      defaultInterval: cdk.Duration.hours(3),
    });

    // ============================================================
    // Header
    // ============================================================

    this.dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: [
          `# ${config.projectPrefix} — Platform Health`,
          '',
          `**Region:** ${config.awsRegion} | `
          + `**Alarms route to:** \`${getResourceName(config, 'alarms')}\` (SNS)`,
          '',
          '**Drill-downs:** '
          + `[AgentCore Runtime detail](/cloudwatch/home?region=${config.awsRegion}#dashboards:name=${getResourceName(config, 'agentcore-observability')}) · `
          + `[Prompt cache & token economics](/cloudwatch/home?region=${config.awsRegion}#dashboards:name=${getResourceName(config, 'prompt-cache-observability')})`,
          '',
          '_The chat path is SSE, so response times of tens of seconds are normal '
          + 'and a sudden DROP in latency can mean turns are failing early._',
        ].join('\n'),
        width: 24,
        height: 4,
      }),
    );

    // ============================================================
    // Row 1 — is traffic being served?
    // ============================================================

    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Front door — requests vs 5xx',
        left: [
          loadBalancer.metrics.requestCount({ period: ALARM_PERIOD, statistic: 'Sum' }),
        ],
        right: [
          loadBalancer.metrics.httpCodeElb(
            elbv2.HttpCodeElb.ELB_5XX_COUNT, { period: ALARM_PERIOD, statistic: 'Sum' },
          ),
          targetGroup.metrics.httpCodeTarget(
            elbv2.HttpCodeTarget.TARGET_5XX_COUNT, { period: ALARM_PERIOD, statistic: 'Sum' },
          ),
        ],
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Agent — invocations vs errors & throttles',
        left: [runtimeMetric('Invocations')],
        right: [
          runtimeMetric('SystemErrors'),
          runtimeMetric('UserErrors'),
          runtimeMetric('Throttles'),
        ],
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Capacity — running tasks vs unhealthy targets',
        left: [
          new cloudwatch.Metric({
            namespace: 'ECS/ContainerInsights',
            metricName: 'RunningTaskCount',
            dimensionsMap: {
              ClusterName: service.cluster.clusterName,
              ServiceName: service.serviceName,
            },
            statistic: 'Minimum',
            period: ALARM_PERIOD,
          }),
        ],
        right: [
          targetGroup.metrics.unhealthyHostCount({ period: ALARM_PERIOD, statistic: 'Maximum' }),
        ],
        width: 8,
        height: 6,
      }),
    );

    // ============================================================
    // Row 2 — saturation: why is it unhealthy?
    // ============================================================

    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'App API saturation (CPU / memory %)',
        left: [
          service.metricCpuUtilization({ period: ALARM_PERIOD, statistic: 'Average' }),
          service.metricMemoryUtilization({ period: ALARM_PERIOD, statistic: 'Average' }),
        ],
        leftYAxis: { min: 0, max: 100 },
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Bedrock quota headroom (TPM %) — leading indicator',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/Bedrock',
            metricName: 'EstimatedTPMQuotaUsage',
            statistic: 'Maximum',
            period: ALARM_PERIOD,
          }),
        ],
        leftYAxis: { min: 0, max: 100 },
        right: [
          new cloudwatch.Metric({
            namespace: 'AWS/Bedrock',
            metricName: 'InvocationThrottles',
            statistic: 'Sum',
            period: ALARM_PERIOD,
          }),
        ],
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Data layer — DynamoDB request errors',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/DynamoDB',
            metricName: 'UserErrors',
            statistic: 'Sum',
            period: ALARM_PERIOD,
          }),
        ],
        width: 8,
        height: 6,
      }),
    );

    // ============================================================
    // Row 3 — what is already known to be broken?
    // ============================================================

    this.dashboard.addWidgets(
      new cloudwatch.AlarmStatusWidget({
        title: `All platform alarms (${alarms.length})`,
        alarms,
        width: 24,
        height: 8,
      }),
    );

    new cdk.CfnOutput(this, 'PlatformDashboardName', {
      value: this.dashboard.dashboardName,
      description: 'Single-pane platform health dashboard',
      exportName: `${config.projectPrefix}-PlatformDashboard`,
    });
  }
}
