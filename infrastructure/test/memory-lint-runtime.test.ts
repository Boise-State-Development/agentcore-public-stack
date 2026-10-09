/**
 * Project-memory content lint on the AgentCore Runtime (docs/specs/shared-projects.md, 2.7).
 *
 * The Runtime's environment is near both of its caps (50 variables; 2,560 bytes on V2,
 * docs/specs/agentcore-runtime-v2.md B4), so it gets one small `MEMORY_LINT` value and reads a
 * deployment's sensitive patterns from SSM. The parameter exists only when there are patterns,
 * and their hash rides in `MEMORY_LINT`, so changing them rolls the Runtime.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { memoryLintRuntimeValue } from '../lib/constructs/inference-api/inference-agentcore-construct';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

const PATTERNS = '[{"pattern": "\\\\bS\\\\d{8}\\\\b", "label": "a student ID"}]';

describe('Project-memory content lint on the Runtime', () => {
  let template: Template;
  let runtimeEnv: Record<string, string>;

  beforeAll(() => {
    const config = createMockConfig({ projects: { enabled: true }, memoryLint: { mode: 'block', sensitivePatterns: PATTERNS } });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
    const runtimes = template.findResources('AWS::BedrockAgentCore::Runtime');
    runtimeEnv = (Object.values(runtimes)[0] as any).Properties.EnvironmentVariables;
  });

  it('puts the patterns in an SSM parameter the Runtime already may read', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: `/${MOCK_PREFIX}/memory/sensitive-patterns`,
      Value: PATTERNS,
      Type: 'String',
    });
  });

  it('gives the Runtime the mode and a hash of the patterns, never the patterns', () => {
    const packed = JSON.parse(runtimeEnv.MEMORY_LINT);
    expect(packed.mode).toBe('block');
    expect(packed.patterns).toMatch(/^ssm:[0-9a-f]{12}$/);
    expect(runtimeEnv.MEMORY_LINT).not.toContain('student');
    // Small whatever the patterns' size: well inside V2's 2,560-byte budget.
    expect('MEMORY_LINT'.length + runtimeEnv.MEMORY_LINT.length).toBeLessThan(60);
  });

  it('changes the Runtime when the patterns change, and only then', () => {
    const value = (sensitivePatterns: string) =>
      memoryLintRuntimeValue(createMockConfig({ memoryLint: { mode: 'warn', sensitivePatterns } }));
    expect(value('["a"]')).toBe(value('["a"]'));
    expect(value('["a"]')).not.toBe(value('["b"]'));
    expect(value('')).toBe('{"mode":"warn"}');
  });
});
