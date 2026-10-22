/**
 * The app-api task role can create the SageMaker Model that Batch Transform
 * needs.
 *
 * Batch Transform is a two-step API: `CreateModel` registers the trained
 * artifact, then `CreateTransformJob` runs against that model. The task role
 * was granted the job actions but never `sagemaker:CreateModel`, so every
 * inference job from the deployed app failed at step one with
 * AccessDeniedException.
 *
 * It went unnoticed for months because the failure is invisible from a
 * developer machine: CloudTrail showed every successful `CreateModel` was
 * called by a human's SSO credentials running app-api locally against dev
 * data. The deployed task role had never once succeeded.
 *
 * The subtle part is the ARN. `sagemaker_service` names the model
 * `model-{job_name}`, so the resource is `model/model-<prefix>-...` — the
 * literal `model-` sits *ahead* of the project prefix. A pattern written to
 * match the training-job and transform-job ARNs looks right and denies every
 * call.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import {
  createMockConfig,
  mockSsmContext,
  MOCK_ACCOUNT,
  MOCK_REGION,
  MOCK_PREFIX,
} from './helpers/mock-config';

type Statement = {
  Sid?: string;
  Action?: string | string[];
  Resource?: unknown;
};

function taskRoleStatements(): Statement[] {
  const app = new cdk.App();
  const config = createMockConfig();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  const template = Template.fromStack(stack);

  const statements: Statement[] = [];
  for (const resource of Object.values(template.findResources('AWS::IAM::Policy'))) {
    statements.push(...((resource.Properties?.PolicyDocument?.Statement ?? []) as Statement[]));
  }
  for (const resource of Object.values(template.findResources('AWS::IAM::ManagedPolicy'))) {
    statements.push(...((resource.Properties?.PolicyDocument?.Statement ?? []) as Statement[]));
  }
  return statements;
}

function actionsOf(statement: Statement): string[] {
  const action = statement.Action;
  if (!action) return [];
  return Array.isArray(action) ? action : [action];
}

describe('app-api SageMaker Batch Transform IAM', () => {
  let statements: Statement[];

  beforeAll(() => {
    statements = taskRoleStatements();
  });

  it('grants sagemaker:CreateModel somewhere on the task role', () => {
    const granted = statements.some((s) => actionsOf(s).includes('sagemaker:CreateModel'));
    expect(granted).toBe(true);
  });

  it('scopes CreateModel to the model- prefixed ARN the service actually uses', () => {
    const statement = statements.find((s) =>
      actionsOf(s).includes('sagemaker:CreateModel'),
    );
    expect(statement).toBeDefined();

    const rendered = JSON.stringify(statement!.Resource);
    // sagemaker_service builds `model-{job_name}`, and job_name already starts
    // with the project prefix.
    expect(rendered).toContain(`model/model-${MOCK_PREFIX}-`);
  });

  it('still grants the transform job actions it pairs with', () => {
    const actions = statements.flatMap(actionsOf);
    expect(actions).toContain('sagemaker:CreateTransformJob');
    expect(actions).toContain('sagemaker:DescribeTransformJob');
  });

  it('does not widen to sagemaker:* anywhere on the task role', () => {
    const wildcard = statements.some((s) =>
      actionsOf(s).some((a) => a === 'sagemaker:*'),
    );
    expect(wildcard).toBe(false);
  });
});
