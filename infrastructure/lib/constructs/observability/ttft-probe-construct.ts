import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as events from 'aws-cdk-lib/aws-events';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { APP_API_CONTAINER_NAME } from '../conversation-index/session-retention-prune-construct';
import { AlarmFactory } from './alarm-factory';

/** What the scheduled task runs instead of uvicorn. */
export const TTFT_PROBE_COMMAND = ['python', '-m', 'apis.app_api.telemetry.ttft_probe'];

/** Hourly, at a minute nothing else in the stack is scheduled on. */
export const TTFT_PROBE_SCHEDULE = 'cron(17 * * * ? *)';

/** Hours without a complete run before the probe alarms. */
export const TTFT_PROBE_SILENT_HOURS = 3;

export interface TtftProbeConstructProps {
  config: AppConfig;
  vpc: ec2.IVpc;
  cluster: ecs.ICluster;
  /** The app-api task definition. The schedule targets its FAMILY. */
  taskDefinition: ecs.FargateTaskDefinition;
  securityGroup: ec2.ISecurityGroup;
  /**
   * The EventBridge role that runs app-api's task definition (the session
   * retention pruner's). Reused rather than created: its grants are exactly
   * what this schedule needs (`ecs:RunTask` on the family, `iam:PassRole` on
   * the task's roles), and the stack is near CloudFormation's resource limit.
   */
  scheduleRole: iam.IRole;
  alarmTopic?: sns.ITopic;
}

/**
 * TtftProbeConstruct — a fixed turn, once an hour, for a time-to-first-token
 * baseline that does not move with traffic (docs/specs/turn-path-ttft.md §6E).
 *
 * Runs `apis.app_api.telemetry.ttft_probe` as a one-off Fargate task on
 * app-api's own task definition (the session retention pruner's pattern: same
 * image, environment and role; the family ARN runs the latest revision). It
 * times a cold first turn, a follow-up and a prewarmed first turn as one
 * dedicated user, and records `TtftProbe*Ms` in AgentCoreStack/TurnLatency.
 *
 * Enabled only when `ttftProbe.userId` is set, because the probe acts as a
 * real user: that user needs an active headless grant (it lasts up to 30 days
 * from their last sign-in) and the probe's turns count against their quota.
 *
 * One alarm, created only when enabled: no complete run for
 * TTFT_PROBE_SILENT_HOURS. Missing data breaches, so a lapsed grant, a broken
 * image or a stopped schedule all surface. Latency thresholds wait for the
 * baseline this produces.
 */
export class TtftProbeConstruct extends Construct {
  public readonly rule: events.CfnRule;
  public readonly silentAlarm?: cloudwatch.Alarm;

  constructor(scope: Construct, id: string, props: TtftProbeConstructProps) {
    super(scope, id);

    const { config, vpc, cluster, taskDefinition, securityGroup, scheduleRole } = props;
    const enabled = config.ttftProbe.userId.length > 0;
    const stack = cdk.Stack.of(this);

    const familyArn = stack.formatArn({
      service: 'ecs',
      resource: 'task-definition',
      resourceName: taskDefinition.family,
      arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
    });

    const environment = [{ name: 'TTFT_PROBE_USER_ID', value: config.ttftProbe.userId }];
    if (config.ttftProbe.modelId) {
      environment.push({ name: 'TTFT_PROBE_MODEL_ID', value: config.ttftProbe.modelId });
    }

    this.rule = new events.CfnRule(this, 'Schedule', {
      description: 'Time-to-first-token probe - a fixed turn hourly, for a baseline (docs/specs/turn-path-ttft.md 6E)',
      scheduleExpression: TTFT_PROBE_SCHEDULE,
      state: enabled ? 'ENABLED' : 'DISABLED',
      targets: [
        {
          id: 'TtftProbe',
          arn: cluster.clusterArn,
          roleArn: scheduleRole.roleArn,
          ecsParameters: {
            taskDefinitionArn: familyArn,
            taskCount: 1,
            launchType: 'FARGATE',
            networkConfiguration: {
              awsVpcConfiguration: {
                subnets: vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnetIds,
                securityGroups: [securityGroup.securityGroupId],
                assignPublicIp: 'DISABLED',
              },
            },
            propagateTags: 'TASK_DEFINITION',
          },
          input: JSON.stringify({
            containerOverrides: [{ name: APP_API_CONTAINER_NAME, command: TTFT_PROBE_COMMAND, environment }],
          }),
          // A missed hour is a gap in the graph, not work to catch up.
          retryPolicy: { maximumRetryAttempts: 0 },
        },
      ],
    });

    if (!enabled) {
      return;
    }
    this.silentAlarm = new AlarmFactory(this, config, props.alarmTopic).alarm('SilentAlarm', {
      name: 'ttft-probe-not-reporting',
      alarmDescription:
        `No complete time-to-first-token probe run in ${TTFT_PROBE_SILENT_HOURS} hours. Usual causes: the probe `
        + "user's headless grant lapsed (sign in and enable headless runs again), the probe user is over quota, "
        + 'or the Runtime is failing turns. The task logs are in the app-api log group (`ttft probe`). '
        + 'See docs/specs/turn-path-ttft.md 6E.',
      metric: new cloudwatch.Metric({
        namespace: 'AgentCoreStack/TurnLatency',
        metricName: 'TtftProbeRuns',
        statistic: 'Sum',
        period: cdk.Duration.hours(1),
      }),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      evaluationPeriods: TTFT_PROBE_SILENT_HOURS,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
  }
}
