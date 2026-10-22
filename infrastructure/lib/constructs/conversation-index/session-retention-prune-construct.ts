import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as events from 'aws-cdk-lib/aws-events';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';

/** The app-api container the task overrides (`AppApiServiceConstruct`). */
export const APP_API_CONTAINER_NAME = 'app-api';

/** What the scheduled task runs instead of uvicorn. */
export const SESSION_RETENTION_PRUNE_COMMAND = [
  'python',
  '-m',
  'apis.app_api.sessions.services.retention_pruner',
];

/** 10:30 UTC, half an hour after the conversation-index reconciler. */
export const SESSION_RETENTION_PRUNE_SCHEDULE = 'cron(30 10 * * ? *)';

/** SSM parameter (under `/{prefix}/`) where the pruner records its dry runs. */
export const SESSION_RETENTION_DRY_RUN_PARAMETER = 'conversation-retention/prune-dry-run';

export interface SessionRetentionPruneConstructProps {
  config: AppConfig;
  vpc: ec2.IVpc;
  cluster: ecs.ICluster;
  /** The app-api task definition. The schedule targets its FAMILY. */
  taskDefinition: ecs.FargateTaskDefinition;
  securityGroup: ec2.ISecurityGroup;
}

/**
 * SessionRetentionPruneConstruct — once a day, deletes session rows whose
 * content has passed `conversationRetentionDays`
 * (docs/specs/conversation-search.md §3, "The session row"; PR-2c).
 *
 * WHY AN ECS TASK ON THE APP-API IMAGE, not a Lambda: pruning a session must
 * run the same delete cascade as the user's delete route (Memory events and
 * summaries, files, share snapshots, artifact shares, archived turns). That
 * cascade's import closure is most of app-api, so a Lambda would need a second
 * copy of the app-api image and a second deploy job, and would still be a
 * copy. A one-off Fargate task from app-api's own task definition runs the
 * route's code with the route's environment and role, and has no 15-minute
 * ceiling for a throttled backlog.
 *
 * WHY THE FAMILY ARN: the backend workflow ships app-api by registering a new
 * task definition revision; CloudFormation only knows the revision it
 * created. An EventBridge ECS target whose task definition ARN has no
 * revision runs the latest ACTIVE one, so the pruner always runs the code the
 * service is running.
 *
 * The task's container overrides carry the three settings the pruner reads,
 * so app-api's service environment is unchanged. The container's HTTP health
 * check fails in this task (nothing listens on :8000); ECS does not stop a
 * standalone task for that.
 *
 * DISABLED when `conversationRetentionPrunesSessions` is false. Otherwise it
 * runs daily, report-only until `conversationRetentionPruneArmed`, and the
 * first run in an environment is a dry run even when armed.
 */
export class SessionRetentionPruneConstruct extends Construct {
  public readonly rule: events.CfnRule;
  public readonly role: iam.Role;

  constructor(scope: Construct, id: string, props: SessionRetentionPruneConstructProps) {
    super(scope, id);

    const { config, vpc, cluster, taskDefinition, securityGroup } = props;
    const stack = cdk.Stack.of(this);

    const familyArn = stack.formatArn({
      service: 'ecs',
      resource: 'task-definition',
      resourceName: taskDefinition.family,
      arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
    });

    this.role = new iam.Role(this, 'ScheduleRole', {
      assumedBy: new iam.ServicePrincipal('events.amazonaws.com'),
      description: 'EventBridge runs the session retention pruner on the app-api task definition',
    });
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'RunAppApiTask',
        effect: iam.Effect.ALLOW,
        actions: ['ecs:RunTask'],
        // `family:*`: whichever revision is latest when the rule fires.
        resources: [`${familyArn}:*`],
        conditions: { ArnEquals: { 'ecs:cluster': cluster.clusterArn } },
      }),
    );
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PassAppApiTaskRoles',
        effect: iam.Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: [taskDefinition.taskRole.roleArn, taskDefinition.obtainExecutionRole().roleArn],
      }),
    );

    // The pruner records each dry run here; an armed run needs one.
    const dryRunParameterArn = stack.formatArn({
      service: 'ssm',
      resource: 'parameter',
      resourceName: `${config.projectPrefix}/${SESSION_RETENTION_DRY_RUN_PARAMETER}`,
      arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
    });
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'SessionRetentionDryRunRecord',
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter', 'ssm:PutParameter'],
        resources: [dryRunParameterArn],
      }),
    );

    const overrides = {
      containerOverrides: [
        {
          name: APP_API_CONTAINER_NAME,
          command: SESSION_RETENTION_PRUNE_COMMAND,
          environment: [
            { name: 'CONVERSATION_RETENTION_DAYS', value: String(config.conversationRetentionDays) },
            {
              name: 'CONVERSATION_RETENTION_PRUNES_SESSIONS',
              value: config.conversationRetentionPrunesSessions ? 'true' : 'false',
            },
            {
              name: 'CONVERSATION_RETENTION_PRUNE_ARMED',
              value: config.conversationRetentionPruneArmed ? 'true' : 'false',
            },
          ],
        },
      ],
    };

    this.rule = new events.CfnRule(this, 'Schedule', {
      description:
        'Session retention pruner - deletes sessions past CONVERSATION_RETENTION_DAYS (report-only until armed)',
      scheduleExpression: SESSION_RETENTION_PRUNE_SCHEDULE,
      state: config.conversationRetentionPrunesSessions ? 'ENABLED' : 'DISABLED',
      targets: [
        {
          id: 'SessionRetentionPrune',
          arn: cluster.clusterArn,
          roleArn: this.role.roleArn,
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
            // Fargate bills per task; carry the stack's tags onto it so the
            // run is visible to tag-scoped cost queries, as the service does.
            propagateTags: 'TASK_DEFINITION',
          },
          input: JSON.stringify(overrides),
          // A missed day is tomorrow's work; never two runs at once.
          retryPolicy: { maximumRetryAttempts: 0 },
        },
      ],
    });
  }
}
