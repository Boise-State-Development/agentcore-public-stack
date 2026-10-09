// V2 environment-payload guard (docs/specs/agentcore-runtime-v2.md §3 B4, §7.5).
//
// Two things are under test. The estimator: literals, Refs to named
// resources, Fn::Join with pseudo parameters, and the conservative fallback.
// The aspect: V2 over the limit is a synth error, V1 over the limit is a
// warning, under budget is silent — on a synthetic runtime with controlled
// sizes, and on the real PlatformStack.
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match } from 'aws-cdk-lib/assertions';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import {
  estimateRuntimeEnvironmentPayload,
  PER_VARIABLE_OVERHEAD_BYTES,
  RUNTIME_ENV_PAYLOAD_BUDGET_BYTES,
  RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES,
  RuntimeEnvironmentPayloadGuard,
  UNRESOLVED_VALUE_BYTES,
} from '../lib/constructs/inference-api/runtime-environment-payload-guard';
import { synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

function syntheticRuntime(env: Record<string, string>, platformVersion: string, extraEnv: (stack: cdk.Stack) => Record<string, string> = () => ({})) {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Guarded', { env: { account: '123456789012', region: 'us-east-1' } });
  const runtime = new bedrock.CfnRuntime(stack, 'Runtime', {
    agentRuntimeName: 'guarded_runtime',
    agentRuntimeArtifact: { containerConfiguration: { containerUri: '123456789012.dkr.ecr.us-east-1.amazonaws.com/x:latest' } },
    roleArn: 'arn:aws:iam::123456789012:role/x',
    networkConfiguration: { networkMode: 'PUBLIC' },
    environmentVariables: { ...env, ...extraEnv(stack) },
  });
  runtime.addPropertyOverride('PlatformVersion', platformVersion);
  cdk.Aspects.of(stack).add(new RuntimeEnvironmentPayloadGuard(platformVersion));
  return { app, stack, runtime };
}

function literalEnvOfBytes(target: number): Record<string, string> {
  // One long variable: key 'X' + value of (target - 1 - overhead) bytes.
  return { X: 'v'.repeat(target - 1 - PER_VARIABLE_OVERHEAD_BYTES) };
}

describe('estimateRuntimeEnvironmentPayload', () => {
  it('counts key + value + per-variable overhead for literals', () => {
    const stack = new cdk.Stack();
    const estimate = estimateRuntimeEnvironmentPayload(stack, { LOG_LEVEL: 'INFO', A: '' });
    expect(estimate.bytes).toBe('LOG_LEVEL'.length + 'INFO'.length + PER_VARIABLE_OVERHEAD_BYTES + 1 + 0 + PER_VARIABLE_OVERHEAD_BYTES);
    expect(estimate.variables).toBe(2);
    expect(estimate.unresolved).toEqual([]);
  });

  it('resolves a Ref to a table or bucket with a literal name, and Fn::Join with AWS::AccountId', () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'S', { env: { account: '123456789012', region: 'us-east-1' } });
    const table = new dynamodb.Table(stack, 'T', {
      tableName: 'acme-ai-sessions-metadata',
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    });
    const bucket = new s3.Bucket(stack, 'B', { bucketName: `acme-ai-rag-documents-${cdk.Aws.ACCOUNT_ID}` });
    const env = stack.resolve({
      DYNAMODB_SESSIONS_METADATA_TABLE_NAME: table.tableName,
      S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME: bucket.bucketName,
    });
    const estimate = estimateRuntimeEnvironmentPayload(stack, env);
    const expected =
      'DYNAMODB_SESSIONS_METADATA_TABLE_NAME'.length + 'acme-ai-sessions-metadata'.length + PER_VARIABLE_OVERHEAD_BYTES +
      'S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME'.length + 'acme-ai-rag-documents-123456789012'.length + PER_VARIABLE_OVERHEAD_BYTES;
    expect(estimate.bytes).toBe(expected);
    expect(estimate.unresolved).toEqual([]);
  });

  it('charges a conservative fixed length for a value it cannot resolve, and names it', () => {
    const stack = new cdk.Stack();
    const estimate = estimateRuntimeEnvironmentPayload(stack, {
      MYSTERY: { 'Fn::GetAtt': ['Something', 'Arn'] } as unknown as string,
    });
    expect(estimate.bytes).toBe('MYSTERY'.length + UNRESOLVED_VALUE_BYTES + PER_VARIABLE_OVERHEAD_BYTES);
    expect(estimate.unresolved).toEqual(['MYSTERY']);
  });

  it('errs high against the one data point we have (dev, 2026-10-09)', () => {
    // 49 variables summing to 2,938 bytes of key+value were counted as 3,007
    // by AWS. The estimator must not come in under AWS's figure.
    expect(2938 + 49 * PER_VARIABLE_OVERHEAD_BYTES).toBeGreaterThanOrEqual(3007);
  });
});

describe('RuntimeEnvironmentPayloadGuard on a synthetic runtime', () => {
  it('fails synth when V2 is selected and the payload exceeds the limit', () => {
    const { stack } = syntheticRuntime(literalEnvOfBytes(RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES + 1), 'V2');
    Annotations.fromStack(stack).hasError('/Guarded/Runtime', Match.stringLikeRegexp('exceeds the 2560-byte environment limit'));
    Annotations.fromStack(stack).hasError('/Guarded/Runtime', Match.stringLikeRegexp('UPDATE_ROLLBACK_FAILED'));
  });

  it('only warns when V1 is selected and the payload exceeds the limit', () => {
    const { stack } = syntheticRuntime(literalEnvOfBytes(RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES + 1), 'V1');
    Annotations.fromStack(stack).hasNoError('/Guarded/Runtime', Match.anyValue());
    Annotations.fromStack(stack).hasWarning('/Guarded/Runtime', Match.stringLikeRegexp('cannot switch to V2 until it shrinks'));
  });

  it('warns on V2 when over the budget but under the limit', () => {
    const { stack } = syntheticRuntime(literalEnvOfBytes(RUNTIME_ENV_PAYLOAD_BUDGET_BYTES + 1), 'V2');
    Annotations.fromStack(stack).hasNoError('/Guarded/Runtime', Match.anyValue());
    Annotations.fromStack(stack).hasWarning('/Guarded/Runtime', Match.stringLikeRegexp('over the 2000-byte budget'));
  });

  it('is silent at or under the budget', () => {
    const { stack } = syntheticRuntime(literalEnvOfBytes(RUNTIME_ENV_PAYLOAD_BUDGET_BYTES), 'V2');
    Annotations.fromStack(stack).hasNoError('/Guarded/Runtime', Match.anyValue());
    Annotations.fromStack(stack).hasNoWarning('/Guarded/Runtime', Match.anyValue());
  });

  it('counts the limit exactly: 2560 estimated bytes still passes on V2', () => {
    const { stack } = syntheticRuntime(literalEnvOfBytes(RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES), 'V2');
    Annotations.fromStack(stack).hasNoError('/Guarded/Runtime', Match.anyValue());
  });
});

describe('RuntimeEnvironmentPayloadGuard on the real PlatformStack', () => {
  // The estimate for the worst-case real stack is printed so the number is in
  // every test run, and the guard's verdict is checked for consistency with it
  // on both versions. Once spec §7.6 PR B drops the derivable variables, the V2
  // synth here must come back clean; until then it documents what blocks V2.
  it('V1: never an error, whatever the payload', () => {
    const { stack, template } = synthWorstCasePlatformStack({ inferenceApi: { runtimePlatformVersion: 'V1' } });
    const runtimes = template.findResources('AWS::BedrockAgentCore::Runtime');
    const [logicalId, resource] = Object.entries(runtimes)[0];
    const estimate = estimateRuntimeEnvironmentPayload(stack, (resource.Properties?.EnvironmentVariables ?? {}) as Record<string, string>);
    console.log(
      `[env-payload] ${logicalId}: ~${estimate.bytes} bytes estimated across ${estimate.variables} variables ` +
      `(limit ${RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES} on V2, budget ${RUNTIME_ENV_PAYLOAD_BUDGET_BYTES}); ` +
      `unresolved: ${estimate.unresolved.join(', ') || 'none'}; largest: ${estimate.largest.map((c) => `${c.name}=${c.bytes}`).join(', ')}`,
    );
    Annotations.fromStack(stack).hasNoError('*', Match.stringLikeRegexp('environment limit'));
  });

  it('V2: the verdict follows the estimate', () => {
    const { stack, template } = synthWorstCasePlatformStack({ inferenceApi: { runtimePlatformVersion: 'V2' } });
    const [resource] = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime'));
    const estimate = estimateRuntimeEnvironmentPayload(stack, (resource.Properties?.EnvironmentVariables ?? {}) as Record<string, string>);
    if (estimate.bytes > RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES) {
      Annotations.fromStack(stack).hasError('*', Match.stringLikeRegexp('exceeds the 2560-byte environment limit'));
    } else {
      Annotations.fromStack(stack).hasNoError('*', Match.stringLikeRegexp('environment limit'));
    }
  });
});
