/**
 * Memory maintenance worker — CDK assertions (docs/specs/shared-projects.md §4.6, 2.6).
 *
 * What a synth can prove and a deploy would only reveal late:
 *   - one arm64 image function at Lambda's maximum timeout, with no async
 *     retries (the runner already ignores a run that isn't queued);
 *   - it reads memory files and never writes them: changes reach memory only
 *     through an editor's approval, which runs in app-api;
 *   - it can call a model and count tokens, and nothing else on Bedrock;
 *   - the deploy script can find it (SSM), it has an error alarm, and the
 *     bootstrap image carries the handler's module.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';

import { AppConfig } from '../lib/config';
import { ProjectsConstruct } from '../lib/constructs/data/projects-construct';
import {
  MEMORY_MAINTENANCE_TIMEOUT_MINUTES,
  MemoryMaintenanceConstruct,
} from '../lib/constructs/memory/memory-maintenance-construct';
import { MemorySpacesConstruct } from '../lib/constructs/memory/memory-spaces-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

const BOOTSTRAP_DIR = path.resolve(__dirname, '..', 'bootstrap-assets', 'memory-maintenance');

interface PolicyStatement {
  Sid?: string;
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
}

function synth(): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', { env: { account: MOCK_ACCOUNT, region: MOCK_REGION } });
  const config: AppConfig = createMockConfig();
  const memorySpaces = new MemorySpacesConstruct(stack, 'MemorySpaces', { config });
  const projects = new ProjectsConstruct(stack, 'Projects', { config });
  new MemoryMaintenanceConstruct(stack, 'MemoryMaintenance', {
    config,
    memorySpacesTable: memorySpaces.table,
    memorySpacesBucket: memorySpaces.bucket,
    projectsTable: projects.projectsTable,
  });
  return Template.fromStack(stack);
}

const template = synth();

function workerProps(): Record<string, any> {
  const matches = Object.entries(template.findResources('AWS::Lambda::Function'))
    .filter(([id]) => id.includes('MemoryMaintenanceWorkerLambda'))
    .map(([, r]) => r.Properties);
  expect(matches).toHaveLength(1);
  return matches[0];
}

function workerStatements(): PolicyStatement[] {
  const out: PolicyStatement[] = [];
  for (const r of Object.values(template.findResources('AWS::IAM::Policy'))) {
    if (!JSON.stringify(r.Properties.Roles ?? []).includes('MemoryMaintenanceWorkerLambda')) continue;
    out.push(...(r.Properties.PolicyDocument?.Statement ?? []));
  }
  return out;
}

function actionsOn(statements: PolicyStatement[], resourceFragment: string): string[] {
  return statements
    .filter((s) => JSON.stringify(s.Resource ?? '').includes(resourceFragment))
    .flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action ?? '']));
}

describe('MemoryMaintenanceConstruct', () => {
  it('runs on arm64 at the Lambda maximum, with the tables and bucket it reads', () => {
    const props = workerProps();
    expect(props.Architectures).toEqual(['arm64']);
    expect(props.Timeout).toBe(MEMORY_MAINTENANCE_TIMEOUT_MINUTES * 60);
    expect(MEMORY_MAINTENANCE_TIMEOUT_MINUTES).toBe(15);
    expect(Object.keys(props.Environment.Variables).sort()).toEqual([
      'DYNAMODB_MEMORY_SPACES_TABLE_NAME',
      'DYNAMODB_PROJECTS_TABLE_NAME',
      'S3_MEMORY_SPACES_BUCKET_NAME',
    ]);
  });

  it('never retries an async invoke', () => {
    const configs = Object.values(template.findResources('AWS::Lambda::EventInvokeConfig'));
    expect(configs).toHaveLength(1);
    expect(configs[0].Properties.MaximumRetryAttempts).toBe(0);
  });

  it('reads memory files and never writes them', () => {
    const onBucket = actionsOn(workerStatements(), 'MemorySpacesBucket');
    expect(onBucket).toContain('s3:GetObject*');
    expect(onBucket.filter((a) => /Put|Delete|Abort/.test(a))).toEqual([]);
  });

  it('can call a model and count tokens, and nothing else on Bedrock', () => {
    const bedrock = workerStatements().find((s) => s.Sid === 'BedrockMaintenanceModel');
    expect(bedrock?.Action).toEqual(['bedrock:InvokeModel', 'bedrock:CountTokens']);
    const others = workerStatements()
      .filter((s) => s.Sid !== 'BedrockMaintenanceModel')
      .flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action ?? '']))
      .filter((a) => a.startsWith('bedrock'));
    expect(others).toEqual([]);
  });

  it('publishes its function name for the code deploy', () => {
    const names = Object.values(template.findResources('AWS::SSM::Parameter')).map((r) => r.Properties.Name);
    expect(names).toContain(`/${MOCK_PREFIX}/memory-maintenance/worker-function-name`);
  });

  it('alarms on any worker error', () => {
    const alarm = Object.values(template.findResources('AWS::CloudWatch::Alarm'))
      .map((r) => r.Properties)
      .find((p) => String(p.AlarmName).endsWith('memory-maintenance-worker-errors'));
    expect(alarm?.Threshold).toBe(1);
  });

  it('ships a bootstrap image whose module matches the real handler', () => {
    const dockerfile = fs.readFileSync(path.join(BOOTSTRAP_DIR, 'Dockerfile'), 'utf-8');
    expect(dockerfile).toContain('CMD ["worker.lambda_handler"]');
    const stub = fs.readFileSync(path.join(BOOTSTRAP_DIR, 'worker.py'), 'utf-8');
    expect(stub).toContain('def lambda_handler(');
    const real = fs.readFileSync(
      path.resolve(__dirname, '..', '..', 'backend', 'src', 'lambdas', 'memory_maintenance_worker', 'worker.py'),
      'utf-8',
    );
    expect(real).toContain('def lambda_handler(');
  });
});
