/**
 * The api-converse in-flight cap reaches the app-api container.
 *
 * backend/src/apis/app_api/chat/bedrock_offload.py reads API_CONVERSE_MAX_IN_FLIGHT
 * to size the worker pool its Bedrock calls run on and to refuse calls past it
 * with 429. Without the variable the container falls back to its own default,
 * so an operator's tuning would be accepted by CDK and ignored by the task.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';
import { API_CONVERSE_MAX_IN_FLIGHT_DEFAULT, AppConfig } from '../lib/config';

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
      return Object.fromEntries((appApi.Environment ?? []).map((e) => [e.Name, e.Value]));
    }
  }
  throw new Error('No app-api container found in any ECS task definition');
}

describe('API_CONVERSE_MAX_IN_FLIGHT on the app-api container', () => {
  it('ships the default', () => {
    const env = appApiEnvironment(createMockConfig());
    expect(env.API_CONVERSE_MAX_IN_FLIGHT).toBe(String(API_CONVERSE_MAX_IN_FLIGHT_DEFAULT));
  });

  it('passes a configured cap through as a string', () => {
    const config = createMockConfig();
    config.appApi.apiConverseMaxInFlight = 4;
    expect(appApiEnvironment(config).API_CONVERSE_MAX_IN_FLIGHT).toBe('4');
  });
});
