import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { TokenEnrichmentConstruct } from '../lib/constructs/identity/token-enrichment-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const BSU_CLAIMS = {
  'https://boisestate.edu/employee_number': 'custom:provider_sub',
};

/**
 * Synthesize a stack with a real UserPool. When `enabled`, also instantiate the
 * TokenEnrichmentConstruct — mirroring the conditional wiring in PlatformStack.
 */
function synth(
  enabled: boolean,
  accessTokenClaims: Record<string, string> = BSU_CLAIMS,
): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const config = createMockConfig({
    mcpIdentity: { tokenEnrichment: { enabled, accessTokenClaims } },
  });
  const userPool = new cognito.UserPool(stack, 'UserPool', {
    featurePlan: cognito.FeaturePlan.ESSENTIALS,
  });
  if (enabled) {
    new TokenEnrichmentConstruct(stack, 'TokenEnrichment', { config, userPool });
  }
  return Template.fromStack(stack);
}

describe('TokenEnrichmentConstruct', () => {
  describe('enabled', () => {
    let t: Template;
    beforeAll(() => {
      t = synth(true);
    });

    it('creates a single Python 3.13 ARM64 Lambda with the handler entrypoint', () => {
      // Exactly one function is created (the enrichment Lambda; a bare UserPool
      // adds none).
      t.resourceCountIs('AWS::Lambda::Function', 1);
      t.hasResourceProperties('AWS::Lambda::Function', {
        Runtime: 'python3.13',
        Handler: 'handler.handler',
        Architectures: ['arm64'],
      });
    });

    it('passes the claim map to the handler via ACCESS_TOKEN_CLAIMS env (JSON)', () => {
      t.hasResourceProperties('AWS::Lambda::Function', {
        Environment: {
          Variables: Match.objectLike({
            ACCESS_TOKEN_CLAIMS: JSON.stringify(BSU_CLAIMS),
          }),
        },
      });
    });

    it('attaches the Lambda as the pool Pre-Token-Generation v2 trigger', () => {
      t.hasResourceProperties('AWS::Cognito::UserPool', {
        LambdaConfig: Match.objectLike({
          PreTokenGenerationConfig: Match.objectLike({
            LambdaVersion: 'V2_0',
            LambdaArn: Match.anyValue(),
          }),
        }),
      });
    });

    it('grants Cognito permission to invoke the function (auto-added by addTrigger)', () => {
      t.hasResourceProperties('AWS::Lambda::Permission', {
        Action: 'lambda:InvokeFunction',
        Principal: 'cognito-idp.amazonaws.com',
      });
    });
  });

  describe('empty claim map', () => {
    it('still synthesizes with an empty JSON object env (safe no-op enrichment)', () => {
      const t = synth(true, {});
      t.hasResourceProperties('AWS::Lambda::Function', {
        Environment: {
          Variables: Match.objectLike({ ACCESS_TOKEN_CLAIMS: '{}' }),
        },
      });
    });
  });

  describe('disabled (construct not instantiated)', () => {
    let t: Template;
    beforeAll(() => {
      t = synth(false);
    });

    it('creates no Lambda function', () => {
      t.resourceCountIs('AWS::Lambda::Function', 0);
    });

    it('leaves the pool without a Pre-Token-Generation trigger', () => {
      const pools = t.findResources('AWS::Cognito::UserPool');
      const poolProps = Object.values(pools)[0]?.Properties ?? {};
      expect(poolProps.LambdaConfig?.PreTokenGenerationConfig).toBeUndefined();
    });

    it('adds no Cognito invoke permission', () => {
      t.resourceCountIs('AWS::Lambda::Permission', 0);
    });
  });
});
