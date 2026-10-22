/**
 * Session retention pruner schedule — CDK assertions
 * (docs/specs/conversation-search.md §3, PR-2c).
 *
 * What a synth can prove:
 *   - the schedule runs app-api's task definition by FAMILY (no revision), so
 *     it follows the revisions the backend workflow registers;
 *   - it overrides only the command and the three settings the pruner reads;
 *   - it is disabled when pruning is opted out, and carries the arming flag;
 *   - EventBridge may run that family on that cluster and pass its two roles,
 *     and the task role may read and write only the dry-run record.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';

import {
  SESSION_RETENTION_PRUNE_COMMAND,
  SessionRetentionPruneConstruct,
} from '../lib/constructs/conversation-index/session-retention-prune-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

function synth(overrides: Record<string, unknown> = {}): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', { env: { account: MOCK_ACCOUNT, region: MOCK_REGION } });
  const config = createMockConfig(overrides);
  const vpc = new ec2.Vpc(stack, 'Vpc', { maxAzs: 2 });
  const cluster = new ecs.Cluster(stack, 'Cluster', { vpc });
  const taskDefinition = new ecs.FargateTaskDefinition(stack, 'TaskDef', { family: 'test-project-app-api-task' });
  taskDefinition.addContainer('AppApiContainer', {
    containerName: 'app-api',
    image: ecs.ContainerImage.fromRegistry('example/app-api:latest'),
  });
  const securityGroup = new ec2.SecurityGroup(stack, 'Sg', { vpc });
  new SessionRetentionPruneConstruct(stack, 'Prune', { config, vpc, cluster, taskDefinition, securityGroup });
  return Template.fromStack(stack);
}

function rule(t: Template): Record<string, any> {
  const rules = Object.values(t.findResources('AWS::Events::Rule')).map((r) => r.Properties);
  expect(rules).toHaveLength(1);
  return rules[0];
}

function overrides(t: Template): Record<string, any> {
  return JSON.parse(rule(t).Targets[0].Input).containerOverrides[0];
}

function statements(t: Template, roleFragment: string): any[] {
  return Object.values(t.findResources('AWS::IAM::Policy'))
    .filter((p) => JSON.stringify(p.Properties.Roles).includes(roleFragment))
    .flatMap((p) => p.Properties.PolicyDocument.Statement);
}

describe('SessionRetentionPruneConstruct', () => {
  it('runs daily on the app-api task definition family, without a revision', () => {
    const r = rule(synth());
    expect(r.ScheduleExpression).toBe('cron(30 10 * * ? *)');
    expect(r.State).toBe('ENABLED');
    const ecsParameters = r.Targets[0].EcsParameters;
    expect(ecsParameters.LaunchType).toBe('FARGATE');
    expect(ecsParameters.TaskCount).toBe(1);
    const arn = JSON.stringify(ecsParameters.TaskDefinitionArn);
    expect(arn).toContain(':task-definition/test-project-app-api-task');
    expect(arn).not.toMatch(/app-api-task:\d/);
    expect(ecsParameters.NetworkConfiguration.AwsVpcConfiguration.AssignPublicIp).toBe('DISABLED');
    expect(r.Targets[0].RetryPolicy.MaximumRetryAttempts).toBe(0);
  });

  it('overrides only the command and the pruner settings', () => {
    const o = overrides(synth());
    expect(o.name).toBe('app-api');
    expect(o.command).toEqual(SESSION_RETENTION_PRUNE_COMMAND);
    expect(Object.fromEntries(o.environment.map((e: any) => [e.name, e.value]))).toEqual({
      CONVERSATION_RETENTION_DAYS: '365',
      CONVERSATION_RETENTION_PRUNES_SESSIONS: 'true',
      CONVERSATION_RETENTION_PRUNE_ARMED: 'false',
    });
  });

  it('is disarmed by default and carries the arming flag when set', () => {
    const armed = overrides(synth({ conversationRetentionPruneArmed: true }));
    expect(armed.environment).toContainEqual({ name: 'CONVERSATION_RETENTION_PRUNE_ARMED', value: 'true' });
  });

  it('is disabled when the deployment opts out of pruning', () => {
    expect(rule(synth({ conversationRetentionPrunesSessions: false })).State).toBe('DISABLED');
  });

  it('follows the retention setting', () => {
    const o = overrides(synth({ conversationRetentionDays: 90 }));
    expect(o.environment).toContainEqual({ name: 'CONVERSATION_RETENTION_DAYS', value: '90' });
  });

  it('lets EventBridge run that family on that cluster and pass only its roles', () => {
    const s = statements(synth(), 'PruneScheduleRole');
    const run = s.find((x) => x.Sid === 'RunAppApiTask');
    expect(run.Action).toBe('ecs:RunTask');
    expect(JSON.stringify(run.Resource)).toContain(':task-definition/test-project-app-api-task:*');
    expect(run.Condition.ArnEquals['ecs:cluster']).toBeDefined();
    const pass = s.find((x) => x.Sid === 'PassAppApiTaskRoles');
    expect(pass.Action).toBe('iam:PassRole');
    expect(pass.Resource).toHaveLength(2);
  });

  it('lets the task role read and write only the dry-run record', () => {
    const s = statements(synth(), 'TaskDefTaskRole').find((x) => x.Sid === 'SessionRetentionDryRunRecord');
    expect(s.Action).toEqual(['ssm:GetParameter', 'ssm:PutParameter']);
    expect(JSON.stringify(s.Resource)).toContain(':parameter/test-project/conversation-retention/prune-dry-run');
  });
});
