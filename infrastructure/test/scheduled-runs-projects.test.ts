/**
 * Schedules that run in a project (docs/specs/shared-projects.md §7, 3.2).
 *
 * The scheduled-runs dispatcher checks a project schedule's standing (the
 * project, and its creator's member row) before each run, and both Lambdas
 * write the project's run rows and pause notices. A synth proves the two
 * things a deploy would only reveal late: both functions are told the projects
 * table and whether Projects is on, and both may read and write that table.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

function synth(projectsEnabled: boolean): Template {
  const app = new cdk.App();
  const config = createMockConfig({
    scheduledRuns: { enabled: true },
    projects: { enabled: projectsEnabled },
  });
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

function lambdaEnvironment(template: Template, idFragment: string): Record<string, unknown> {
  const matches = Object.entries(template.findResources('AWS::Lambda::Function'))
    .filter(([id]) => id.includes(idFragment))
    .map(([, r]) => r.Properties?.Environment?.Variables ?? {});
  expect(matches).toHaveLength(1);
  return matches[0];
}

function statementsFor(template: Template, idFragment: string): Array<{ Action?: string | string[]; Resource?: unknown }> {
  const out: Array<{ Action?: string | string[]; Resource?: unknown }> = [];
  for (const r of Object.values(template.findResources('AWS::IAM::Policy'))) {
    if (!JSON.stringify(r.Properties.Roles ?? []).includes(idFragment)) continue;
    out.push(...(r.Properties.PolicyDocument?.Statement ?? []));
  }
  return out;
}

const LAMBDAS = ['ScheduledRunsDispatcherLambda', 'ScheduledRunsWorkerLambda'];

describe('Scheduled runs in a project', () => {
  const on = synth(true);

  it.each(LAMBDAS)('%s knows the projects table and that Projects is on', (lambda) => {
    const env = lambdaEnvironment(on, lambda);
    expect(JSON.stringify(env.DYNAMODB_PROJECTS_TABLE_NAME)).toContain('Projects');
    expect(env.PROJECTS_ENABLED).toBe('true');
  });

  it.each(LAMBDAS)('%s may read and write the projects table', (lambda) => {
    const actions = statementsFor(on, lambda)
      .filter((s) => JSON.stringify(s.Resource ?? '').includes('Projects'))
      .flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action ?? '']));
    for (const action of ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:BatchWriteItem']) {
      expect(actions).toContain(action);
    }
  });

  it('tells both functions when Projects is off, so the dispatcher pauses instead of running', () => {
    const off = synth(false);
    for (const lambda of LAMBDAS) {
      expect(lambdaEnvironment(off, lambda).PROJECTS_ENABLED).toBe('false');
    }
  });
});
