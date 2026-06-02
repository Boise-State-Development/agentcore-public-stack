/**
 * Compute image URI resolution shape: assert that the synthesized
 * template reads the app-api task-def Image and the AgentCore
 * Runtime containerUri from SSM at deploy time, and emits the
 * bootstrap-hash CfnOutputs the seed script needs.
 *
 * Why this matters
 * ----------------
 * The compute constructs used to bake the bootstrap container URI
 * directly into the synthesized template. CFN's update semantics
 * differ by resource type:
 *   - ECS TaskDefinition: every property except Tags is "Update
 *     requires: Replacement", so any env-var / CPU / role change
 *     forced a new revision carrying the bootstrap stub Image.
 *   - AgentCore Runtime: AgentRuntimeArtifact is "No interruption",
 *     but update-agent-runtime is full-replacement at the API
 *     level, so CFN re-sent the bootstrap containerUri on any
 *     property change.
 * Both reverted live images to the bootstrap stub on infra changes.
 *
 * Fix: read Image/containerUri from SSM at deploy time. The build
 * pipeline writes the live image URI to the same SSM path, so CFN
 * always picks up the latest live image — bootstrap stub or real
 * image — on any task-def / Runtime update. See:
 *   scripts/stack-bootstrap/seed-image-tags.sh
 *   scripts/platform/deploy.sh
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

describe('Compute image URI resolution', () => {
  let template: Template;

  beforeAll(() => {
    const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
    const config = createMockConfig({
      domainName: 'example.com',
      infrastructureHostedZoneDomain: 'example.com',
      certificateArn: cert,
      frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
      artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
      mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
      fineTuning: {},
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
  });

  function findSsmParameterValue(ssmPath: string): { paramName: string; type: string } | undefined {
    const params = template.toJSON().Parameters ?? {};
    for (const [name, p] of Object.entries(params) as [string, Record<string, unknown>][]) {
      if (p.Default === ssmPath) {
        return { paramName: name, type: p.Type as string };
      }
    }
    return undefined;
  }

  it('emits an AWS::SSM::Parameter::Value<String> for app-api image-tag', () => {
    const found = findSsmParameterValue('/test-project/app-api/image-tag');
    expect(found).toBeDefined();
    expect(found!.type).toBe('AWS::SSM::Parameter::Value<String>');
  });

  it('emits an AWS::SSM::Parameter::Value<String> for inference-api image-tag', () => {
    const found = findSsmParameterValue('/test-project/inference-api/image-tag');
    expect(found).toBeDefined();
    expect(found!.type).toBe('AWS::SSM::Parameter::Value<String>');
  });

  it('the app-api task definition Image is a Ref to the app-api image-tag parameter, not a literal', () => {
    const found = findSsmParameterValue('/test-project/app-api/image-tag');
    expect(found).toBeDefined();

    const taskDefs = template.findResources('AWS::ECS::TaskDefinition');
    const appApiTaskDef = Object.values(taskDefs).find(
      (td) => (td.Properties as { Family?: string })?.Family?.includes('app-api'),
    );
    expect(appApiTaskDef).toBeDefined();

    const containers = (appApiTaskDef!.Properties as { ContainerDefinitions: Array<{ Image: unknown }> }).ContainerDefinitions;
    expect(containers).toHaveLength(1);
    expect(containers[0].Image).toEqual({ Ref: found!.paramName });
  });

  it('the AgentCore Runtime containerUri is a Ref to the inference-api image-tag parameter, not a literal', () => {
    const found = findSsmParameterValue('/test-project/inference-api/image-tag');
    expect(found).toBeDefined();

    const runtimes = template.findResources('AWS::BedrockAgentCore::Runtime');
    expect(Object.keys(runtimes)).toHaveLength(1);
    const runtime = Object.values(runtimes)[0];
    const artifact = (runtime.Properties as {
      AgentRuntimeArtifact: { ContainerConfiguration: { ContainerUri: unknown } };
    }).AgentRuntimeArtifact;

    expect(artifact.ContainerConfiguration.ContainerUri).toEqual({ Ref: found!.paramName });
  });

  it('emits bootstrap-image-hash CfnOutputs for the seed script', () => {
    const outputs = template.toJSON().Outputs ?? {};
    const outputNames = Object.keys(outputs);

    const appApiOutputName = outputNames.find((n) => n.includes('AppApiBootstrapImageHash'));
    expect(appApiOutputName).toBeDefined();
    expect(typeof outputs[appApiOutputName!].Value).toBe('string');

    const inferenceOutputName = outputNames.find((n) => n.includes('InferenceApiBootstrapImageHash'));
    expect(inferenceOutputName).toBeDefined();
    expect(typeof outputs[inferenceOutputName!].Value).toBe('string');
  });
});
