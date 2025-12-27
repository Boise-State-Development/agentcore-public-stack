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
 * - Lambda function for Brave Search (web, local, video, image, news, summarizer)
 * - Gateway Targets connecting Lambda to Gateway as MCP tools
 * - IAM roles with appropriate permissions
 *
 * Note: API credentials must be created in Secrets Manager before first deployment.
 */
export class GatewayStack extends cdk.Stack {
  public readonly gateway: agentcore.CfnGateway;
  public readonly googleSearchFunction: lambda.Function;
  public readonly braveSearchFunction: lambda.Function;

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
      description: 'Google Custom Search API credentials (update with real values)',
      secretObjectValue: {
        api_key: cdk.SecretValue.unsafePlainText('REPLACE_WITH_YOUR_GOOGLE_API_KEY'),
        search_engine_id: cdk.SecretValue.unsafePlainText('REPLACE_WITH_YOUR_SEARCH_ENGINE_ID'),
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN, // Preserve secret on stack deletion
    });

    // Brave Search Credentials Secret - Create with placeholder values
    // Format: {"api_key": "YOUR_BRAVE_API_KEY"}
    // Get API key from: https://api-dashboard.search.brave.com/app/keys
    const braveCredentialsSecret = new secretsmanager.Secret(this, 'BraveCredentials', {
      description: 'Brave Search API credentials (update with real values)',
      secretObjectValue: {
        api_key: cdk.SecretValue.unsafePlainText('REPLACE_WITH_YOUR_BRAVE_API_KEY'),
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
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
          `${braveCredentialsSecret.secretArn}*`,
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

    // Brave Search Lambda Function
    this.braveSearchFunction = new lambda.Function(this, 'BraveSearchFunction', {
      functionName: getResourceName(config, 'mcp-brave-search'),
      description: 'Brave Search for web, local, video, image, news, and summarizer',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset('../backend/lambda-functions/brave-search'),
      role: lambdaRole,
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        BRAVE_CREDENTIALS_SECRET_NAME: braveCredentialsSecret.secretName,
        LOG_LEVEL: config.gateway.logLevel || 'INFO',
      },
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
          supportedVersions: ['2025-11-25'],
          searchType: 'SEMANTIC',
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

    this.braveSearchFunction.addPermission('GatewayPermission', {
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
    // Brave Search Gateway Targets
    // ============================================================

    // Brave Web Search Target
    new agentcore.CfnGatewayTarget(this, 'BraveWebSearchTarget', {
      name: 'brave-web-search',
      gatewayIdentifier: gatewayId,
      description: 'Comprehensive web search with rich result types',

      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],

      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.braveSearchFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'brave_web_search',
                  description:
                    'Performs comprehensive web searches using Brave Search API. Returns rich results with titles, URLs, descriptions, and optional AI summaries.',
                  inputSchema: {
                    type: 'object',
                    description: 'Web search parameters',
                    required: ['query'],
                    properties: {
                      query: {
                        type: 'string',
                        description: 'Search terms (max 400 chars, 50 words)',
                      },
                      country: {
                        type: 'string',
                        description: 'Country code for localized results (default: US)',
                      },
                      count: {
                        type: 'number',
                        description: 'Number of results (1-20, default: 10)',
                      },
                      freshness: {
                        type: 'string',
                        description: 'Time filter: pd (past day), pw (past week), pm (past month), py (past year)',
                      },
                      summary: {
                        type: 'boolean',
                        description: 'Enable summary key generation for AI summarization',
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

    // Brave Local Search Target
    new agentcore.CfnGatewayTarget(this, 'BraveLocalSearchTarget', {
      name: 'brave-local-search',
      gatewayIdentifier: gatewayId,
      description: 'Local business and place search with ratings and hours',

      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],

      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.braveSearchFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'brave_local_search',
                  description:
                    'Searches for local businesses and places with detailed information including ratings, hours, and AI-generated descriptions. Requires Pro plan for full capabilities.',
                  inputSchema: {
                    type: 'object',
                    description: 'Local search parameters',
                    required: ['query'],
                    properties: {
                      query: {
                        type: 'string',
                        description: 'Search terms including location (e.g., "coffee shops in Seattle")',
                      },
                      country: {
                        type: 'string',
                        description: 'Country code for localized results (default: US)',
                      },
                      count: {
                        type: 'number',
                        description: 'Number of results (1-20, default: 10)',
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

    // Brave Video Search Target
    new agentcore.CfnGatewayTarget(this, 'BraveVideoSearchTarget', {
      name: 'brave-video-search',
      gatewayIdentifier: gatewayId,
      description: 'Video search with metadata and thumbnails',

      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],

      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.braveSearchFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'brave_video_search',
                  description:
                    'Searches for videos with comprehensive metadata including duration, views, creator info, and thumbnails.',
                  inputSchema: {
                    type: 'object',
                    description: 'Video search parameters',
                    required: ['query'],
                    properties: {
                      query: {
                        type: 'string',
                        description: 'Search terms (max 400 chars, 50 words)',
                      },
                      country: {
                        type: 'string',
                        description: 'Country code for localized results (default: US)',
                      },
                      count: {
                        type: 'number',
                        description: 'Number of results (1-50, default: 20)',
                      },
                      freshness: {
                        type: 'string',
                        description: 'Time filter: pd (past day), pw (past week), pm (past month), py (past year)',
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

    // Brave Image Search Target
    new agentcore.CfnGatewayTarget(this, 'BraveImageSearchTarget', {
      name: 'brave-image-search',
      gatewayIdentifier: gatewayId,
      description: 'Image search with URLs and metadata',

      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],

      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.braveSearchFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'brave_image_search',
                  description:
                    'Searches for images with URLs, thumbnails, and metadata. Returns image URLs (no base64 encoding).',
                  inputSchema: {
                    type: 'object',
                    description: 'Image search parameters',
                    required: ['query'],
                    properties: {
                      query: {
                        type: 'string',
                        description: 'Search terms (max 400 chars, 50 words)',
                      },
                      country: {
                        type: 'string',
                        description: 'Country code for localized results (default: US)',
                      },
                      count: {
                        type: 'number',
                        description: 'Number of results (1-200, default: 50)',
                      },
                      safesearch: {
                        type: 'string',
                        description: 'Content filtering: off, strict (default: strict)',
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

    // Brave News Search Target
    new agentcore.CfnGatewayTarget(this, 'BraveNewsSearchTarget', {
      name: 'brave-news-search',
      gatewayIdentifier: gatewayId,
      description: 'Current news search with freshness controls',

      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],

      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.braveSearchFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'brave_news_search',
                  description:
                    'Searches for current news articles with freshness controls and breaking news indicators.',
                  inputSchema: {
                    type: 'object',
                    description: 'News search parameters',
                    required: ['query'],
                    properties: {
                      query: {
                        type: 'string',
                        description: 'Search terms (max 400 chars, 50 words)',
                      },
                      country: {
                        type: 'string',
                        description: 'Country code for localized results (default: US)',
                      },
                      count: {
                        type: 'number',
                        description: 'Number of results (1-50, default: 20)',
                      },
                      freshness: {
                        type: 'string',
                        description: 'Time filter: pd (past day, default), pw (past week), pm (past month)',
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

    // Brave Summarizer Target
    new agentcore.CfnGatewayTarget(this, 'BraveSummarizerTarget', {
      name: 'brave-summarizer',
      gatewayIdentifier: gatewayId,
      description: 'AI-powered summaries from web search results',

      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],

      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.braveSearchFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'brave_summarizer',
                  description:
                    'Generates AI-powered summaries from web search results. First perform brave_web_search with summary=true, then use the returned summary key with this tool.',
                  inputSchema: {
                    type: 'object',
                    description: 'Summarizer parameters',
                    required: ['key'],
                    properties: {
                      key: {
                        type: 'string',
                        description: 'Summary key obtained from brave_web_search with summary=true',
                      },
                      entity_info: {
                        type: 'boolean',
                        description: 'Include entity information (default: false)',
                      },
                      inline_references: {
                        type: 'boolean',
                        description: 'Add source URL references inline (default: false)',
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

    new cdk.CfnOutput(this, 'GoogleLambdaArn', {
      value: this.googleSearchFunction.functionArn,
      description: 'Google Search Lambda Function ARN',
    });

    new cdk.CfnOutput(this, 'BraveLambdaArn', {
      value: this.braveSearchFunction.functionArn,
      description: 'Brave Search Lambda Function ARN',
    });

    new cdk.CfnOutput(this, 'TotalTargets', {
      value: '8',
      description: 'Total number of Gateway Targets (2 Google + 6 Brave)',
    });

    new cdk.CfnOutput(this, 'UsageInstructions', {
      value: `
Gateway URL: ${gatewayUrl}
Authentication: AWS_IAM (SigV4)

⚠️  IMPORTANT: Update API credentials before using search tools

1. Google Search credentials:
  aws secretsmanager put-secret-value \\
    --secret-id ${googleCredentialsSecret.secretName} \\
    --secret-string '{"api_key":"YOUR_API_KEY","search_engine_id":"YOUR_ENGINE_ID"}' \\
    --region ${this.region}

  Get credentials from:
  - API Key: https://console.cloud.google.com/apis/credentials
  - Search Engine ID: https://programmablesearchengine.google.com/

2. Brave Search credentials:
  aws secretsmanager put-secret-value \\
    --secret-id ${braveCredentialsSecret.secretName} \\
    --secret-string '{"api_key":"YOUR_BRAVE_API_KEY"}' \\
    --region ${this.region}

  Get API key from: https://api-dashboard.search.brave.com/app/keys

To test Gateway connectivity:
  aws bedrock-agentcore invoke-gateway \\
    --gateway-identifier ${gatewayId} \\
    --region ${this.region}
      `.trim(),
      description: 'Usage instructions for Gateway',
    });
  }
}
