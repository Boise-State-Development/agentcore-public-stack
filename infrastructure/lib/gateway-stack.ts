import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, applyStandardTags } from './config';

export interface GatewayStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * Gateway Stack - AWS Bedrock AgentCore Gateway with MCP Tools
 * 
 * This stack creates:
 * - AgentCore Gateway with MCP protocol and AWS_IAM authorization
 * - Lambda function for Google Custom Search (web & image search)
 * - Gateway Targets connecting Lambda to Gateway as MCP tools
 * - IAM roles with appropriate permissions
 * 
 * Note: Google API credentials must be created in Secrets Manager before first deployment.
 */
export class GatewayStack extends cdk.Stack {
  public readonly gateway: agentcore.CfnGateway;
  public readonly googleSearchFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: GatewayStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Apply standard tags
    applyStandardTags(this, config);

    // ============================================================
    // Import Secrets Manager Placeholders
    // ============================================================
    
    // Google Custom Search Credentials Secret - Create with placeholder values
    // Format: {"api_key": "YOUR_KEY", "search_engine_id": "YOUR_ID"}
    // Users should update this secret with real credentials after deployment
    const googleCredentialsSecret = new secretsmanager.Secret(this, 'GoogleCredentials', {
      secretName: `${config.projectPrefix}/mcp/google-credentials`,
      description: 'Google Custom Search API credentials (update with real values)',
      secretObjectValue: {
        api_key: cdk.SecretValue.unsafePlainText('REPLACE_WITH_YOUR_GOOGLE_API_KEY'),
        search_engine_id: cdk.SecretValue.unsafePlainText('REPLACE_WITH_YOUR_SEARCH_ENGINE_ID'),
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN, // Preserve secret on stack deletion
    });

    // ============================================================
    // Lambda Execution Role
    // ============================================================
    
    const lambdaRole = new iam.Role(this, 'LambdaExecutionRole', {
      roleName: getResourceName(config, 'gateway-lambda-role'),
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for AgentCore Gateway Lambda functions',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });

    // Secrets Manager read permissions
    // Note: Using wildcard (*) suffix because AWS Secrets Manager automatically
    // appends random 6-character suffix to secret ARNs (e.g., -aeb8Cc)
    lambdaRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SecretsManagerAccess',
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue'],
        resources: [
          `${googleCredentialsSecret.secretArn}*`,
        ],
      })
    );

    // CloudWatch Logs
    lambdaRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogsAccess',
        effect: iam.Effect.ALLOW,
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/lambda/mcp-*`],
      })
    );

    // ============================================================
    // Gateway Execution Role
    // ============================================================
    
    const gatewayRole = new iam.Role(this, 'GatewayExecutionRole', {
      roleName: getResourceName(config, 'gateway-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AgentCore Gateway',
    });

    // Lambda invocation permissions
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'LambdaInvokeAccess',
        effect: iam.Effect.ALLOW,
        actions: ['lambda:InvokeFunction'],
        resources: [`arn:aws:lambda:${this.region}:${this.account}:function:mcp-*`],
      })
    );

    // CloudWatch Logs for Gateway
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GatewayLogsAccess',
        effect: iam.Effect.ALLOW,
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/gateways/*`,
        ],
      })
    );

    // ============================================================
    // Lambda Functions
    // ============================================================
    
    // Google Search Lambda Function
    this.googleSearchFunction = new lambda.Function(this, 'GoogleSearchFunction', {
      functionName: getResourceName(config, 'mcp-google-search'),
      description: 'Google Custom Search for web and images',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset('../backend/lambda-functions/google-search'),
      role: lambdaRole,
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        GOOGLE_CREDENTIALS_SECRET_NAME: googleCredentialsSecret.secretName,
        LOG_LEVEL: config.gateway.logLevel || 'INFO',
      },
    });

    // CloudWatch Log Group for Lambda
    new logs.LogGroup(this, 'GoogleSearchLogGroup', {
      logGroupName: `/aws/lambda/${this.googleSearchFunction.functionName}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ============================================================
    // AgentCore Gateway
    // ============================================================
    
    this.gateway = new agentcore.CfnGateway(this, 'MCPGateway', {
      name: getResourceName(config, 'mcp-gateway'),
      description: 'MCP Gateway for custom tools (Google Search)',
      roleArn: gatewayRole.roleArn,

      // Authentication: AWS_IAM (SigV4)
      authorizerType: 'AWS_IAM',

      // Protocol: MCP
      protocolType: 'MCP',

      // Exception level: Only DEBUG is supported
      exceptionLevel: 'DEBUG',

      // MCP Protocol Configuration
      protocolConfiguration: {
        mcp: {
          supportedVersions: ['2024-11-05'],
          searchType: 'HYBRID',
        },
      },
    });

    const gatewayArn = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/${this.gateway.attrGatewayIdentifier}`;
    const gatewayUrl = this.gateway.attrGatewayUrl;
    const gatewayId = this.gateway.attrGatewayIdentifier;

    // Lambda Permission for Gateway to invoke
    this.googleSearchFunction.addPermission('GatewayPermission', {
      principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceArn: gatewayArn,
    });

    // ============================================================
    // Gateway Targets
    // ============================================================
    
    // Google Web Search Target
    new agentcore.CfnGatewayTarget(this, 'GoogleWebSearchTarget', {
      name: 'google-web-search',
      gatewayIdentifier: gatewayId,
      description: 'Google web search',

      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],

      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.googleSearchFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'google_web_search',
                  description:
                    'Search the web using Google Custom Search API. Returns up to 5 high-quality results.',
                  inputSchema: {
                    type: 'object',
                    description: 'Search parameters',
                    required: ['query'],
                    properties: {
                      query: {
                        type: 'string',
                        description: 'Search query string',
                      },
                    },
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Google Image Search Target
    new agentcore.CfnGatewayTarget(this, 'GoogleImageSearchTarget', {
      name: 'google-image-search',
      gatewayIdentifier: gatewayId,
      description: 'Google image search',

      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],

      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.googleSearchFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'google_image_search',
                  description:
                    "Search for images using Google's image search. Returns up to 5 verified accessible images.",
                  inputSchema: {
                    type: 'object',
                    description: 'Image search parameters',
                    required: ['query'],
                    properties: {
                      query: {
                        type: 'string',
                        description: 'Search query for images',
                      },
                    },
                  },
                },
              ],
            },
          },
        },
      },
    });

    // ============================================================
    // SSM Parameters
    // ============================================================
    
    new ssm.StringParameter(this, 'GatewayUrlParameter', {
      parameterName: `/${config.projectPrefix}/gateway/url`,
      stringValue: gatewayUrl,
      description: 'AgentCore Gateway URL for remote invocation (SigV4 authenticated)',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'GatewayIdParameter', {
      parameterName: `/${config.projectPrefix}/gateway/id`,
      stringValue: gatewayId,
      description: 'AgentCore Gateway Identifier',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Outputs
    // ============================================================
    
    new cdk.CfnOutput(this, 'GatewayArn', {
      value: gatewayArn,
      description: 'AgentCore Gateway ARN',
      exportName: getResourceName(config, 'gateway-arn'),
    });

    new cdk.CfnOutput(this, 'GatewayUrl', {
      value: gatewayUrl,
      description: 'AgentCore Gateway URL (requires SigV4 authentication)',
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

    new cdk.CfnOutput(this, 'LambdaFunctionArn', {
      value: this.googleSearchFunction.functionArn,
      description: 'Google Search Lambda Function ARN',
    });

    new cdk.CfnOutput(this, 'TotalTargets', {
      value: '2',
      description: 'Total number of Gateway Targets (tools)',
    });

    new cdk.CfnOutput(this, 'UsageInstructions', {
      value: `
Gateway URL: ${gatewayUrl}
Authentication: AWS_IAM (SigV4)

⚠️  IMPORTANT: Update Google API credentials before using search tools
  aws secretsmanager put-secret-value \\
    --secret-id ${googleCredentialsSecret.secretName} \\
    --secret-string '{"api_key":"YOUR_API_KEY","search_engine_id":"YOUR_ENGINE_ID"}' \\
    --region ${this.region}

Get API credentials from:
  - API Key: https://console.cloud.google.com/apis/credentials
  - Search Engine ID: https://programmablesearchengine.google.com/

To test Gateway connectivity:
  aws bedrock-agentcore invoke-gateway \\
    --gateway-identifier ${gatewayId} \\
    --region ${this.region}
      `.trim(),
      description: 'Usage instructions for Gateway',
    });
  }
}
