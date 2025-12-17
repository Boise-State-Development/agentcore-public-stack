import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, applyStandardTags } from './config';

export interface InferenceApiStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * Inference API Stack - AWS Bedrock AgentCore Runtime Infrastructure
 * 
 * This stack creates:
 * - AWS Bedrock AgentCore Runtime for AI agent workloads
 * - AgentCore Memory for conversation context and memory
 * - Code Interpreter Custom for Python code execution
 * - Browser Custom for web browsing capabilities
 * - IAM roles with appropriate permissions
 * 
 * Note: ECR repository is created by the build pipeline, not by CDK.
 */
export class InferenceApiStack extends cdk.Stack {
  public readonly runtime: bedrock.CfnRuntime;
  public readonly memory: bedrock.CfnMemory;
  public readonly codeInterpreter: bedrock.CfnCodeInterpreterCustom;
  public readonly browser: bedrock.CfnBrowserCustom;

  constructor(scope: Construct, id: string, props: InferenceApiStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Apply standard tags
    applyStandardTags(this, config);

    // ============================================================
    // Import Image Tag from SSM (set by push-to-ecr.sh)
    // ============================================================
    
    const imageTag = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/inference-api/image-tag`
    );

    // ============================================================
    // ECR Repository Reference
    // ============================================================
    
    // Note: ECR Repository is created automatically by the build pipeline
    // when pushing the first Docker image (see scripts/stack-inference-api/push-to-ecr.sh)
    const ecrRepository = ecr.Repository.fromRepositoryName(
      this,
      'InferenceApiRepository',
      getResourceName(config, 'inference-api')
    );

    const containerImageUri = `${ecrRepository.repositoryUri}:${imageTag}`;

    // ============================================================
    // IAM Execution Role for AgentCore Runtime
    // ============================================================
    
    const runtimeExecutionRole = new iam.Role(this, 'AgentCoreRuntimeExecutionRole', {
      roleName: getResourceName(config, 'agentcore-runtime-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AWS Bedrock AgentCore Runtime',
    });

    // CloudWatch Logs permissions - structured per AWS best practices
    // Log group creation and stream description
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:DescribeLogStreams',
        'logs:CreateLogGroup',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock-agentcore/runtimes/*`],
    }));

    // Describe all log groups (required for runtime initialization)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:DescribeLogGroups',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:*`],
    }));

    // Log stream writing
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogStream',
        'logs:PutLogEvents',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*`],
    }));

    // X-Ray tracing permissions (full tracing capability)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'xray:PutTraceSegments',
        'xray:PutTelemetryRecords',
        'xray:GetSamplingRules',
        'xray:GetSamplingTargets',
      ],
      resources: ['*'],
    }));

    // CloudWatch Metrics permissions
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'cloudwatch:PutMetricData',
      ],
      resources: ['*'],
      conditions: {
        StringEquals: {
          'cloudwatch:namespace': 'bedrock-agentcore',
        },
      },
    }));

    // Bedrock model invocation permissions (all foundation models + account resources)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockModelInvocation',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
      ],
      resources: [
        `arn:aws:bedrock:*::foundation-model/*`,
        `arn:aws:bedrock:${config.awsRegion}:${config.awsAccount}:*`,
      ],
    }));

    // AgentCore Gateway permissions (for MCP tool integration)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AgentCoreGatewayAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:InvokeGateway',
        'bedrock-agentcore:GetGateway',
        'bedrock-agentcore:ListGateways',
      ],
      resources: [`arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:gateway/*`],
    }));

    // SSM Parameter Store read permissions
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'ssm:GetParameter',
        'ssm:GetParameters',
        'ssm:GetParametersByPath',
      ],
      resources: [`arn:aws:ssm:${config.awsRegion}:${config.awsAccount}:parameter/${config.projectPrefix}/*`],
    }));

    // ECR image access - scoped to specific repository
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ECRImageAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'ecr:BatchGetImage',
        'ecr:GetDownloadUrlForLayer',
        'ecr:BatchCheckLayerAvailability',
      ],
      resources: [ecrRepository.repositoryArn],
    }));

    // ECR token access - required for authentication (must be wildcard)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ECRTokenAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'ecr:GetAuthorizationToken',
      ],
      resources: ['*'],
    }));

    // Bedrock AgentCore workload identity and access token permissions
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'GetAgentAccessToken',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:GetWorkloadAccessToken',
        'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
        'bedrock-agentcore:GetWorkloadAccessTokenForUserId',
      ],
      resources: [
        `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/default`,
        `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/default/workload-identity/hosted_agent_*`,
      ],
    }));

    // ============================================================
    // IAM Execution Role for AgentCore Memory
    // ============================================================
    
    const memoryExecutionRole = new iam.Role(this, 'AgentCoreMemoryExecutionRole', {
      roleName: getResourceName(config, 'agentcore-memory-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AWS Bedrock AgentCore Memory',
    });

    // Bedrock model access for memory processing
    memoryExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
      ],
      resources: [
        `arn:aws:bedrock:${config.awsRegion}::foundation-model/anthropic.claude-*`,
        `arn:aws:bedrock:${config.awsRegion}::foundation-model/amazon.nova-*`,
      ],
    }));

    // ============================================================
    // IAM Execution Role for Code Interpreter
    // ============================================================
    
    const codeInterpreterExecutionRole = new iam.Role(this, 'CodeInterpreterExecutionRole', {
      roleName: getResourceName(config, 'code-interpreter-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AWS Bedrock AgentCore Code Interpreter',
    });

    // CloudWatch Logs permissions
    codeInterpreterExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock/agentcore/${config.projectPrefix}/code-interpreter/*`],
    }));

    // ============================================================
    // IAM Execution Role for Browser
    // ============================================================
    
    const browserExecutionRole = new iam.Role(this, 'BrowserExecutionRole', {
      roleName: getResourceName(config, 'browser-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AWS Bedrock AgentCore Browser',
    });

    // CloudWatch Logs permissions
    browserExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock/agentcore/${config.projectPrefix}/browser/*`],
    }));

    // ============================================================
    // AgentCore Memory
    // ============================================================
    
    this.memory = new bedrock.CfnMemory(this, 'AgentCoreMemory', {
      name: getResourceName(config, 'agentcore_memory').replace(/-/g, '_'),
      eventExpiryDuration: 90, // 90 days (property expects days, not hours; max is 365, min is 7)
      memoryExecutionRoleArn: memoryExecutionRole.roleArn,
      description: 'AgentCore Memory for maintaining conversation context, user preferences, and semantic facts',
      memoryStrategies: [
        {
          semanticMemoryStrategy: {
            name: 'SemanticFactExtraction',
            description: 'Extracts and stores semantic facts from conversations',
          },
        },
        {
          summaryMemoryStrategy: {
            name: 'ConversationSummary',
            description: 'Generates and stores conversation summaries',
          },
        },
        {
          userPreferenceMemoryStrategy: {
            name: 'UserPreferenceExtraction',
            description: 'Identifies and stores user preferences',
          },
        },
      ],
    });

    // ============================================================
    // AgentCore Code Interpreter Custom
    // ============================================================
    
    this.codeInterpreter = new bedrock.CfnCodeInterpreterCustom(this, 'CodeInterpreterCustom', {
      name: getResourceName(config, 'code_interpreter').replace(/-/g, '_'),
      description: 'Custom Code Interpreter for Python code execution with advanced configuration',
      networkConfiguration: {
        networkMode: 'PUBLIC',
      },
      executionRoleArn: codeInterpreterExecutionRole.roleArn,
    });

    this.codeInterpreter.node.addDependency(codeInterpreterExecutionRole);

    // ============================================================
    // AgentCore Browser Custom
    // ============================================================
    
    this.browser = new bedrock.CfnBrowserCustom(this, 'BrowserCustom', {
      name: getResourceName(config, 'browser').replace(/-/g, '_'),
      description: 'Custom Browser for secure web interaction and data extraction',
      networkConfiguration: {
        networkMode: 'PUBLIC',
      },
      executionRoleArn: browserExecutionRole.roleArn,
    });

    this.browser.node.addDependency(browserExecutionRole);

    // ============================================================
    // AgentCore Runtime
    // ============================================================
    
    // Grant Runtime permission to access Memory
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:RetrieveMemory',
        'bedrock-agentcore:ListEvents',
      ],
      resources: [this.memory.attrMemoryArn],
    }));

    // Grant Runtime permission to use Code Interpreter
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeCodeInterpreter',
      ],
      resources: [this.codeInterpreter.attrCodeInterpreterArn],
    }));

    // Grant Runtime permission to use Browser
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeBrowser',
      ],
      resources: [this.browser.attrBrowserArn],
    }));

    this.runtime = new bedrock.CfnRuntime(this, 'AgentCoreRuntime', {
      agentRuntimeName: getResourceName(config, 'agentcore_runtime').replace(/-/g, '_'),
      roleArn: runtimeExecutionRole.roleArn,      
      agentRuntimeArtifact: {
        containerConfiguration: {
          containerUri: containerImageUri
        },
      },
      networkConfiguration: {
        networkMode: 'PUBLIC',
      },
      protocolConfiguration: 'HTTP',
      description: 'AgentCore Runtime for AI agent workloads with LangGraph and Strands framework support',
      environmentVariables: {
        // AgentCore Runtime configuration
        'LOG_LEVEL': config.inferenceApi.logLevel,
        'PROJECT_NAME': config.projectPrefix,
        'ENVIRONMENT': config.environment || 'production',
        
        // AWS Configuration
        'AWS_REGION': config.awsRegion,
        'AWS_DEFAULT_REGION': config.awsRegion,
        
        // AgentCore Resources
        'MEMORY_ARN': this.memory.attrMemoryArn,
        'MEMORY_ID': this.memory.attrMemoryId,
        'CODE_INTERPRETER_ID': this.codeInterpreter.attrCodeInterpreterId,
        'BROWSER_ID': this.browser.attrBrowserId,
        
        // AgentCore Gateway (optional - for MCP tools)
        'GATEWAY_URL': config.gateway.enabled
          ? ssm.StringParameter.valueForStringParameter(
              this,
              `/${config.projectPrefix}/gateway/url`
            )
          : '',
        
        // Authentication (from GitHub Variables)
        'ENABLE_AUTHENTICATION': config.inferenceApi.enableAuthentication
        ,
        
        // Storage directories (from GitHub Variables)
        'UPLOAD_DIR': config.inferenceApi.uploadDir,
        'OUTPUT_DIR': config.inferenceApi.outputDir,
        'GENERATED_IMAGES_DIR': config.inferenceApi.generatedImagesDir,
        
        // API URLs (from GitHub Variables)
        'API_URL': config.inferenceApi.apiUrl,
        'FRONTEND_URL': config.inferenceApi.frontendUrl,
        
        // CORS Configuration (from GitHub Variables)
        'CORS_ORIGINS': config.inferenceApi.corsOrigins,
        
        // API Keys (from GitHub Secrets)
        'TAVILY_API_KEY': config.inferenceApi.tavilyApiKey,
        'NOVA_ACT_API_KEY': config.inferenceApi.novaActApiKey,
      },
    });

    // Ensure Runtime is created after IAM roles and dependencies
    this.runtime.node.addDependency(runtimeExecutionRole);
    this.runtime.node.addDependency(memoryExecutionRole);
    this.runtime.node.addDependency(codeInterpreterExecutionRole);
    this.runtime.node.addDependency(browserExecutionRole);
    this.runtime.node.addDependency(this.memory);
    this.runtime.node.addDependency(this.codeInterpreter);
    this.runtime.node.addDependency(this.browser);

    // ============================================================
    // SSM Parameters for Cross-Stack References
    // ============================================================
    
    new ssm.StringParameter(this, 'InferenceApiRuntimeArnParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/runtime-arn`,
      stringValue: this.runtime.attrAgentRuntimeArn,
      description: 'Inference API AgentCore Runtime ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiRuntimeIdParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/runtime-id`,
      stringValue: this.runtime.attrAgentRuntimeId,
      description: 'Inference API AgentCore Runtime ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiMemoryArnParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/memory-arn`,
      stringValue: this.memory.attrMemoryArn,
      description: 'Inference API AgentCore Memory ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiMemoryIdParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/memory-id`,
      stringValue: this.memory.attrMemoryId,
      description: 'Inference API AgentCore Memory ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiCodeInterpreterIdParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/code-interpreter-id`,
      stringValue: this.codeInterpreter.attrCodeInterpreterId,
      description: 'Inference API AgentCore Code Interpreter ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiBrowserIdParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/browser-id`,
      stringValue: this.browser.attrBrowserId,
      description: 'Inference API AgentCore Browser ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // CloudFormation Outputs
    // ============================================================
    
    new cdk.CfnOutput(this, 'InferenceApiRuntimeArn', {
      value: this.runtime.attrAgentRuntimeArn,
      description: 'Inference API AgentCore Runtime ARN',
      exportName: `${config.projectPrefix}-InferenceApiRuntimeArn`,
    });

    new cdk.CfnOutput(this, 'InferenceApiRuntimeId', {
      value: this.runtime.attrAgentRuntimeId,
      description: 'Inference API AgentCore Runtime ID',
      exportName: `${config.projectPrefix}-InferenceApiRuntimeId`,
    });

    new cdk.CfnOutput(this, 'InferenceApiMemoryArn', {
      value: this.memory.attrMemoryArn,
      description: 'Inference API AgentCore Memory ARN',
      exportName: `${config.projectPrefix}-InferenceApiMemoryArn`,
    });

    new cdk.CfnOutput(this, 'InferenceApiCodeInterpreterId', {
      value: this.codeInterpreter.attrCodeInterpreterId,
      description: 'Inference API AgentCore Code Interpreter ID',
      exportName: `${config.projectPrefix}-InferenceApiCodeInterpreterId`,
    });

    new cdk.CfnOutput(this, 'InferenceApiBrowserId', {
      value: this.browser.attrBrowserId,
      description: 'Inference API AgentCore Browser ID',
      exportName: `${config.projectPrefix}-InferenceApiBrowserId`,
    });

    new cdk.CfnOutput(this, 'EcrRepositoryUri', {
      value: ecrRepository.repositoryUri,
      description: 'Inference API ECR Repository URI',
      exportName: `${config.projectPrefix}-InferenceApiEcrRepositoryUri`,
    });
   }
}
