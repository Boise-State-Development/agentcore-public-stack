import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface AgentCoreGatewayConstructProps {
  config: AppConfig;

  /**
   * Cognito user pool whose access tokens the Gateway trusts for inbound auth.
   *
   * Passed as a construct ref rather than read from SSM: the pool lives in this
   * same stack, and CloudFormation resolves
   * `AWS::SSM::Parameter::Value<String>` template parameters *before* any of the
   * stack's resources are created — so reading a parameter this stack publishes
   * is unsatisfiable on first deploy.
   *
   * Required when `config.gateway.inboundAuth === 'jwt'`.
   */
  userPool?: cognito.IUserPool;

  /**
   * BFF app client. Its client ID is the only value allowed in the inbound
   * token's `client_id` claim.
   *
   * Required when `config.gateway.inboundAuth === 'jwt'`.
   */
  bffAppClient?: cognito.IUserPoolClient;
}

/**
 * AgentCoreGatewayConstruct — AWS Bedrock AgentCore Gateway with MCP
 * protocol and configurable inbound authorization.
 *
 * Inbound auth (`config.gateway.inboundAuth`, default `iam`):
 *   - `iam` — AWS_IAM (SigV4). The default, and what every already-deployed
 *     Gateway uses, because the authorizer cannot be changed after creation.
 *   - `jwt` — CUSTOM_JWT trusting the platform Cognito user pool. Required for
 *     outbound on-behalf-of (OBO) token exchange, because AgentCore can only
 *     exchange a *user* subject token and an IAM-authorized Gateway has none.
 *     The agent calls the Gateway with the signed-in user's Cognito access
 *     token as a bearer token. Only takes effect on a *newly created* Gateway.
 *
 * The authorizer is immutable: changing it on an existing Gateway fails with
 * "Authorizer type cannot be updated for an existing gateway" — see the inline
 * WARNING below and docs/specs/AGENTCORE_GATEWAY_TOKEN_EXCHANGE_PLAN.md.
 *
 * AgentCore permits exactly one inbound authorizer per Gateway
 * (`authorizerType` is a scalar), but outbound credentials are *per target* —
 * so this single Gateway still fronts both IAM-invoked Lambda targets
 * (Wikipedia, ArXiv, policy-search) and OAuth/token-exchange targets (campus
 * token-service APIs). One front desk, many doors, different keys.
 *
 * Provides:
 *   - Gateway IAM execution role with NO standing Lambda-invoke grant. The
 *     permission to invoke an `mcpServer` target's Lambda Function URL is
 *     granted *per target* at registration time by app-api
 *     (`lambda:AddPermission` on the function's resource policy, naming this
 *     role), and revoked on delete — so an admin can register any same-account
 *     MCP server through the form with no infra change, and the role can invoke
 *     only the functions explicitly registered (no naming-convention wildcard).
 *     See `apis/shared/tools/gateway_lambda_grant.py`.
 *   - CloudWatch Logs publish rights for gateway-scoped log groups
 *   - `agentcore.CfnGateway` configured for MCP protocol with SEMANTIC search
 *
 * SSM publications:
 *   /{prefix}/gateway/id    — gateway identifier, read at runtime by app-api's
 *                             GatewayTargetService (issue #419) to manage MCP
 *                             targets. Reading from SSM at runtime (not at CFN
 *                             deploy time) sidesteps the same-stack ordering
 *                             deadlock that forces sibling refs (e.g. Memory id)
 *                             to be threaded as explicit props.
 *
 * Also emits CloudFormation outputs for deploy-time visibility:
 *   GatewayArn, GatewayUrl, GatewayId, GatewayStatus, UsageInstructions
 */
export class AgentCoreGatewayConstruct extends Construct {
  public readonly gateway: agentcore.CfnGateway;
  public readonly gatewayRole: iam.Role;

  constructor(
    scope: Construct,
    id: string,
    props: AgentCoreGatewayConstructProps,
  ) {
    super(scope, id);

    const { config } = props;
    const stack = cdk.Stack.of(this);

    // IMPORTANT: keep an explicit, stable roleName. This role's ARN is
    // consumed by the Gateway `roleArn` — renaming the role (auto-gen)
    // replaces it and risks Gateway replacement on deployed stacks.
    // See browser-construct.ts.
    this.gatewayRole = new iam.Role(this, 'GatewayExecutionRole', {
      roleName: getResourceName(config, 'gateway-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AgentCore Gateway',
    });

    // Lambda invoke grant for MCP targets. AWS docs require an identity-based
    // policy on the gateway service role allowing `lambda:InvokeFunction` on
    // target Lambdas, in addition to the resource-based policy added per-target
    // at registration (lambda:AddPermission). The resource-policy-only approach
    // is not sufficient — the Gateway needs both.
    // Ref: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-prerequisites-permissions.html
    // Scoped to the MCP server naming conventions used by the mcp-servers repo
    // (`mcp-*`) and this platform (`${prefix}-mcp-*`).
    this.gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'LambdaInvokeForMcpTargets',
        effect: iam.Effect.ALLOW,
        actions: ['lambda:InvokeFunction'],
        resources: [
          `arn:aws:lambda:${stack.region}:${stack.account}:function:mcp-*`,
          `arn:aws:lambda:${stack.region}:${stack.account}:function:${config.projectPrefix}-mcp-*`,
        ],
      }),
    );

    this.gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GatewayLogsAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
        ],
        resources: [
          `arn:aws:logs:${stack.region}:${stack.account}:log-group:/aws/bedrock-agentcore/gateways/*`,
        ],
      }),
    );

    // Inbound authorizer. AgentCore takes exactly one type per Gateway, so this
    // is an either/or — not additive.
    //
    // WARNING: the authorizer is immutable after creation. The AgentCore
    // control plane rejects a change on an existing Gateway with
    // "Authorizer type cannot be updated for an existing gateway" (400), even
    // though CloudFormation models both AuthorizerType and
    // AuthorizerConfiguration as "no interruption" updates and `cdk diff`
    // reports an in-place `[~]` modify. The CFN schema and the change set both
    // describe CFN's intent, not the service's validation — the failure only
    // appears at deploy time. Switching an existing deployment therefore needs
    // a new Gateway + target re-registration, not a config flip.
    const useJwtInboundAuth = config.gateway.inboundAuth === 'jwt';

    if (useJwtInboundAuth && (!props.userPool || !props.bffAppClient)) {
      throw new Error(
        "AgentCoreGatewayConstruct: config.gateway.inboundAuth is 'jwt' but " +
        'userPool and/or bffAppClient were not supplied. Both are required to ' +
        'build the CUSTOM_JWT authorizer. Pass them from PlatformStack, or set ' +
        "gateway.inboundAuth='iam' to keep SigV4 inbound auth.",
      );
    }

    const authorizerProps = useJwtInboundAuth
      ? {
          authorizerType: 'CUSTOM_JWT',
          authorizerConfiguration: {
            customJwtAuthorizer: {
              // Cognito's OIDC discovery document. Built from region + pool id
              // rather than `userPoolProviderUrl` (which is only on the concrete
              // UserPool, not IUserPool). Token-safe: userPoolId may be an
              // unresolved CFN token and interpolates fine.
              discoveryUrl:
                `https://cognito-idp.${stack.region}.amazonaws.com/` +
                `${props.userPool!.userPoolId}/.well-known/openid-configuration`,
              // MUST be allowedClients, NOT allowedAudience. Cognito *access*
              // tokens carry a `client_id` claim and no `aud` claim, so an
              // audience check can never match and would 401 every call. (The
              // travel-auth MCP server documents the same trap for its PyJWT
              // check, where JWT_AUDIENCE is deliberately left unset.)
              allowedClients: [props.bffAppClient!.userPoolClientId],
            },
          },
        }
      : { authorizerType: 'AWS_IAM' };

    this.gateway = new agentcore.CfnGateway(this, 'MCPGateway', {
      name: getResourceName(config, 'mcp-gateway'),
      description: 'MCP Gateway for external tools',
      roleArn: this.gatewayRole.roleArn,
      ...authorizerProps,
      protocolType: 'MCP',
      exceptionLevel: 'DEBUG', // Only DEBUG is supported
      protocolConfiguration: {
        mcp: {
          supportedVersions: ['2025-11-25'],
          searchType: 'SEMANTIC',
        },
      },
    });

    const gatewayArn = `arn:aws:bedrock-agentcore:${stack.region}:${stack.account}:gateway/${this.gateway.attrGatewayIdentifier}`;
    const gatewayUrl = this.gateway.attrGatewayUrl;
    const gatewayId = this.gateway.attrGatewayIdentifier;

    // Publish the gateway id so app-api (a sibling in this stack) can resolve
    // it at runtime via SSM to manage MCP targets (issue #419). app-api reads
    // this with `ssm:GetParameter` from the running container — never at CFN
    // synth/deploy time — so there is no same-stack publish/consume ordering
    // problem.
    new ssm.StringParameter(this, 'GatewayIdParam', {
      parameterName: `/${config.projectPrefix}/gateway/id`,
      stringValue: gatewayId,
      description: 'AgentCore Gateway identifier (consumed by app-api GatewayTargetService)',
    });

    new cdk.CfnOutput(this, 'GatewayArn', {
      value: gatewayArn,
      description: 'AgentCore Gateway ARN',
      exportName: getResourceName(config, 'gateway-arn'),
    });

    new cdk.CfnOutput(this, 'GatewayUrl', {
      value: gatewayUrl,
      description: useJwtInboundAuth
        ? 'AgentCore Gateway URL (requires a Cognito access token as a Bearer token)'
        : 'AgentCore Gateway URL (requires SigV4 authentication)',
      exportName: getResourceName(config, 'gateway-url'),
    });

    new cdk.CfnOutput(this, 'GatewayId', {
      value: gatewayId,
      description: 'AgentCore Gateway Identifier',
      exportName: getResourceName(config, 'gateway-id'),
    });

    new cdk.CfnOutput(this, 'GatewayStatus', {
      value: this.gateway.attrStatus,
      description: 'Gateway Status',
    });

    new cdk.CfnOutput(this, 'GatewayInboundAuth', {
      value: useJwtInboundAuth ? 'CUSTOM_JWT' : 'AWS_IAM',
      description: 'Gateway inbound authorizer type (config.gateway.inboundAuth)',
    });

    new cdk.CfnOutput(this, 'UsageInstructions', {
      value: useJwtInboundAuth
        ? `
Gateway URL: ${gatewayUrl}
Authentication: CUSTOM_JWT (Cognito access token as Bearer)

The agent sends the signed-in user's Cognito access token per invocation:
  Authorization: Bearer <cognito-access-token>

Accepted only when the token's client_id matches the BFF app client.
Note: Cognito access tokens have no 'aud' claim — validation is on client_id.
        `.trim()
        : `
Gateway URL: ${gatewayUrl}
Authentication: AWS_IAM (SigV4)

To test Gateway connectivity:
  aws bedrock-agentcore invoke-gateway \\
    --gateway-identifier ${gatewayId} \\
    --region ${stack.region}
        `.trim(),
      description: 'Usage instructions for Gateway',
    });
  }
}
