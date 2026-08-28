/**
 * Fine-tuning runtime flags reach the app-api container.
 *
 * The fine-tuning tables, bucket, SageMaker role and IAM grants have always
 * been provisioned unconditionally, but the two variables that decide whether
 * the feature is *usable* were never passed to the container:
 *
 *   - `FINE_TUNING_ENABLED` mounts the `/fine-tuning` and `/admin/fine-tuning`
 *     routers (app_api/main.py, admin/routes.py). It defaults to "false" in
 *     Python, so every deployed environment served 404s from a fully built and
 *     fully provisioned feature.
 *   - `FINE_TUNING_DEFAULT_QUOTA_HOURS` selects whitelist-only mode (0) versus
 *     open access with a default budget. Its absence defaults to 0, so users
 *     get a 403 instead of the intended automatic grant.
 *
 * The trap was a name collision: a GitHub variable `CDK_FINE_TUNING_ENABLED`
 * existed and read `true`, but it had gated whether the SageMaker *stack*
 * deployed and its consumer was deleted in the single-stack migration (#396).
 * Reading the repo settings suggested the feature was on; the container env
 * said otherwise. These tests assert the container, not the settings.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';
import { AppConfig } from '../lib/config';

function appApiEnvironment(config: AppConfig): Record<string, unknown> {
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  const template = Template.fromStack(stack);

  const taskDefs = template.findResources('AWS::ECS::TaskDefinition');
  for (const resource of Object.values(taskDefs)) {
    const containers = (resource.Properties?.ContainerDefinitions ?? []) as Array<{
      Name?: string;
      Environment?: Array<{ Name: string; Value: unknown }>;
    }>;
    const appApi = containers.find((c) => c.Name === 'app-api');
    if (appApi) {
      return Object.fromEntries(
        (appApi.Environment ?? []).map((e) => [e.Name, e.Value]),
      );
    }
  }

  throw new Error('No app-api container found in any ECS task definition');
}

describe('fine-tuning runtime flags on the app-api container', () => {
  it('mounts the routers by default', () => {
    const env = appApiEnvironment(createMockConfig());

    // Python compares this lowercased against "true".
    expect(env.FINE_TUNING_ENABLED).toBe('true');
  });

  it('passes the default quota through', () => {
    const env = appApiEnvironment(
      createMockConfig({ fineTuning: { enabled: true, defaultQuotaHours: 10 } }),
    );

    expect(env.FINE_TUNING_DEFAULT_QUOTA_HOURS).toBe('10');
  });

  it('emits a quota of 0 as "0", not an empty string', () => {
    // A missing or empty value silently means whitelist-only. Assert the
    // literal so a stringify regression cannot pass unnoticed.
    const env = appApiEnvironment(
      createMockConfig({ fineTuning: { enabled: true, defaultQuotaHours: 0 } }),
    );

    expect(env.FINE_TUNING_DEFAULT_QUOTA_HOURS).toBe('0');
  });

  it('honours the kill switch', () => {
    const env = appApiEnvironment(
      createMockConfig({ fineTuning: { enabled: false, defaultQuotaHours: 0 } }),
    );

    expect(env.FINE_TUNING_ENABLED).toBe('false');
  });

  it('still provisions storage when the routes are switched off', () => {
    // Storage is unconditional by design — turning the routes off must not
    // orphan or destroy a user's datasets and trained models.
    const env = appApiEnvironment(
      createMockConfig({ fineTuning: { enabled: false, defaultQuotaHours: 0 } }),
    );

    expect(env.DYNAMODB_FINE_TUNING_JOBS_TABLE_NAME).toBeDefined();
    expect(env.DYNAMODB_FINE_TUNING_ACCESS_TABLE_NAME).toBeDefined();
    expect(env.S3_FINE_TUNING_BUCKET_NAME).toBeDefined();
  });
});
