import * as cdk from 'aws-cdk-lib';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';
import { logRetentionFor } from '../observability/log-retention';

/** Where the AgentCore service puts every Runtime's log groups. */
export const AGENTCORE_RUNTIME_LOG_GROUP_ROOT = '/aws/bedrock-agentcore/runtimes/';

/**
 * The log-group prefix that covers every generation of one Runtime.
 *
 * AgentCore names a Runtime's groups `<runtime-name>-<runtime-id>-<endpoint>`,
 * and Runtime names cannot contain `-`. So `<runtime-name>-` matches this
 * deployment's current and replaced Runtimes and cannot match another
 * deployment's, even one whose name starts with ours.
 */
export function runtimeLogGroupPrefix(agentRuntimeName: string): string {
  return `${AGENTCORE_RUNTIME_LOG_GROUP_ROOT}${agentRuntimeName}-`;
}

export interface RuntimeLogRetentionSweepConstructProps {
  config: AppConfig;
  /** The CfnRuntime's `agentRuntimeName` — stable across replacements. */
  agentRuntimeName: string;
}

/**
 * RuntimeLogRetentionSweepConstruct — keeps every generation of this
 * deployment's AgentCore Runtime log groups on the configured retention.
 *
 * `RuntimeLogRetention` (the AwsCustomResource beside the Runtime) sets
 * retention on the live `-DEFAULT` group once per deploy. It cannot reach a
 * group left behind when the Runtime is replaced (new id, new group, the old
 * one keeps whatever it had, often nothing), and it cannot hold its value in
 * an account whose landing zone re-stamps retention on every CreateLogGroup
 * event a few minutes later. These groups carry conversation text, so a
 * group that never expires is a privacy problem, not just a storage bill.
 *
 * A daily Lambda lists the groups under `runtimeLogGroupPrefix()` and lowers
 * any whose retention is unset or longer than `observability.logRetentionDays`.
 * It never raises a shorter value, never deletes a group, and never reads log
 * events. Deleting an orphaned group stays an operator decision.
 *
 * WHY A SCHEDULE, NOT AN EVENT RULE: an EventBridge rule on CreateLogGroup
 * depends on a CloudTrail trail being configured in the account, which a fork
 * cannot assume. Retention is applied to a group's existing events whenever
 * it is set, so a daily tick reaches the same end state.
 *
 * On by default with a kill switch
 * (`CDK_OBSERVABILITY_RUNTIME_LOG_RETENTION_SWEEP_ENABLED=false`) for an
 * account whose governance requires a longer retention than the stack's.
 */
export class RuntimeLogRetentionSweepConstruct extends Construct {
  public readonly sweepFunction: lambda.Function;
  public readonly scheduleRule: events.Rule;

  constructor(scope: Construct, id: string, props: RuntimeLogRetentionSweepConstructProps) {
    super(scope, id);

    const { config, agentRuntimeName } = props;
    const prefix = runtimeLogGroupPrefix(agentRuntimeName);
    const stack = cdk.Stack.of(this);

    // Auto-generated name so a failed-deploy orphan cannot collide with a redeploy.
    const logGroup = new logs.LogGroup(this, 'RuntimeLogRetentionSweepLogGroup', {
      retention: logRetentionFor(config),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.sweepFunction = new lambda.Function(this, 'RuntimeLogRetentionSweepFunction', {
      functionName: getResourceName(config, 'runtime-log-retention-sweep'),
      description:
        'Daily: applies the configured retention to every generation of this deployment\'s AgentCore Runtime log groups',
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      logGroup,
      code: lambda.Code.fromAsset(
        path.resolve(__dirname, '..', '..', '..', 'lambda-assets', 'runtime-log-retention-sweep'),
        { exclude: ['test_*.py', '*_test.py', '__pycache__', '.pytest_cache'] },
      ),
      memorySize: 128,
      timeout: cdk.Duration.minutes(2),
      environment: {
        LOG_GROUP_PREFIX: prefix,
        RETENTION_IN_DAYS: String(config.observability.logRetentionDays),
      },
    });

    // DescribeLogGroups has no resource-level scoping by name; the handler
    // filters server-side with logGroupNamePrefix.
    this.sweepFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ListLogGroups',
        effect: iam.Effect.ALLOW,
        actions: ['logs:DescribeLogGroups'],
        resources: [`arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:*`],
      }),
    );
    // The write is scoped to this deployment's Runtime groups, so a bug in
    // the handler cannot touch another deployment's retention.
    this.sweepFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'SetRuntimeLogRetention',
        effect: iam.Effect.ALLOW,
        actions: ['logs:PutRetentionPolicy'],
        resources: [`arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:${prefix}*`],
      }),
    );

    this.scheduleRule = new events.Rule(this, 'RuntimeLogRetentionSweepSchedule', {
      ruleName: getResourceName(config, 'runtime-log-retention-sweep-daily'),
      description: 'Daily retention sweep over this deployment\'s AgentCore Runtime log groups',
      schedule: events.Schedule.rate(cdk.Duration.days(1)),
      targets: [new targets.LambdaFunction(this.sweepFunction)],
    });
  }
}
