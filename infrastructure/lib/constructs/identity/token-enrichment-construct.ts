import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';
import { logRetentionFor } from '../observability/log-retention';

export interface TokenEnrichmentConstructProps {
  config: AppConfig;
  /**
   * The Cognito user pool to attach the Pre-Token-Generation v2 trigger to.
   * MUST be on the ESSENTIALS (or Plus) feature plan for access-token
   * customization to take effect — CognitoConstruct sets that explicitly.
   */
  userPool: cognito.UserPool;
}

/**
 * TokenEnrichmentConstruct — Cognito Pre-Token-Generation (v2) Lambda that
 * copies configured user-pool attributes into named claims on the *access*
 * token (docs/specs/MCP_USER_IDENTITY_FORWARDING_SPEC.md).
 *
 * WHY: the access token is the only token forwarded end-to-end to MCP servers,
 * but it carries `sub` and little else. Personalized MCP tools need a stable
 * identity claim (e.g. an employee number). This trigger enriches the access
 * token in place, so the existing SPA -> app-api -> inference-api -> MCP
 * forwarding path needs no changes.
 *
 * OPT-IN: this construct is only instantiated by PlatformStack when
 * `config.mcpIdentity.tokenEnrichment.enabled` is true. When disabled (the
 * default) it produces zero resources and the pool has no trigger — the access
 * token is forwarded exactly as before. Flipping the flag on and redeploying
 * via the platform (CDK) workflow creates the Lambda + trigger and attaches it
 * to the existing pool in place; flipping it off removes them cleanly.
 *
 * BLAST RADIUS / FAIL-OPEN: the trigger runs on EVERY token issuance for the
 * whole pool. The handler (lambda-assets/token-enrichment/handler.py) is
 * dependency-free, does no I/O, and returns the event unchanged on any error,
 * so a bug degrades to "claim not added", never "login blocked".
 *
 * CODE SHIPPING: unlike the bootstrap-container Lambdas (artifact-render,
 * rag-ingestion), this handler ships its REAL code via `lambda.Code.fromAsset`
 * on the normal platform (CDK) deploy path. It's generic, tiny, dependency-free
 * and rarely changes, so there's no reason to put it on the fast backend
 * code-deploy path. The claim map is passed as an env var (function
 * configuration), so it updates on a plain CDK deploy.
 */
export class TokenEnrichmentConstruct extends Construct {
  public readonly enrichmentFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: TokenEnrichmentConstructProps) {
    super(scope, id);

    const { config, userPool } = props;

    const accessTokenClaims =
      config.mcpIdentity.tokenEnrichment?.accessTokenClaims ?? {};

    // Auto-generated log group name (no explicit name) so a failed-deploy
    // orphan can't collide with a redeploy. Short retention — these logs are
    // only useful for diagnosing a misconfigured claim map.
    const logGroup = new logs.LogGroup(this, 'TokenEnrichmentLogGroup', {
      retention: logRetentionFor(config),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.enrichmentFunction = new lambda.Function(this, 'TokenEnrichmentFunction', {
      functionName: getResourceName(config, 'token-enrichment'),
      description:
        'Cognito Pre-Token-Generation v2 trigger — copies configured user attributes into access-token claims for MCP identity forwarding (fail-open)',
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      logGroup,
      // Real handler code (NOT a bootstrap placeholder) — see class docstring.
      // `exclude` keeps the test out of the deployed zip.
      code: lambda.Code.fromAsset(
        path.resolve(__dirname, '..', '..', '..', 'lambda-assets', 'token-enrichment'),
        { exclude: ['test_*.py', '*_test.py', '__pycache__', '.pytest_cache'] },
      ),
      memorySize: 128,
      timeout: cdk.Duration.seconds(5),
      environment: {
        // {claimName: sourceCognitoAttribute}. The handler skips attributes the
        // user doesn't have, so an empty map is a safe no-op.
        ACCESS_TOKEN_CLAIMS: JSON.stringify(accessTokenClaims),
      },
    });

    // Attach as the pool's Pre-Token-Generation v2 trigger. `addTrigger`
    // automatically grants Cognito (`cognito-idp.amazonaws.com`) permission to
    // invoke the function, scoped to this pool's ARN — no manual addPermission
    // needed. V2_0 is the trigger version that can customize the ACCESS token
    // (V1 could only touch the ID token).
    userPool.addTrigger(
      cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG,
      this.enrichmentFunction,
      cognito.LambdaVersion.V2_0,
    );
  }
}
