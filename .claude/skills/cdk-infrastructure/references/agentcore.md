# Bedrock AgentCore Patterns

## Important: Naming Convention

AgentCore resources require underscores instead of hyphens:

```typescript
// CORRECT
name: getResourceName(config, 'agentcore_memory').replace(/-/g, '_')

// INCORRECT - will fail
name: getResourceName(config, 'agentcore-memory')
```

## Memory Configuration

```typescript
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import { getResourceName } from './config';

const memory = new bedrock.CfnMemory(this, 'Memory', {
  name: getResourceName(config, 'agentcore_memory').replace(/-/g, '_'),
  eventExpiryDuration: 90,  // Days (min: 7, max: 365)
  memoryExecutionRoleArn: memoryRole.roleArn,
  description: 'AgentCore Memory for conversation context',

  memoryStrategies: [
    {
      semanticMemoryStrategy: {
        name: 'SemanticFactExtraction',
        description: 'Extracts semantic facts from conversations',
      },
    },
    {
      summaryMemoryStrategy: {
        name: 'ConversationSummary',
        description: 'Generates conversation summaries',
      },
    },
    {
      userPreferenceMemoryStrategy: {
        name: 'UserPreferenceExtraction',
        description: 'Stores user preferences',
      },
    },
  ],
});

// Add dependency on role
memory.node.addDependency(memoryRole);
```

## Memory Role

```typescript
const memoryRole = new iam.Role(this, 'MemoryRole', {
  roleName: getResourceName(config, 'agentcore-memory-role'),
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  description: 'Execution role for AgentCore Memory',
});

memoryRole.addToPolicy(new iam.PolicyStatement({
  sid: 'BedrockModelAccess',
  actions: [
    'bedrock:InvokeModel',
    'bedrock:InvokeModelWithResponseStream',
  ],
  resources: ['arn:aws:bedrock:*::foundation-model/*'],
}));
```

## Gateway Configuration

```typescript
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';

const gateway = new agentcore.CfnGateway(this, 'MCPGateway', {
  name: getResourceName(config, 'mcp-gateway'),
  description: 'MCP Gateway for custom tools',
  roleArn: gatewayRole.roleArn,

  authorizerType: 'AWS_IAM',  // SigV4 authentication
  protocolType: 'MCP',        // MCP protocol only
  exceptionLevel: 'DEBUG',    // Only DEBUG supported currently

  protocolConfiguration: {
    mcp: {
      supportedVersions: ['2025-11-25'],
      searchType: 'SEMANTIC',
    },
  },
});
```

## Gateway Role

```typescript
const gatewayRole = new iam.Role(this, 'GatewayRole', {
  roleName: getResourceName(config, 'agentcore-gateway-role'),
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  description: 'Execution role for AgentCore MCP Gateway',
});

// Allow invoking Lambda functions
gatewayRole.addToPolicy(new iam.PolicyStatement({
  sid: 'LambdaInvoke',
  actions: ['lambda:InvokeFunction'],
  resources: [
    `arn:aws:lambda:${config.awsRegion}:${config.awsAccount}:function:${config.projectPrefix}-mcp-*`,
  ],
}));
```

## Gateway Target (MCP Tool)

```typescript
const gatewayTarget = new agentcore.CfnGatewayTarget(this, 'GoogleWebSearchTarget', {
  name: 'google-web-search',
  gatewayIdentifier: gateway.attrGatewayId,
  description: 'Google web search via MCP',

  credentialProviderConfigurations: [{
    credentialProviderType: 'GATEWAY_IAM_ROLE',
  }],

  targetConfiguration: {
    mcp: {
      lambda: {
        lambdaArn: googleSearchFunction.functionArn,
        toolSchema: {
          inlinePayload: [{
            name: 'google_web_search',
            description: 'Search the web using Google Custom Search API',
            inputSchema: {
              type: 'object',
              required: ['query'],
              properties: {
                query: {
                  type: 'string',
                  description: 'Search query string',
                },
                num_results: {
                  type: 'integer',
                  description: 'Number of results (1-10)',
                  default: 10,
                },
              },
            },
          }],
        },
      },
    },
  },
});

gatewayTarget.node.addDependency(gateway);
```

## Lambda Permission for Gateway

```typescript
// Allow Gateway to invoke Lambda
googleSearchFunction.addPermission('GatewayPermission', {
  principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  action: 'lambda:InvokeFunction',
  sourceArn: gateway.attrGatewayArn,
});
```

## Code Interpreter

```typescript
const codeInterpreter = new bedrock.CfnCodeInterpreterCustom(this, 'CodeInterpreter', {
  name: getResourceName(config, 'code_interpreter').replace(/-/g, '_'),
  description: 'Custom Code Interpreter for Python code execution',
  networkConfiguration: { networkMode: 'PUBLIC' },
  executionRoleArn: codeInterpreterRole.roleArn,
});

codeInterpreter.node.addDependency(codeInterpreterRole);
```

## Code Interpreter Role

```typescript
const codeInterpreterRole = new iam.Role(this, 'CodeInterpreterRole', {
  roleName: getResourceName(config, 'agentcore-code-interpreter-role'),
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  description: 'Execution role for AgentCore Code Interpreter',
});

// S3 access for file operations
codeInterpreterRole.addToPolicy(new iam.PolicyStatement({
  sid: 'S3Access',
  actions: [
    's3:GetObject',
    's3:PutObject',
    's3:ListBucket',
  ],
  resources: [
    workspaceBucket.bucketArn,
    `${workspaceBucket.bucketArn}/*`,
  ],
}));
```

## Browser

```typescript
const browser = new bedrock.CfnBrowserCustom(this, 'Browser', {
  name: getResourceName(config, 'browser').replace(/-/g, '_'),
  description: 'Custom Browser for web interaction',
  networkConfiguration: { networkMode: 'PUBLIC' },
  executionRoleArn: browserRole.roleArn,
});

browser.node.addDependency(browserRole);
```

## Browser Role

```typescript
const browserRole = new iam.Role(this, 'BrowserRole', {
  roleName: getResourceName(config, 'agentcore-browser-role'),
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  description: 'Execution role for AgentCore Browser',
});

// No additional permissions typically needed for basic browsing
```

## Runtime Role (for invoking AgentCore services)

```typescript
const runtimeRole = new iam.Role(this, 'RuntimeRole', {
  roleName: getResourceName(config, 'agentcore-runtime-role'),
  assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
  description: 'Execution role for AWS Bedrock AgentCore Runtime',
});

// Model invocation
runtimeRole.addToPolicy(new iam.PolicyStatement({
  sid: 'BedrockModelInvocation',
  actions: [
    'bedrock:InvokeModel',
    'bedrock:InvokeModelWithResponseStream',
  ],
  resources: [
    'arn:aws:bedrock:*::foundation-model/*',
    `arn:aws:bedrock:${config.awsRegion}:${config.awsAccount}:*`,
  ],
}));

// Memory access
runtimeRole.addToPolicy(new iam.PolicyStatement({
  sid: 'MemoryAccess',
  actions: [
    'bedrock-agentcore:CreateEvent',
    'bedrock-agentcore:RetrieveMemory',
    'bedrock-agentcore:ListMemorySessions',
    'bedrock-agentcore:GetMemorySession',
  ],
  resources: [memory.attrMemoryArn],
}));

// Code Interpreter access
runtimeRole.addToPolicy(new iam.PolicyStatement({
  sid: 'CodeInterpreterAccess',
  actions: [
    'bedrock-agentcore:InvokeCodeInterpreter',
    'bedrock-agentcore:CreateCodeInterpreterSession',
  ],
  resources: [codeInterpreter.attrCodeInterpreterArn],
}));

// Browser access
runtimeRole.addToPolicy(new iam.PolicyStatement({
  sid: 'BrowserAccess',
  actions: ['bedrock-agentcore:InvokeBrowser'],
  resources: [browser.attrBrowserArn],
}));

// Gateway access
runtimeRole.addToPolicy(new iam.PolicyStatement({
  sid: 'GatewayAccess',
  actions: ['bedrock-agentcore:InvokeGateway'],
  resources: [gateway.attrGatewayArn],
}));
```

## Exporting AgentCore Resources to SSM

```typescript
// Memory
new ssm.StringParameter(this, 'MemoryIdParam', {
  parameterName: `/${config.projectPrefix}/agentcore/memory-id`,
  stringValue: memory.attrMemoryId,
});

new ssm.StringParameter(this, 'MemoryArnParam', {
  parameterName: `/${config.projectPrefix}/agentcore/memory-arn`,
  stringValue: memory.attrMemoryArn,
});

// Gateway
new ssm.StringParameter(this, 'GatewayIdParam', {
  parameterName: `/${config.projectPrefix}/gateway/id`,
  stringValue: gateway.attrGatewayId,
});

new ssm.StringParameter(this, 'GatewayUrlParam', {
  parameterName: `/${config.projectPrefix}/gateway/url`,
  stringValue: gateway.attrGatewayUrl,
});

// Code Interpreter
new ssm.StringParameter(this, 'CodeInterpreterIdParam', {
  parameterName: `/${config.projectPrefix}/agentcore/code-interpreter-id`,
  stringValue: codeInterpreter.attrCodeInterpreterId,
});

// Browser
new ssm.StringParameter(this, 'BrowserIdParam', {
  parameterName: `/${config.projectPrefix}/agentcore/browser-id`,
  stringValue: browser.attrBrowserId,
});
```

## Environment Variables for Backend

```typescript
// In ECS task definition
environment: {
  AGENTCORE_MEMORY_ID: ssm.StringParameter.valueForStringParameter(
    this,
    `/${config.projectPrefix}/agentcore/memory-id`
  ),
  AGENTCORE_GATEWAY_URL: ssm.StringParameter.valueForStringParameter(
    this,
    `/${config.projectPrefix}/gateway/url`
  ),
  AGENTCORE_CODE_INTERPRETER_ID: ssm.StringParameter.valueForStringParameter(
    this,
    `/${config.projectPrefix}/agentcore/code-interpreter-id`
  ),
  AGENTCORE_BROWSER_ID: ssm.StringParameter.valueForStringParameter(
    this,
    `/${config.projectPrefix}/agentcore/browser-id`
  ),
},
```
