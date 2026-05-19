import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { InferenceApiStack } from '../lib/inference-api-stack';
import { getTruncatedResourceName } from '../lib/config';
import { createMockConfig, createMockApp, mockEnv } from './helpers/mock-config';

describe('InferenceApiStack', () => {
  let template: Template;
  let config: ReturnType<typeof createMockConfig>;

  beforeEach(() => {
    config = createMockConfig();
    const app = createMockApp(config, ['InferenceApiStack']);
    const stack = new InferenceApiStack(app, 'TestInferenceApiStack', {
      config,
      env: mockEnv(config),
    });
    template = Template.fromStack(stack);
  });

  test('synthesizes without errors', () => {
    // If we got here, synthesis succeeded
    expect(template.toJSON()).toBeDefined();
  });

  describe('IAM Runtime Execution Role', () => {
    test('exists with bedrock-agentcore service principal', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        AssumeRolePolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'sts:AssumeRole',
              Principal: Match.objectLike({
                Service: 'bedrock-agentcore.amazonaws.com',
              }),
            }),
          ]),
        }),
        Description: Match.stringLikeRegexp('.*AgentCore Runtime.*'),
      });
    });

    test('has Bedrock model invocation permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Effect: 'Allow',
              Action: Match.arrayWith([
                'bedrock:InvokeModel',
                'bedrock:InvokeModelWithResponseStream',
              ]),
            }),
          ]),
        }),
      });
    });

    test('has DynamoDB permissions for imported tables', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Effect: 'Allow',
              Action: Match.arrayWith(['dynamodb:GetItem']),
            }),
          ]),
        }),
      });
    });

    test('has Secrets Manager permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Effect: 'Allow',
              Action: Match.arrayWith([
                'secretsmanager:GetSecretValue',
                'secretsmanager:DescribeSecret',
              ]),
            }),
          ]),
        }),
      });
    });

    test('has S3 Vectors permissions for RAG', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: 'AssistantsVectorStoreAccess',
              Effect: 'Allow',
              Action: Match.arrayWith([
                's3vectors:QueryVectors',
              ]),
            }),
          ]),
        }),
      });
    });

    test('has Memory access permissions', () => {
      const inlinePolicies = template.findResources('AWS::IAM::Policy');
      const managedPolicies = template.findResources('AWS::IAM::ManagedPolicy');
      const allPolicies = { ...inlinePolicies, ...managedPolicies };

      const hasMemoryAccess = Object.values(allPolicies).some((resource: any) => {
        const statements = resource.Properties?.PolicyDocument?.Statement ?? [];
        return statements.some((s: any) =>
          s.Sid === 'MemoryAccess' &&
          s.Effect === 'Allow' &&
          Array.isArray(s.Action) &&
          s.Action.includes('bedrock-agentcore:CreateEvent') &&
          s.Action.includes('bedrock-agentcore:RetrieveMemory')
        );
      });
      expect(hasMemoryAccess).toBe(true);
    });

    test('has Code Interpreter access permissions', () => {
      const inlinePolicies = template.findResources('AWS::IAM::Policy');
      const managedPolicies = template.findResources('AWS::IAM::ManagedPolicy');
      const allPolicies = { ...inlinePolicies, ...managedPolicies };

      const hasCodeInterpreterAccess = Object.values(allPolicies).some((resource: any) => {
        const statements = resource.Properties?.PolicyDocument?.Statement ?? [];
        return statements.some((s: any) =>
          s.Sid === 'CodeInterpreterAccess' &&
          s.Effect === 'Allow' &&
          Array.isArray(s.Action) &&
          s.Action.includes('bedrock-agentcore:InvokeCodeInterpreter')
        );
      });
      expect(hasCodeInterpreterAccess).toBe(true);
    });

    test('has Browser access permissions', () => {
      const inlinePolicies = template.findResources('AWS::IAM::Policy');
      const managedPolicies = template.findResources('AWS::IAM::ManagedPolicy');
      const allPolicies = { ...inlinePolicies, ...managedPolicies };

      const hasBrowserAccess = Object.values(allPolicies).some((resource: any) => {
        const statements = resource.Properties?.PolicyDocument?.Statement ?? [];
        return statements.some((s: any) => {
          const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
          return actions.some((a: string) => a.includes('InvokeBrowser'));
        });
      });
      expect(hasBrowserAccess).toBe(true);
    });
  });

  describe('AgentCore Runtime', () => {
    test('runtime environment includes CORS_ORIGINS', () => {
      template.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
        EnvironmentVariables: Match.objectLike({
          CORS_ORIGINS: Match.anyValue(),
        }),
      });
    });

    // MCP Apps sandbox-proxy origin (PR #7 of
    // docs/kaizen/scoping/mcp-apps-host-renderer.md). Conditional-SSM
    // pattern: only wired when the mcp-sandbox stack is deployed, so a
    // disabled environment never issues an SSM read against a parameter
    // that doesn't exist.
    test('omits AGENTCORE_MCP_APPS_SANDBOX_ORIGIN when mcp-sandbox disabled', () => {
      // Default mock config has mcpSandbox.enabled = false.
      template.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
        EnvironmentVariables: Match.objectLike({
          AGENTCORE_MCP_APPS_SANDBOX_ORIGIN: Match.absent(),
        }),
      });
    });

    test('wires AGENTCORE_MCP_APPS_SANDBOX_ORIGIN when mcp-sandbox enabled', () => {
      const enabledConfig = createMockConfig({
        mcpSandbox: { enabled: true, extraFrameAncestors: [] },
      });
      const app = createMockApp(enabledConfig, ['InferenceApiStack']);
      const stack = new InferenceApiStack(app, 'SandboxEnabledStack', {
        config: enabledConfig,
        env: mockEnv(enabledConfig),
      });
      Template.fromStack(stack).hasResourceProperties(
        'AWS::BedrockAgentCore::Runtime',
        {
          EnvironmentVariables: Match.objectLike({
            AGENTCORE_MCP_APPS_SANDBOX_ORIGIN: Match.anyValue(),
          }),
        }
      );
    });
  });

  describe('AgentCore Memory', () => {
    test('CfnMemory resource exists', () => {
      template.hasResourceProperties('AWS::BedrockAgentCore::Memory', {
        Description: Match.stringLikeRegexp('.*AgentCore Memory.*'),
        EventExpiryDuration: 90,
        MemoryStrategies: Match.arrayWith([
          Match.objectLike({
            SemanticMemoryStrategy: Match.objectLike({
              Name: 'SemanticFactExtraction',
            }),
          }),
        ]),
      });
    });
  });

  describe('Code Interpreter', () => {
    test('CfnCodeInterpreterCustom resource exists', () => {
      template.hasResourceProperties('AWS::BedrockAgentCore::CodeInterpreterCustom', {
        Description: Match.stringLikeRegexp('.*Code Interpreter.*'),
        NetworkConfiguration: {
          NetworkMode: 'PUBLIC',
        },
      });
    });
  });

  describe('Browser', () => {
    test('CfnBrowserCustom resource exists', () => {
      template.hasResourceProperties('AWS::BedrockAgentCore::BrowserCustom', {
        Description: Match.stringLikeRegexp('.*Browser.*'),
        NetworkConfiguration: {
          NetworkMode: 'PUBLIC',
        },
      });
    });
  });

  describe('SSM Parameters', () => {
    test('creates 12 SSM parameters', () => {
      template.resourceCountIs('AWS::SSM::Parameter', 12);
    });

    test('exports runtime execution role ARN', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${config.projectPrefix}/inference-api/runtime-execution-role-arn`,
        Type: 'String',
      });
    });

    test('exports memory ARN', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${config.projectPrefix}/inference-api/memory-arn`,
        Type: 'String',
      });
    });

    test('exports memory ID', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${config.projectPrefix}/inference-api/memory-id`,
        Type: 'String',
      });
    });

    test('exports code interpreter ID', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${config.projectPrefix}/inference-api/code-interpreter-id`,
        Type: 'String',
      });
    });

    test('exports code interpreter ARN', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${config.projectPrefix}/inference-api/code-interpreter-arn`,
        Type: 'String',
      });
    });

    test('exports browser ID', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${config.projectPrefix}/inference-api/browser-id`,
        Type: 'String',
      });
    });

    test('exports browser ARN', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${config.projectPrefix}/inference-api/browser-arn`,
        Type: 'String',
      });
    });

    test('exports ECR repository URI', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${config.projectPrefix}/inference-api/ecr-repository-uri`,
        Type: 'String',
      });
    });
  });

  describe('Least-privilege IAM', () => {
    test('no policy has Action:* combined with Resource:*', () => {
      // Check both inline policies and managed policies (overflow)
      const inlinePolicies = template.findResources('AWS::IAM::Policy');
      const managedPolicies = template.findResources('AWS::IAM::ManagedPolicy');
      const allPolicies = { ...inlinePolicies, ...managedPolicies };

      for (const [logicalId, resource] of Object.entries(allPolicies)) {
        const statements = (resource as any).Properties?.PolicyDocument?.Statement ?? [];
        for (const stmt of statements) {
          if (stmt.Effect !== 'Allow') continue;
          const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
          const rawResources = stmt.Resource ?? [];
          const resList = Array.isArray(rawResources) ? rawResources : [rawResources];

          const hasWildcardAction = actions.includes('*');
          const allResourcesWildcard = resList.length > 0 && resList.every((r: unknown) => r === '*');

          expect(hasWildcardAction && allResourcesWildcard).toBe(false);
        }
      }
    });
  });

  // ============================================================
  // X-Ray Resource Name Length Limits
  // ============================================================

  describe('X-Ray resource name length limits', () => {
    test('sampling rule name is at most 32 characters', () => {
      const rules = template.findResources('AWS::XRay::SamplingRule');
      for (const [, resource] of Object.entries(rules)) {
        const ruleName = (resource as any).Properties?.SamplingRule?.RuleName;
        if (ruleName && typeof ruleName === 'string') {
          expect(ruleName.length).toBeLessThanOrEqual(32);
        }
      }
    });

    test('X-Ray group name is at most 32 characters', () => {
      const groups = template.findResources('AWS::XRay::Group');
      for (const [, resource] of Object.entries(groups)) {
        const groupName = (resource as any).Properties?.GroupName;
        if (groupName && typeof groupName === 'string') {
          expect(groupName.length).toBeLessThanOrEqual(32);
        }
      }
    });

    test('names stay within limits with a long project prefix', () => {
      const longConfig = createMockConfig({ projectPrefix: 'dev-boisestateai-v2' });
      const app = createMockApp(longConfig, ['InferenceApiStack']);
      const stack = new InferenceApiStack(app, 'LongPrefixStack', {
        config: longConfig,
        env: mockEnv(longConfig),
      });
      const tmpl = Template.fromStack(stack);

      const rules = tmpl.findResources('AWS::XRay::SamplingRule');
      for (const [, resource] of Object.entries(rules)) {
        const ruleName = (resource as any).Properties?.SamplingRule?.RuleName;
        if (ruleName && typeof ruleName === 'string') {
          expect(ruleName.length).toBeLessThanOrEqual(32);
        }
      }

      const groups = tmpl.findResources('AWS::XRay::Group');
      for (const [, resource] of Object.entries(groups)) {
        const groupName = (resource as any).Properties?.GroupName;
        if (groupName && typeof groupName === 'string') {
          expect(groupName.length).toBeLessThanOrEqual(32);
        }
      }
    });
  });

  // ============================================================
  // getTruncatedResourceName unit tests
  // ============================================================

  describe('getTruncatedResourceName', () => {
    test('returns full name when within limit', () => {
      const cfg = createMockConfig({ projectPrefix: 'short' });
      expect(getTruncatedResourceName(cfg, 32, 'ac-sampling')).toBe('short-ac-sampling');
    });

    test('truncates prefix when name exceeds limit', () => {
      const cfg = createMockConfig({ projectPrefix: 'dev-boisestateai-v2' });
      const name = getTruncatedResourceName(cfg, 32, 'ac-sampling');
      expect(name.length).toBeLessThanOrEqual(32);
      expect(name).toMatch(/-ac-sampling$/);
    });

    test('preserves suffix parts intact', () => {
      const cfg = createMockConfig({ projectPrefix: 'a-very-long-project-prefix-name' });
      const name = getTruncatedResourceName(cfg, 32, 'ac-traces');
      expect(name.length).toBeLessThanOrEqual(32);
      expect(name.endsWith('-ac-traces')).toBe(true);
    });
  });
});
